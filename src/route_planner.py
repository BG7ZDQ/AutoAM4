# -*- coding: utf-8 -*-
"""航线开辟规划器：机场/机型下拉、候选筛选、收益预估、一键建设。

数据源：AM4Help（abc8747/am4）开源数据
- data/airports.csv          机场列表（CSV 行序 = 需求矩阵索引）
- data/aircrafts.csv         机型列表（501 款，speed 为 realism 基础速度）
- data/demands-v1.00.bin     需求矩阵前半（rkyv 序列化，按字节对半切开）
- data/demands-v1.01.bin     需求矩阵后半（读取时拼接还原）

收益计算：逐行移植 AM4Help 的 Rust 实现（schedule/metrics/ticket/config），
默认 Realism 模式（与正式账号一致）、CI=200、训练全关；客运与货运载运系数均为 0.95、
油价 600、CO2 130。直飞距离超过机型航程时自动查找最优经停（同 AM4Help 的
find_by_efficiency：两段均在航程内且总距离最小），飞行时长按总距离计算。
需求来自离线矩阵（某日快照），点选单条后仍可抓游戏实时需求精算。
"""
from __future__ import annotations

import csv
import html
import math
import re
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from collector import (
    BASE, HOME, _do_curl, _ensure_login, classify_takeoff_response, parse_status_data,
)

try:
    import numpy as _np
except Exception:  # numpy 不可用时降级为纯 Python 逐条计算
    _np = None

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# 默认预估参数
DEFAULT_PAX_LOAD = 0.95
DEFAULT_CARGO_LOAD = 0.95
DEFAULT_FUEL_PRICE = 600          # $/1000 Lbs
DEFAULT_CO2_PRICE = 130           # $/1000 kg
DEFAULT_COST_INDEX = 200

# 当前项目固定使用 Realism 模式；简易模式测试账号仅用于安全实跑。
DEFAULT_GAME_MODE = "realism"
SPEED_MULT = {"realism": 1.0, "easy": 1.5}
ACHECK_MULT = {"realism": 2.0, "easy": 1.0}
CONTRIBUTION_MULT = {"realism": 1.5, "easy": 1.0}
EARTH_RADIUS_KM = 6371.0
MIN_DISTANCE_KM = 100.0           # 短于该距离视为无效航线

_airports_cache: list[dict] | None = None
_aircraft_cache: list[dict] | None = None
_demands_cache: bytes | None = None
_route_count: int | None = None
_lats_rad: list[float] | None = None
_lons_rad: list[float] | None = None
_rwys: list[int] | None = None


def json_load(path: Path) -> list[dict]:
    import json
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 数据加载

def _load_airports() -> list[dict]:
    """加载机场 CSV；行序即需求矩阵索引（与 AM4Help 一致）。"""
    global _airports_cache, _lats_rad, _lons_rad, _rwys
    if _airports_cache is not None:
        return _airports_cache
    rows: list[dict] = []
    lats: list[float] = []
    lons: list[float] = []
    rwys: list[int] = []
    path = DATA_DIR / "airports.csv"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                lat = float(r.get("lat", 0) or 0)
                lng = float(r.get("lng", 0) or 0)
                rows.append({
                    "idx": i,
                    "id": r["id"],
                    "name": r["name"],
                    "fullname": r.get("fullname", ""),
                    "country": r.get("country", ""),
                    "continent": r.get("continent", ""),
                    "iata": r.get("iata", ""),
                    "icao": r.get("icao", ""),
                    "lat": lat,
                    "lng": lng,
                    "rwy": int(r.get("rwy", 0) or 0),
                    "market": r.get("market", ""),
                    "hub_cost": r.get("hub_cost", "0"),
                })
                lats.append(math.radians(lat))
                lons.append(math.radians(lng))
                rwys.append(int(r.get("rwy", 0) or 0))
    _airports_cache = rows
    _lats_rad = lats
    _lons_rad = lons
    _rwys = rwys
    return rows


def _load_aircraft_models() -> list[dict]:
    """机型列表：AM4Help aircrafts.csv（501 款，含 MC-21-400）。"""
    global _aircraft_cache
    if _aircraft_cache is not None:
        return _aircraft_cache
    rows: list[dict] = []
    path = DATA_DIR / "aircrafts.csv"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                def num(key, cast):
                    try:
                        return cast(r.get(key, 0) or 0)
                    except ValueError:
                        return 0
                rows.append({
                    "id": r["id"],
                    "shortname": r.get("shortname", ""),
                    "manufacturer": r.get("manufacturer", ""),
                    "name": r["name"],
                    "type": r.get("type", "0"),
                    "priority": r.get("priority", "0"),
                    "eid": r.get("eid", ""),
                    "ename": r.get("ename", ""),
                    "speed": num("speed", float),
                    "fuel": num("fuel", float),
                    "co2": num("co2", float),
                    "cost": num("cost", int),
                    "capacity": num("capacity", int),
                    "rwy": num("rwy", int),
                    "check_cost": num("check_cost", int),
                    "range": num("range", int),
                    "ceil": num("ceil", int),
                    "maint": num("maint", int),
                    "pilots": num("pilots", int),
                    "crew": num("crew", int),
                    "engineers": num("engineers", int),
                    "technicians": num("technicians", int),
                    "img": r.get("img", ""),
                    "wingspan": num("wingspan", int),
                    "length": num("length", int),
                })
    _aircraft_cache = rows
    return rows


def _load_demands() -> bytes:
    """拼接两个需求分片，返回完整 rkyv 归档字节（数据区从偏移 0 开始）。"""
    global _demands_cache
    if _demands_cache is not None:
        return _demands_cache
    p0 = DATA_DIR / "demands-v1.00.bin"
    p1 = DATA_DIR / "demands-v1.01.bin"
    if not (p0.exists() and p1.exists()):
        raise FileNotFoundError("缺少 data/demands-v1.00.bin / demands-v1.01.bin")
    buf = p0.read_bytes() + p1.read_bytes()
    _demands_cache = buf
    return buf


def _route_count() -> int:
    """矩阵路由总数（读根对象末尾的 len 字段）。"""
    global _route_count
    if _route_count is None:
        buf = _load_demands()
        _route_count = struct.unpack_from("<I", buf, len(buf) - 4)[0]
    return _route_count


def _route_index(i: int, j: int) -> int:
    """严格上三角矩阵索引（AM4Help 同款公式，i<j 取小者在前）。"""
    if i == j:
        raise ValueError("same airport")
    if i > j:
        i, j = j, i
    n = len(_load_airports())
    return i * (2 * n - i - 1) // 2 + (j - i - 1)


def demand_for(origin: dict, dest: dict) -> tuple[int, int, int]:
    """离线需求 (Y, J, F)。"""
    buf = _load_demands()
    off = _route_index(origin["idx"], dest["idx"]) * 6
    return struct.unpack_from("<HHH", buf, off)


# ---------------------------------------------------------------- 查询

def search_airports(q: str = "", limit: int = 100) -> list[dict]:
    """按名称 / IATA / ICAO / 国家模糊搜索机场。"""
    q = (q or "").strip().lower()
    airports = _load_airports()
    if not q:
        return airports[:limit]
    out = []
    for a in airports:
        hay = " ".join([a["name"], a["iata"], a["icao"], a["country"], a["fullname"]]).lower()
        if q in hay:
            out.append(a)
            if len(out) >= limit:
                break
    return out


def airport_by_id(airport_id: str) -> dict | None:
    for a in _load_airports():
        if a["id"] == str(airport_id):
            return a
    return None


def airport_by_iata(iata: str) -> dict | None:
    for a in _load_airports():
        if a.get("iata", "").upper() == str(iata).upper():
            return a
    return None


def aircraft_by_name(name: str, engine: str | None = None) -> dict | None:
    """按机型名和可选发动机 ID 取唯一配置；指定错误发动机时不静默降级。"""
    wanted_engine = str(engine).strip() if engine is not None else ""
    for m in _load_aircraft_models():
        if (m["name"].lower() == str(name).lower()
                and (not wanted_engine or str(m.get("eid", "")) == wanted_engine)):
            return m
    return None


def aircraft_engines(name: str) -> list[dict]:
    """返回同一机型的全部发动机配置（CSV priority 顺序即游戏默认顺序）。"""
    low = str(name).lower()
    return [m for m in _load_aircraft_models() if m["name"].lower() == low]


def _order_error(resp: str) -> str | None:
    """从下单响应中提取错误信息（toast 或 alert-danger 面板）；无错误返回 None。"""
    if not resp:
        return None
    m = re.search(r"toast\('([^']*)','([^']*)','error'\)", resp)
    if m:
        return f"{m.group(2)}（{m.group(1)}）"
    m = re.search(r"alert alert-danger[^>]*>\s*<strong>([^<]+)</strong>", resp)
    if m:
        return m.group(1).strip()
    m = re.search(r"class=['\"]alert alert-danger['\"][^>]*>(.*?)</div>", resp, re.S)
    if m:
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            return txt[:200]
    return None


def hub_airport_id(hub_name: str) -> str | None:
    """按枢纽名（如 'China, Beijing Capital'）反查机场 CSV ID。"""
    city = hub_name.split(", ", 1)[1] if ", " in hub_name else hub_name
    q = city.strip().lower()
    for a in _load_airports():
        if a["name"].strip().lower() == q:
            return a["id"]
    return None


def haversine_km(a: dict, b: dict) -> float:
    """两机场大圆距离（km），与 AM4Help 同款 haversine。"""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lng"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lng"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _distances_from(origin: dict) -> list[float]:
    """origin 到全部机场的大圆距离（numpy 向量化，无 numpy 时逐条计算）。"""
    _load_airports()
    lat1, lon1 = math.radians(origin["lat"]), math.radians(origin["lng"])
    if _np is not None:
        lats = _np.array(_lats_rad, dtype=_np.float64)
        lons = _np.array(_lons_rad, dtype=_np.float64)
        dlat = lats - lat1
        dlon = lons - lon1
        a = _np.sin(dlat / 2) ** 2 + math.cos(lat1) * _np.cos(lats) * _np.sin(dlon / 2) ** 2
        return (2 * EARTH_RADIUS_KM * _np.arcsin(_np.sqrt(a))).tolist()
    out = []
    for a in _airports_cache:
        out.append(haversine_km(origin, a))
    return out


def _find_stopover(origin: dict, dest: dict, aircraft: dict,
                   o_dists: list[float], game_mode: str = DEFAULT_GAME_MODE) -> tuple[dict, float] | None:
    """查找最优经停：两段距离均在 [100, 航程] 内、跑道足够，总距离最小。

    与 AM4Help Stopover::find_by_efficiency 一致。返回 (经停机场, 总距离)。
    """
    ac_range = float(aircraft.get("range", 0) or 0)
    ac_rwy = int(aircraft.get("rwy", 0) or 0)
    rwy_req = ac_rwy if game_mode == "realism" else 0
    oidx, didx = origin["idx"], dest["idx"]
    aps = _load_airports()

    if _np is not None:
        lats = _np.array(_lats_rad, dtype=_np.float64)
        lons = _np.array(_lons_rad, dtype=_np.float64)
        rwys = _np.array(_rwys, dtype=_np.int64)
        od = _np.array(o_dists, dtype=_np.float64)
        mask = (od >= MIN_DISTANCE_KM) & (od <= ac_range)
        if rwy_req > 0:
            mask &= (rwys >= rwy_req)
        mask[oidx] = False
        mask[didx] = False
        idxs = _np.nonzero(mask)[0]
        if len(idxs) == 0:
            return None
        lat1, lon1 = math.radians(dest["lat"]), math.radians(dest["lng"])
        dlat = lats[idxs] - lat1
        dlon = lons[idxs] - lon1
        a = _np.sin(dlat / 2) ** 2 + math.cos(lat1) * _np.cos(lats[idxs]) * _np.sin(dlon / 2) ** 2
        dd = 2 * EARTH_RADIUS_KM * _np.arcsin(_np.sqrt(a))
        totals = od[idxs] + dd
        valid = (dd >= MIN_DISTANCE_KM) & (dd <= ac_range)
        totals = _np.where(valid, totals, _np.inf)
        k = int(_np.argmin(totals))
        if not _np.isfinite(totals[k]):
            return None
        return aps[int(idxs[k])], float(totals[k])

    best: dict | None = None
    best_total = float("inf")
    for s in aps:
        if s["idx"] == oidx or s["idx"] == didx:
            continue
        if rwy_req and s["rwy"] < rwy_req:
            continue
        os_d = o_dists[s["idx"]]
        if os_d < MIN_DISTANCE_KM or os_d > ac_range:
            continue
        sd = haversine_km(dest, s)
        if sd < MIN_DISTANCE_KM or sd > ac_range:
            continue
        t = os_d + sd
        if t < best_total:
            best, best_total = s, t
    return (best, best_total) if best is not None else None


# ---------------------------------------------------------------- AM4Help 计算引擎

def _rust_round(x: float) -> float:
    """Rust f64::round（远离零取整），用于 CargoDemand 换算。"""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def _optimal_ticket(ac_type: str, d: float, game_mode: str = DEFAULT_GAME_MODE) -> dict:
    """Realism/Easy 最优票价（直接距离 d）。"""
    if game_mode == "easy":
        base = (0.4 * d + 170, 0.8 * d + 560, 1.2 * d + 1200)
        mul = (1.10, 1.08, 1.06)
    else:
        base = (0.3 * d + 150, 0.6 * d + 500, 0.9 * d + 1000)
        mul = (1.10, 1.08, 1.06)
    if ac_type == "2":  # VIP
        vip = 1.7489
        return {
            "economy": int(1.22 * vip * base[0]) - 2,
            "business": int(1.20 * vip * base[1]) - 2,
            "first": int(1.17 * vip * base[2]) - 2,
        }
    return {
        "economy": int(mul[0] * base[0]) - 2,
        "business": int(mul[1] * base[1]) - 2,
        "first": int(mul[2] * base[2]) - 2,
    }


def _optimal_cargo_ticket(d: float, game_mode: str = DEFAULT_GAME_MODE) -> dict:
    """Realism/Easy 货机最优票价（$/kg）。"""
    if game_mode == "easy":
        l = 0.0948283724581252 * d + 85.2045432642377
        h = 0.0689663577640275 * d + 28.2981124272893
    else:
        l = 0.0776321822039374 * d + 85.0567600367807
        h = 0.0517742799409248 * d + 24.6369915396414
    return {
        "l": math.floor(1.10 * l) / 100.0,
        "h": math.floor(1.08 * h) / 100.0,
    }


def _pax_config(dem: tuple[int, int, int], capacity: int, d: float,
                game_mode: str = DEFAULT_GAME_MODE) -> tuple[int, int, int] | None:
    """Pax 贪心舱位算法（Auto），返回 (y, j, f)；需求不足返回 None。"""
    dy, dj, df = dem
    cap = capacity

    def fjy():
        nonlocal cap
        f = min(df, cap // 3)
        cap -= f * 3
        j = min(dj, cap // 2)
        cap -= j * 2
        y = cap
        return (y, j, f) if y < dy else None

    def fyj():
        nonlocal cap
        f = min(df, cap // 3)
        cap -= f * 3
        y = min(dy, cap)
        cap -= y
        j = cap // 2
        return (y, j, f) if j < dj else None

    def jfy():
        nonlocal cap
        j = min(dj, cap // 2)
        cap -= j * 2
        f = min(df, cap // 3)
        cap -= f * 3
        y = cap
        return (y, j, f) if y < dy else None

    def jyf():
        nonlocal cap
        j = min(dj, cap // 2)
        cap -= j * 2
        y = min(dy, cap)
        cap -= y
        f = cap // 3
        return (y, j, f) if f < df else None

    def yfj():
        nonlocal cap
        y = min(dy, cap)
        cap -= y
        f = min(df, cap // 3)
        cap -= f * 3
        j = cap // 2
        return (y, j, f) if j < dj else None

    def yjf():
        nonlocal cap
        y = min(dy, cap)
        cap -= y
        j = min(dj, cap // 2)
        cap -= j * 2
        f = cap // 3
        return (y, j, f) if f < df else None

    if game_mode == "easy":
        if d < 14425.0:
            return fjy()
        if d < 14812.5:
            return fyj()
        if d < 15200.0:
            return yfj()
        return yjf()
    # realism
    if d < 13888.889:
        return fjy()
    if d < 15694.444:
        return jfy()
    if d < 17500.0:
        return jyf()
    return yjf()


def _cargo_config(dem: tuple[int, int, int], capacity: int) -> dict | None:
    """货机 L/H 舱位算法（Auto，训练全关）；需求不足返回 None。"""
    dy, dj, _df = dem
    dem_l = _rust_round(dy / 2.0) * 1000          # kg
    dem_h = dj * 1000                             # kg
    cap = float(capacity)

    l_cap = cap * 0.7
    if dem_l > l_cap:
        return {"l": 100, "h": 0}
    l = dem_l / l_cap
    h = 1.0 - l
    if dem_h < cap * h:
        return None
    lu = int(l * 100.0)
    return {"l": lu, "h": 100 - lu}


def am4_estimate(
    aircraft: dict,
    origin: dict,
    dest: dict,
    tpd: int,
    pax_load: float = DEFAULT_PAX_LOAD,
    cargo_load: float = DEFAULT_CARGO_LOAD,
    fuel_price: float = DEFAULT_FUEL_PRICE,
    co2_price: float = DEFAULT_CO2_PRICE,
    cost_index: int = DEFAULT_COST_INDEX,
    demand: tuple[int, int, int] | None = None,
    direct_distance: float | None = None,
    total_distance: float | None = None,
    stopover: dict | None = None,
    game_mode: str = DEFAULT_GAME_MODE,
) -> dict:
    """按 AM4Help 算法计算单条航线收益（零网络，纯离线）。

    - 直飞距离 ≤ 航程时为直飞；否则需经停（由调用方传入 stopover / total_distance）
    - 飞行时长 = 总距离 / 巡航速度（realism 无加速、easy ×1.5，CI=200 系数为 1.0）
    - 班次超出物理上限或需求不足时 feasible=False 并附原因
    """
    ac_type = str(aircraft.get("type", "0"))
    speed = float(aircraft.get("speed", 0) or 0)
    capacity = int(aircraft.get("capacity", 0) or 0)

    if direct_distance is None:
        d = haversine_km(origin, dest)
    else:
        d = float(direct_distance)
    total = float(total_distance) if total_distance is not None else d
    dist_val = math.ceil(total * 100.0) / 100.0

    speed_mult = SPEED_MULT.get(game_mode, 1.0)
    ci_mult = cost_index / 2000.0 + 0.9
    speed_val = speed * speed_mult * ci_mult
    flight_time = total / speed_val if speed_val > 0 else 0.0

    warnings: list[str] = []
    feasible = True
    reason = ""

    if d > float(aircraft.get("range", 0) or 0) and stopover is None:
        feasible = False
        reason = f"直飞 {d:.0f}km 超过机型航程 {aircraft.get('range', 0)}km，且无可用经停"
        warnings.append(reason)

    max_tpd_phys = math.floor(24.0 / flight_time) if flight_time > 0 else 0
    if feasible and tpd > max_tpd_phys:
        feasible = False
        reason = f"班次 {tpd} 超出物理上限（单程 {flight_time:.2f}h，最多 {max_tpd_phys} 班）"
        warnings.append(reason)

    if demand is None:
        try:
            demand = demand_for(origin, dest)
        except Exception:
            demand = (0, 0, 0)
    dy, dj, df = demand

    # 票价为直飞距离定价（经停不改变票价基准）
    if ac_type == "1":
        ticket = _optimal_cargo_ticket(d, game_mode)
        prices = {
            "economy": round(ticket["l"] * 1000),
            "business": round(ticket["h"] * 1000),
            "first": 0,
        }
    else:
        ticket = _optimal_ticket(ac_type, d, game_mode)
        prices = dict(ticket)

    divisor = tpd * (cargo_load if ac_type == "1" else pax_load)
    dem_trip = (
        math.floor(dy / divisor) if divisor > 0 else 0,
        math.floor(dj / divisor) if divisor > 0 else 0,
        math.floor(df / divisor) if divisor > 0 else 0,
    )

    if feasible:
        if ac_type == "1":
            cfg = _cargo_config(dem_trip, capacity) if divisor > 0 else None
        else:
            cfg = _pax_config(dem_trip, capacity, total, game_mode) if divisor > 0 else None
        if cfg is None:
            feasible = False
            reason = "需求不足：日均需求小于单次航班可承载量"
            warnings.append(reason)
    else:
        cfg = None

    if ac_type == "1" and feasible:
        c = cfg  # {"l": .., "h": ..}
        cap_f = float(capacity)
        l_pct, h_pct = c["l"] / 100.0, c["h"] / 100.0
        revenue = (l_pct * 0.7 * ticket["l"] + h_pct * ticket["h"]) * cap_f * cargo_load
        seats = {"economy": 0, "business": 0, "first": 0, "l": c["l"], "h": c["h"]}
        pax_per_day = {"economy": 0, "business": 0, "first": 0,
                       "l": round(c["l"] / 100.0 * cap_f * cargo_load * tpd),
                       "h": round(c["h"] / 100.0 * cap_f * cargo_load * tpd)}

        mass_term = (l_pct * 0.7 / 1000.0 + h_pct / 500.0) * cargo_load * cap_f
        capacity_term = (l_pct * 0.7 + h_pct) * cap_f
        co2_kg = (dist_val * float(aircraft.get("co2", 0) or 0) * mass_term + capacity_term) * ci_mult
    elif feasible:
        y, j, f = cfg
        revenue = (y * ticket["economy"] + j * ticket["business"] + f * ticket["first"]) * pax_load
        seats = {"economy": y, "business": j, "first": f}
        pax_per_day = {
            "economy": round(y * pax_load * tpd),
            "business": round(j * pax_load * tpd),
            "first": round(f * pax_load * tpd),
        }

        seats_total = y + j + f
        pax_mass = (y + 2 * j + 3 * f) * pax_load
        co2_kg = (dist_val * float(aircraft.get("co2", 0) or 0) * pax_mass + seats_total) * ci_mult
    else:
        revenue = 0.0
        seats = {"economy": 0, "business": 0, "first": 0}
        pax_per_day = {"economy": 0, "business": 0, "first": 0}
        co2_kg = 0.0

    # 燃油 / CO2 / 检修 / 维修（按总距离）
    fuel_lbs = dist_val * float(aircraft.get("fuel", 0) or 0) * (cost_index / 500.0 + 0.6)
    acheck = float(aircraft.get("check_cost", 0) or 0) * ACHECK_MULT.get(game_mode, 1.0) \
        * math.ceil(flight_time * speed_mult) / max(float(aircraft.get("maint", 1) or 1), 1.0)
    repair = float(aircraft.get("cost", 0) or 0) / 1000.0 * 0.0075

    expense = (fuel_lbs * fuel_price / 1000.0
               + co2_kg * co2_price / 1000.0
               + acheck + repair)

    profit_trip = revenue - expense
    profit_day = profit_trip * tpd

    # 联盟贡献（仅展示，按总距离）
    k = 0.0048 if total > 10000.0 else (0.0032 if total > 6000.0 else 0.0064)
    contribution = min(k * total * (3.0 - cost_index / 100.0), 152.0) \
        * CONTRIBUTION_MULT.get(game_mode, 1.0) * 0.875

    creation_cost = 0.4 * (d + capacity * sum(ticket.values()))
    aircraft_cost = int(aircraft.get("cost", 0) or 0)
    initial_investment = aircraft_cost + round(creation_cost)
    payback_days = (round(initial_investment / profit_day, 1)
                    if feasible and profit_day > 0 else None)
    roi_30d_pct = (round(profit_day * 30.0 / initial_investment * 100.0, 1)
                   if feasible and initial_investment > 0 else None)

    return {
        "ok": True,
        "feasible": feasible,
        "reason": reason,
        "game_mode": game_mode,
        "distance_km": round(total, 1),
        "direct_distance_km": round(d, 1),
        "total_distance_km": round(total, 1),
        "stopover": ({"iata": stopover.get("iata", ""), "name": stopover.get("name", ""),
                      "country": stopover.get("country", ""), "id": stopover.get("id", "")}
                     if stopover else None),
        "flight_hours": round(flight_time, 4),
        "max_tpd": max_tpd_phys,
        "demand": {"economy": dy, "business": dj, "first": df},
        "prices": prices,
        "config": seats,
        "pax_per_day": pax_per_day,
        "revenue_per_trip": round(revenue),
        "revenue_per_day": round(revenue * tpd),
        "fuel_lbs_per_flight": round(fuel_lbs),
        "fuel_cost_per_day": round(fuel_lbs * tpd * fuel_price / 1000.0),
        "co2_kg_per_flight": round(co2_kg),
        "co2_cost_per_day": round(co2_kg * tpd * co2_price / 1000.0),
        "acheck_cost_per_day": round(acheck * tpd),
        "repair_cost_per_day": round(repair * tpd),
        "net_per_trip": round(profit_trip),
        "net_per_day": round(profit_day) if feasible else None,
        "contribution": round(contribution, 1),
        "creation_cost": round(creation_cost),
        "aircraft_cost": aircraft_cost,
        "initial_investment": initial_investment,
        "payback_days": payback_days,
        "roi_30d_pct": roi_30d_pct,
        "warnings": warnings,
        "params": {
            "pax_load": pax_load,
            "cargo_load": cargo_load,
            "fuel_price": fuel_price,
            "co2_price": co2_price,
            "cost_index": cost_index,
            "tpd": tpd,
        },
    }


# ---------------------------------------------------------------- 候选与排序

def _flight_speed(aircraft: dict, cost_index: int = DEFAULT_COST_INDEX,
                  game_mode: str = DEFAULT_GAME_MODE) -> float:
    """Realism/Easy 模式下 CI 巡航速度（与 AM4Help schedule 一致）。"""
    u = float(aircraft.get("speed", 0) or 0)
    ci_mult = 0.0035 * cost_index + 0.3 if cost_index != DEFAULT_COST_INDEX else 1.0
    return u * SPEED_MULT.get(game_mode, 1.0) * ci_mult


def candidate_routes(aircraft: dict, origin: dict, tpd: int,
                     cost_index: int = DEFAULT_COST_INDEX,
                     exclude: set[str] | None = None,
                     limit: int = 500,
                     pax_load: float = DEFAULT_PAX_LOAD,
                     cargo_load: float = DEFAULT_CARGO_LOAD,
                     fuel_price: float = DEFAULT_FUEL_PRICE,
                     co2_price: float = DEFAULT_CO2_PRICE,
                     game_mode: str = DEFAULT_GAME_MODE,
                     maximize: bool = False,
                     max_tpd: int = 20) -> list[dict]:
    """按机型+班次+出发自动筛选可飞航线（纯离线，与 AM4Help 一致）。

    规则：
    - 单程总距离 >100km；直飞距离 ≤ 机型航程，超出则自动找最优经停
    - 固定班次：飞行时长（总距离 / 巡航速度）≤ 24/tpd 小时
    - Maximise：每条航线从物理上限向下试，取仍能满载的最高班次（上限 max_tpd）
    - 需求装得满一架（Auto 舱位算法非空）
    排除已运营航线（exclude 为 "出发IATA:到达IATA" 或 "出发ID:到达ID" 集合）。
    """
    ac_range = float(aircraft.get("range", 0) or 0)
    if ac_range <= 0 or (not maximize and tpd <= 0):
        return []
    max_tpd = max(1, int(max_tpd))
    max_hours = 24.0 / tpd if not maximize else 24.0
    speed = _flight_speed(aircraft, cost_index, game_mode)
    if speed <= 0:
        return []
    exclude = exclude or set()
    origin_id = str(origin["id"])
    origin_iata = (origin.get("iata", "") or "").strip() or origin_id
    origin_icao = (origin.get("icao", "") or "").strip() or origin_id
    o_dists = _distances_from(origin)
    out: list[dict] = []
    for a in _load_airports():
        if a["id"] == origin_id:
            continue
        d = o_dists[a["idx"]]
        if d <= MIN_DISTANCE_KM:
            continue
        # realism 模式需检查到达机场跑道长度（AM4Help with_aircraft 同款规则）
        if game_mode == "realism" and int(a.get("rwy", 0) or 0) < int(aircraft.get("rwy", 0) or 0):
            continue
        # 经停只会让总距离更长：直飞已超班次时限则整条航线不可行
        if not maximize and d > max_hours * speed:
            continue
        stopover = None
        total = d
        if d > ac_range:
            res = _find_stopover(origin, a, aircraft, o_dists, game_mode)
            if res is None:
                continue
            stopover, total = res
        hours = total / speed
        if hours > max_hours:
            continue
        d_icao = (a.get("icao", "") or "").strip() or a["id"]
        d_iata = (a.get("iata", "") or "").strip() or a["id"]
        pair_keys = {
            f"{origin_icao}:{d_icao}", f"{d_icao}:{origin_icao}",
            f"{origin_iata}:{d_iata}", f"{d_iata}:{origin_iata}",
            f"{origin_id}:{a['id']}", f"{a['id']}:{origin_id}",
        }
        if exclude & pair_keys:
            continue
        try:
            demand = demand_for(origin, a)
        except Exception:
            demand = (0, 0, 0)
        physical_tpd = min(max_tpd, math.floor(24.0 / hours)) if hours > 0 else 0
        trial_tpds = (range(physical_tpd, 0, -1) if maximize else (tpd,))
        est = None
        route_tpd = 0
        for trial_tpd in trial_tpds:
            trial = am4_estimate(
                aircraft, origin, a, trial_tpd,
                pax_load=pax_load, cargo_load=cargo_load,
                fuel_price=fuel_price, co2_price=co2_price, cost_index=cost_index,
                demand=demand, direct_distance=d, total_distance=total,
                stopover=stopover, game_mode=game_mode,
            )
            if trial["feasible"]:
                est = trial
                route_tpd = trial_tpd
                break
        if est is None:
            continue
        out.append({
            "id": a["id"],
            "idx": a["idx"],
            "name": a["name"],
            "iata": a.get("iata", ""),
            "icao": a.get("icao", ""),
            "country": a.get("country", ""),
            "direct_distance_km": round(d, 1),
            "distance_km": round(total, 1),
            "flight_hours": round(hours, 4),
            "tpd": route_tpd,
            "max_tpd": physical_tpd,
            "stopover": est["stopover"],
            "demand": est["demand"],
            "prices": est["prices"],
            "config": est["config"],
            "revenue_per_trip": est["revenue_per_trip"],
            "revenue_per_day": est["revenue_per_day"],
            "fuel_cost_per_day": est["fuel_cost_per_day"],
            "co2_cost_per_day": est["co2_cost_per_day"],
            "acheck_cost_per_day": est["acheck_cost_per_day"],
            "repair_cost_per_day": est["repair_cost_per_day"],
            "net_per_trip": est["net_per_trip"],
            "net_per_day": est["net_per_day"],
            "profit_per_day": est["net_per_day"],
            "contribution": est["contribution"],
            "creation_cost": est["creation_cost"],
            "aircraft_cost": est["aircraft_cost"],
            "initial_investment": est["initial_investment"],
            "payback_days": est["payback_days"],
            "roi_30d_pct": est["roi_30d_pct"],
        })
    out.sort(key=lambda x: (x.get("net_per_day") is None, -(x.get("net_per_day") or 0)))
    return out[:limit]


def rank_routes(aircraft: dict, origin: dict, tpd: int,
                cost_index: int = DEFAULT_COST_INDEX,
                pax_load: float = DEFAULT_PAX_LOAD,
                fuel_price: float = DEFAULT_FUEL_PRICE,
                co2_price: float = DEFAULT_CO2_PRICE,
                cargo_load: float = DEFAULT_CARGO_LOAD,
                exclude: set[str] | None = None,
                limit: int = 500,
                game_mode: str = DEFAULT_GAME_MODE) -> list[dict]:
    """候选航线按每日净利降序（AM4Help 同款算法，零网络、秒级）。"""
    return candidate_routes(
        aircraft, origin, tpd,
        cost_index=cost_index, exclude=exclude, limit=limit,
        pax_load=pax_load, cargo_load=cargo_load,
        fuel_price=fuel_price, co2_price=co2_price, game_mode=game_mode,
    )


# ---------------------------------------------------------------- 实时抓取精算

def fetch_demand(origin_id: str, dest_id: str) -> dict | None:
    """调用游戏 route_analyze.php 抓取当日剩余需求（Y/J/F）。"""
    _ensure_login()
    try:
        html = _do_curl(
            f"{BASE}/route_analyze.php?dep={origin_id}&arr={dest_id}",
            data=None, output=None, referer=HOME,
        )
    except Exception:
        return None
    if not html.strip():
        return None
    nums = re.findall(r"<td>([\d,]+)</td>", html)
    if len(nums) >= 3:
        return {
            "economy": int(nums[0].replace(",", "")),
            "business": int(nums[1].replace(",", "")),
            "first": int(nums[2].replace(",", "")),
        }
    return None


def estimate_route(
    aircraft: dict,
    tpd: int,
    origin: dict,
    dest: dict,
    pax_load: float = DEFAULT_PAX_LOAD,
    cargo_load: float = DEFAULT_CARGO_LOAD,
    fuel_price: float = DEFAULT_FUEL_PRICE,
    co2_price: float = DEFAULT_CO2_PRICE,
    cost_index: int = DEFAULT_COST_INDEX,
    game_mode: str = DEFAULT_GAME_MODE,
) -> dict:
    """单条航线精算：先抓游戏实时需求，再套用 AM4Help 公式计算。"""
    live = fetch_demand(origin["id"], dest["id"])
    demand = None
    source = "offline"
    if live and live.get("economy") is not None:
        demand = (live["economy"], live["business"], live["first"])
        source = "live"
    d = haversine_km(origin, dest)
    ac_range = float(aircraft.get("range", 0) or 0)
    stopover = None
    total = d
    if d > ac_range:
        o_dists = _distances_from(origin)
        res = _find_stopover(origin, dest, aircraft, o_dists, game_mode)
        if res is not None:
            stopover, total = res
    est = am4_estimate(
        aircraft, origin, dest, tpd,
        pax_load=pax_load, cargo_load=cargo_load,
        fuel_price=fuel_price, co2_price=co2_price, cost_index=cost_index,
        demand=demand, direct_distance=d, total_distance=total,
        stopover=stopover, game_mode=game_mode,
    )
    est["demand_source"] = source
    return est


def estimate_route_local(
    aircraft: dict,
    tpd: int,
    origin: dict,
    dest: dict,
    pax_load: float = DEFAULT_PAX_LOAD,
    fuel_price: float = DEFAULT_FUEL_PRICE,
    co2_price: float = DEFAULT_CO2_PRICE,
    cost_index: int = DEFAULT_COST_INDEX,
    game_mode: str = DEFAULT_GAME_MODE,
) -> dict:
    """纯离线单条估算（离线矩阵 + AM4Help 公式），供 API 兼容调用。"""
    d = haversine_km(origin, dest)
    ac_range = float(aircraft.get("range", 0) or 0)
    stopover = None
    total = d
    if d > ac_range:
        o_dists = _distances_from(origin)
        res = _find_stopover(origin, dest, aircraft, o_dists, game_mode)
        if res is not None:
            stopover, total = res
    return am4_estimate(
        aircraft, origin, dest, tpd,
        pax_load=pax_load, cargo_load=DEFAULT_CARGO_LOAD,
        fuel_price=fuel_price, co2_price=co2_price, cost_index=cost_index,
        direct_distance=d, total_distance=total, stopover=stopover, game_mode=game_mode,
    )


# ---------------------------------------------------------------- 一键建设

def _route_id_from_flight_info(page: str) -> str:
    """从已读取的飞机信息页提取航线 ID，不产生额外请求。"""
    for pattern in (
        r"route_depart\.php\?id=(\d+)",
        r"routeMainList(\d+)",
        r"\brouteId\s*[:=]\s*['\"]?(\d+)",
    ):
        match = re.search(pattern, page or "", re.I)
        if match:
            return match.group(1)
    return ""


def _route_lookup_wait(steps: list[dict], fid: str, message: str) -> dict:
    steps.append({"step": "route", "ok": False, "msg": message})
    return {"ok": True, "steps": steps, "waiting_route_lookup": True,
            "fid": fid, "remain_sec": 300}

def build_route(aircraft: dict, origin_hub_id: str, dest_airport_id: str,
                reg: str, economy: int | None = None, business: int = 0,
                first: int = 0, engine: str | None = None, amount: int = 1,
                cargo_l: int | None = None, cargo_h: int | None = None,
                cost_index: int = DEFAULT_COST_INDEX,
                origin_airport_id: str | None = None,
                retrofit: str | None = None,
                confirmed_fid: str | None = None,
                fleet_absence_confirmed: bool = False,
                delivery_confirmed: bool = False,
                after_spend: Callable[[], None] | None = None) -> dict:
    """一键建设的在线部分：买飞机（带布局）→ 等待到货 → 定航线。

    步骤：
    1. 客机调用 ac_order_do.php；货机调用 ac_order_do_cargo.php（aft/fwd 货舱比例）
    2. 轮询 fleet.php 等飞机交付到货，拿到机队飞机 ID
    3. 抓 new_route_info.php 面板解析航班号；票价使用 AM4Help 最优价（与预估一致）
    4. new_route_info.php?mode=do 确认建线，返回航线 ID
    5. 起飞由 server 侧排入待办任务（takeoff），稍后自动执行 route_depart.php
    6. 建线后的改装与起飞由 server 侧持久化待办接管
    """
    _ensure_login()
    ac_id = aircraft.get("id")
    engine = str(engine) if engine else str(aircraft.get("eid") or "312")
    capacity = int(aircraft.get("capacity", 0))
    is_cargo = str(aircraft.get("type", "0")) == "1"
    # 等效座位：J 占 2 格、F 占 3 格（与游戏/AM4Help 一致）
    eco = max(0, capacity - 2 * business - 3 * first) if economy is None else economy
    if is_cargo:
        if cargo_l is None or cargo_h is None or cargo_l < 0 or cargo_h < 0 or cargo_l + cargo_h != 100:
            return {"ok": False, "steps": [{
                "step": "buy", "ok": False, "msg": "货机舱位必须提供合计 100% 的 L/H 比例",
            }]}

    steps = []
    created_route_id = None

    # 1) 幂等买飞机：机队已有该注册号则跳过下单，避免重复购买
    try:
        fid = (str(confirmed_fid) if confirmed_fid
               else (None if fleet_absence_confirmed
                     else _fleet_aircraft_id(str(ac_id), reg)))
    except FleetLookupError as e:
        steps.append({
            "step": "buy", "ok": False,
            "msg": f"无法确认机队中是否已存在 {reg}：{e}；未发送购机请求",
        })
        return {"ok": True, "steps": steps, "waiting_fleet_lookup": True,
                "fid": "", "remain_sec": 300}
    if fid is None:
        if is_cargo:
            # 游戏把货舱分成前后各 50%；aft/fwd 均表示该半舱中的重货百分点。
            aft = int(cargo_h) // 2
            fwd = int(cargo_h) - aft
            order_url = f"{BASE}/ac_order_do_cargo.php?" + urlencode({
                "engine": engine, "reg": reg, "hub": origin_hub_id,
                "acId": ac_id, "aft": aft, "fwd": fwd,
            })
            layout_msg = f"货舱 L{cargo_l}%/H{cargo_h}%"
        else:
            order_url = f"{BASE}/ac_order_do.php?" + urlencode({
                "id": ac_id, "hub": origin_hub_id, "e": eco, "b": business,
                "f": first, "r": reg, "engine": engine, "amount": amount, "charter": 0,
            })
            layout_msg = f"布局 E{eco}/J{business}/F{first}"
        try:
            order_resp = _do_curl(order_url, data=None, output=None, referer=HOME)
            err = _order_error(order_resp or "")
            if err:
                steps.append({"step": "buy", "ok": False,
                              "msg": f"订单被拒绝：{err}"})
                return {"ok": False, "steps": steps}
            steps.append({"step": "buy", "ok": True,
                          "msg": f"已下单 {aircraft['name']}×{amount}（注册号 {reg}，{layout_msg}，发动机 {engine}）"})
        except Exception as e:
            # 写请求可能已到达游戏服务器，响应丢失时不能贸然再次下单。
            steps.append({
                "step": "buy", "ok": False,
                "msg": f"下单响应未确认：{e}；稍后检查机队避免重复购买",
            })
            return {"ok": True, "steps": steps, "waiting_fleet_lookup": True,
                    "fid": "", "remain_sec": 300}
        if after_spend:
            try:
                after_spend()
            except Exception:
                # 余额回读只是本地缓存维护；不能把它的失败误判为购机响应不明，
                # 更不能因此进入可能重复下单的恢复分支。
                pass
        try:
            fid = _fleet_aircraft_id(str(ac_id), reg)
        except FleetLookupError as e:
            steps.append({
                "step": "deliver", "ok": False,
                "msg": f"下单后暂时无法读取机队：{e}；稍后恢复",
            })
            return {"ok": True, "steps": steps, "waiting_fleet_lookup": True,
                    "fid": "", "remain_sec": 300}
    else:
        steps.append({"step": "aircraft", "ok": True,
                      "msg": f"{reg} 已到货（机队ID {fid}）"})

    if fid is None:
        steps.append({
            "step": "deliver", "ok": False,
            "msg": f"下单后暂未在机队中找到 {reg}（机型ID {ac_id}）；稍后恢复",
        })
        return {"ok": True, "steps": steps, "waiting_fleet_lookup": True,
                "fid": "", "remain_sec": 300}

    # 2) 交付状态：机队列表出现 ≠ 已交付，需查 flight_info.php 的倒计时
    delivered, remain_sec = ((True, 0) if delivery_confirmed else _delivery_status(fid))
    if delivered is None:
        steps.append({
            "step": "deliver", "ok": True,
            "msg": f"暂时无法确认 {reg} 的交付状态，将在 5 分钟后重试",
        })
        return {"ok": True, "steps": steps, "waiting_delivery": True,
                "delivery_unknown": True, "fid": fid, "remain_sec": 300}
    if delivered is False:
        minutes = max(1, round(remain_sec / 60))
        steps.append({
            "step": "deliver", "ok": True,
            "msg": f"{reg} 正在交付，剩余 {minutes} 分钟；\n飞机交付后将会自动建设航线",
        })
        return {"ok": True, "steps": steps, "waiting_delivery": True,
                "fid": fid, "remain_sec": remain_sec}
    steps.append({"step": "deliver", "ok": True, "msg": f"飞机 {reg} 已到货（机队ID {fid}）"})

    # 3) 检查该飞机是否已有航线（避免重复建线）
    try:
        fi = _do_curl(f"{BASE}/flight_info.php?id={fid}", data=None, output=None, referer=HOME)
    except Exception as e:
        return _route_lookup_wait(
            steps, fid, f"无法确认 {reg} 是否已有航线：{e}；未执行建线写操作")
    body = (fi or "").strip()
    if (not body
            or re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
            or re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)):
        return _route_lookup_wait(
            steps, fid, f"无法确认 {reg} 是否已有航线；未执行建线写操作")
    if "建立新航線" not in body:
        route_id = _route_id_from_flight_info(body)
        if not route_id:
            # 恢复路径最多补读一次主页；不翻航线分页。
            try:
                home = _do_curl(HOME, data=None, output=None, referer=HOME)
                route_id = str(parse_status_data(home).get(str(fid), {}).get("航线ID", ""))
            except Exception:
                route_id = ""
        if not route_id:
            return _route_lookup_wait(
                steps, fid, f"飞机 {reg} 已有航线，但暂时无法恢复航线 ID")
        steps.append({"step": "route", "ok": True,
                      "msg": f"飞机 {reg} 已有航线（航线ID {route_id}），跳过重复建线"})
        steps.append({"step": "retrofit", "ok": True,
                      "msg": "已有航线，改装交由待办继续处理" if retrofit else "未要求改装，跳过"})
        return {"ok": True, "steps": steps, "route_id": route_id,
                "existing_route": True, "fid": fid}

    # 4) 定航线：抓面板 → 解析航班号/建议票价 → 调 mode=do 确认端点
    try:
        panel = _do_curl(
            f"{BASE}/new_route_info.php?id={fid}&airportId={dest_airport_id}&ferry=0",
            data=None, output=None, referer=HOME,
        )
        if "$('#newRouteInfo').hide()" in panel or len(panel.strip()) < 100:
            steps.append({
                "step": "route", "ok": False,
                "msg": f"建航线面板为空（到达机场 {dest_airport_id}），无法自动建线，请到游戏手动完成",
            })
            return {"ok": False, "steps": steps}
        route_regs = re.findall(r"id='routeReg'[^>]*value='([^']*)'", panel)
        route_reg = next((r for r in reversed(route_regs) if r and not r.startswith("<=")), None) \
            or f"MA-{fid[-4:]}"
        # 票价：优先用 AM4Help 最优价（与预估一致）；缺出发机场信息时退回面板自动定价
        origin_ap = airport_by_id(origin_airport_id) if origin_airport_id else None
        dest_ap = airport_by_id(dest_airport_id)
        if origin_ap and dest_ap:
            d = haversine_km(origin_ap, dest_ap)
            if is_cargo:
                ticket = _optimal_cargo_ticket(d, DEFAULT_GAME_MODE)
                y, j, f = ticket["l"], ticket["h"], 0
            else:
                ticket = _optimal_ticket(str(aircraft.get("type", "0")), d, DEFAULT_GAME_MODE)
                y, j, f = ticket["economy"], ticket["business"], ticket["first"]
        else:
            price_pattern = (r"ticketPriceSuggest\(([\d.]+),([\d.]+),([\d.]+),"
                             if is_cargo else r"autoPrice\((\d+),(\d+),(\d+),(\d+)\)")
            prices = re.findall(price_pattern, panel)
            if not prices:
                steps.append({"step": "route", "ok": False,
                              "msg": "面板缺少建议票价，无法自动建线，请到游戏手动完成"})
                return {"ok": False, "steps": steps}
            if is_cargo:
                y, j, f = float(prices[0][0]), float(prices[0][1]), 0
            else:
                y, j, f = int(prices[0][0]), int(prices[0][1]), int(prices[0][2])
        fee_m = re.findall(r"建立航線費用</b></div><div[^>]*>\$?\s*([\d,]+)", panel)
        fee = fee_m[0] if fee_m else ""

        do_url = (f"{BASE}/new_route_info.php?mode=do&id={fid}&airportId={dest_airport_id}"
                  f"&reg={route_reg}&e={y}&b={j}&f={f}&endCostIndex={cost_index}"
                  f"&stopoverId=0&ferry=0&intro=0")
        resp = _do_curl(do_url, data=None, output=None, referer=HOME)
        m = re.search(r"addRouteToMap\(\d+,(\d+),", resp)
        if m:
            route_id = m.group(1)
            created_route_id = route_id
            steps.append({
                "step": "route", "ok": True,
                "msg": f"航线已建立（航班号 {route_reg}，航线ID {route_id}"
                       f"{'，建线费 $' + fee if fee else ''}）",
            })
            if after_spend:
                try:
                    after_spend()
                except Exception:
                    # 航线已经由服务端明确创建；余额回读失败不得改变建线结果。
                    pass
            # 建线后改装由 server 侧的 retrofit 待办任务执行，这里只登记提示，
            # 避免与任务重复应用（第二次会被游戏拒绝：請選擇一個新的配置）
            steps.append({"step": "retrofit", "ok": True,
                          "msg": "改装已排入待办任务，稍后自动执行" if retrofit else "无需改装，跳过"})
            steps.append({
                "step": "takeoff", "ok": True,
                "msg": f"起飞已排入待办任务（航线 {route_id}，CI {cost_index}，稍后自动执行）",
            })
        else:
            err = re.search(r"toast\('([^']*)','([^']*)','error'\)", resp)
            steps.append({
                "step": "route", "ok": False,
                "msg": f"建线被拒：{err.group(2) if err else '未知原因'}"
                       f"{'' if err else '（' + resp.strip()[:120] + '）'}",
            })
            return {"ok": False, "steps": steps}
    except Exception as e:
        steps.append({"step": "route", "ok": False, "msg": f"建航线失败: {e}"})
        return {"ok": False, "steps": steps}

    return {"ok": True, "steps": steps, "route_id": created_route_id, "fid": str(fid)}


class FleetLookupError(RuntimeError):
    """机队存在性无法确认；调用方不得把它当成飞机不存在。"""


def _fleet_aircraft_id(model_id: str, reg: str) -> str | None:
    """查机队列表，返回注册号对应的机队飞机 ID（列表出现即返回，可能仍在交付）。"""
    try:
        page_html = _do_curl(f"{BASE}/fleet.php?type={model_id}", data=None, output=None,
                             referer=HOME)
    except Exception as e:
        raise FleetLookupError(str(e)) from e
    body = (page_html or "").strip()
    if (not body
            or re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
            or re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)):
        raise FleetLookupError("机队页面为空、登录失效或游戏服务异常")
    if not re.search(
            rf"fleet_ground\.php\?mode=all&amp;type={re.escape(str(model_id))}|"
            rf"fleet_ground\.php\?mode=all&type={re.escape(str(model_id))}", body, re.I):
        raise FleetLookupError("机队分组页面结构无法确认")
    for fid, name in re.findall(
            r"fleet_details\.php\?id=(\d+)&returnType=\d+[^>]*>([^<]+)</a>", body):
        if html.unescape(name).strip().lower() == str(reg).strip().lower():
            return fid
    return None


def _delivery_status(fid: str) -> tuple[bool | None, int]:
    """查询交付状态：True 已交付、False 交付中、None 查询失败。"""
    try:
        html = _do_curl(f"{BASE}/flight_info.php?id={fid}", data=None, output=None,
                        referer=HOME)
    except Exception:
        return None, 0
    body = (html or "").strip()
    if (not body
            or re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
            or re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)):
        return None, 0
    if not re.search(r"等待交付|交貨時間剩下|現在送達", body):
        return True, 0
    m = re.search(r"until:\s*(\d+)", body)
    return False, int(m.group(1)) if m else 0


def takeoff_route(route_id: str, cost_index: int = DEFAULT_COST_INDEX) -> str:
    """请求航线起飞，返回游戏响应文本。"""
    _ensure_login()
    return _do_curl(
        f"{BASE}/route_depart.php?id={route_id}&ref=list&costIndex={cost_index}",
        data=None, output=None, referer=HOME,
    )


def fetch_aircraft_fleet_row(fid: str, hub_name: str = "", reg: str = "") -> dict | None:
    """抓取飞机详情并转成机队行（建线+改装完成后回填真实数据）。

    复用采集脚本的 parse_aircraft_detail；失败返回 None（由调用方保留旧数据）。
    """
    try:
        from collector import parse_aircraft_detail
        html = _do_curl(f"{BASE}/fleet_details.php?id={fid}", data=None, output=None,
                        referer=HOME)
        if not html.strip():
            return None
        d = parse_aircraft_detail(html)
        # 航距：由起降机场 ICAO 查经纬度算大圆距离
        dist = ""
        oa = next((a for a in _load_airports()
                   if a.get("icao", "").upper() == str(d["origin_code"]).upper()), None)
        da = next((a for a in _load_airports()
                   if a.get("icao", "").upper() == str(d["destination_code"]).upper()), None)
        if oa and da:
            dist = str(round(haversine_km(oa, da)))
        ac = aircraft_by_name(d.get("aircraft_model", "")) if d.get("aircraft_model") else None
        is_cargo = bool(ac and str(ac.get("type", "0")) == "1")
        return {
            "飞机ID": fid,
            "注册号": (d.get("fleet_reg") or reg or "").upper(),
            "航班号": d.get("route_reg", ""),
            "机型": d.get("aircraft_model", ""),
            "经济舱座位": d.get("seat_economy", ""),
            "商务舱座位": d.get("seat_business", ""),
            "头等舱座位": d.get("seat_first", ""),
            "经济舱票价": d.get("ticket_economy", ""),
            "商务舱票价": d.get("ticket_business", ""),
            "头等舱票价": d.get("ticket_first", ""),
            "起飞机场代码": d.get("origin_code", ""),
            "起飞机场名称": d.get("origin_name", ""),
            "到达机场代码": d.get("destination_code", ""),
            "到达机场名称": d.get("destination_name", ""),
            "起飞时间UTC": d.get("departure_utc", ""),
            "到达时间UTC": d.get("arrival_utc", ""),
            "飞行时长": d.get("flight_duration", ""),
            "航距km": dist,
            "枢纽分类": hub_name,
            "距A-Check小时": "",
            "损坏率%": "",
            "CO2减排放": "未查询",
            "飞行速度增加": "未查询",
            "耗油量减少": "未查询",
            "经济舱需求": d.get("demand_economy", ""),
            "商务舱需求": d.get("demand_business", ""),
            "头等舱需求": d.get("demand_first", ""),
            "大货需求": d.get("cargo_large_demand", ""),
            "重货需求": d.get("cargo_heavy_demand", ""),
            "大货容量": d.get("cargo_large_cap", ""),
            "重货容量": d.get("cargo_heavy_cap", ""),
            "需求状态": d.get("demand_status", ""),
            "组类型": str(ac.get("id", "")) if ac else "",
            "客机组数量": "0" if is_cargo else "1",
            "最后更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        return None


def _retrofit_completed_mods(page: str) -> set[str]:
    completed = set()
    for mod_id, key in (("mod1", "co2"), ("mod2", "speed"), ("mod3", "fuel")):
        if re.search(
                rf"<input type=\"checkbox\" class='mod-check' id='{mod_id}'\s*disabled\s*checked",
                page or ""):
            completed.add(key)
    return completed


def _valid_retrofit_page(page: str) -> bool:
    body = (page or "").strip()
    return bool(
        body
        and not re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
        and not re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)
        and re.search(r"class='mod-check'\s+id='mod[123]'", body)
    )


def _confirm_retrofit(fid: str, mods: set[str]) -> dict:
    """只读确认已提交改装的完成状态；绝不再次发送改装写请求。"""
    try:
        page = _do_curl(f"{BASE}/maint_plan_do.php?type=modify&id={fid}",
                        data=None, output=None, referer=HOME)
    except Exception as e:
        return {"ok": False, "retryable": True,
                "msg": f"确认页抓取失败：{e}"}
    if not _valid_retrofit_page(page):
        return {"ok": False, "retryable": True,
                "msg": "确认页暂不可用或飞机仍在安装中"}
    completed = _retrofit_completed_mods(page)
    if not mods.issubset(completed):
        return {"ok": False, "retryable": True,
                "msg": "改装尚未全部完成"}
    name_map = {"co2": "CO₂减排放", "speed": "飞行速度增加", "fuel": "耗油量减少"}
    return {"ok": True, "msg": "改装已完成（" +
            "、".join(name_map[item] for item in ("co2", "speed", "fuel")
                     if item in mods) + "）"}


def _apply_retrofit(fid: str, economy: int, business: int, first: int,
                    mods: set[str], cargo_l: int | None = None,
                    cargo_h: int | None = None) -> dict:
    """给飞机应用改装（co2=减排放 / speed=提速 / fuel=省油）。

    先读改装页确认已完成项，再调 mode=do&type=modify 应用未完成项。
    """
    name_map = {"co2": "CO₂减排放", "speed": "飞行速度增加", "fuel": "耗油量减少"}
    try:
        page = _do_curl(f"{BASE}/maint_plan_do.php?type=modify&id={fid}",
                        data=None, output=None, referer=HOME)
    except Exception as e:
        return {"ok": False, "retryable": True, "msg": f"改装页抓取失败: {e}"}
    if not _valid_retrofit_page(page):
        return {"ok": False, "retryable": True,
                "msg": "改装页为空、登录失效或页面结构无法确认"}

    # 客机/货机使用不同的 modType（货机为 cargo）
    mt = re.search(r"modType=(\w+)", page)
    mod_type = mt.group(1) if mt else "pax"

    completed = _retrofit_completed_mods(page)

    to_apply = [m for m in ("co2", "speed", "fuel") if m in mods and m not in completed]
    if not to_apply:
        already = "、".join(name_map[m] for m in mods if m in completed)
        return {"ok": True, "msg": f"改装已完成，无需重复（{already or '无待改装项'}）"}

    # 改装安装时间：改装页 modXtime = N*1.5（如 800*1.5=1200s/项）；
    # 点击处理里若为 &&1==0 则当前活动免时/即时
    install_secs = 0
    gate = re.search(r"&&\s*1==([01])", page)
    if gate and gate.group(1) == "1":
        mod_no = {"co2": "1", "speed": "2", "fuel": "3"}
        for m in to_apply:
            mm = re.search(rf"mod{mod_no[m]}time\s*=\s*([\d.]+)\s*\*\s*1\.5", page)
            if mm:
                install_secs += float(mm.group(1)) * 1.5

    m1 = 1 if "co2" in to_apply else 0
    m2 = 1 if "speed" in to_apply else 0
    m3 = 1 if "fuel" in to_apply else 0
    params = {"mode": "do", "modType": mod_type, "id": fid, "type": "modify",
              "mod1": m1, "mod2": m2, "mod3": m3}
    if mod_type == "cargo":
        max_m = re.search(r"var\s+maxLoad\s*=\s*(\d+)", page)
        if not max_m:
            return {"ok": False, "retryable": True,
                    "msg": "货机改装页缺少最大载量，未提交改装"}
        capacity = int(max_m.group(1))
        if cargo_l is None or cargo_h is None or cargo_l < 0 or cargo_h < 0 or cargo_l + cargo_h != 100:
            return {"ok": False, "retryable": False,
                    "msg": "货机改装缺少有效的 L/H 货舱比例"}
        heavy = round(capacity * cargo_h / 100.0)
        params.update({"large": capacity - heavy, "heavy": heavy})
    else:
        params.update({"eSeat": economy, "bSeat": business, "fSeat": first})
    url = f"{BASE}/maint_plan_do.php?" + urlencode(params)
    try:
        resp = _do_curl(url, data=None, output=None, referer=HOME)
    except Exception as e:
        return {"ok": False, "retryable": True, "msg": f"改装请求失败: {e}"}
    err = re.search(r"toast\('([^']*)','([^']*)','error'\)", resp or "")
    if err:
        return {"ok": False, "retryable": False,
                "msg": f"改装被拒：{err.group(2)}（{err.group(1)}）"}
    # 写请求只发送一次；随后只读确认。响应丢失或返回异常页面时，下次重试会先
    # 读取完成项，因此不会重复应用已经成功的改装。
    confirmation = resp if _valid_retrofit_page(resp) else ""
    if not confirmation:
        try:
            confirmation = _do_curl(
                f"{BASE}/maint_plan_do.php?type=modify&id={fid}",
                data=None, output=None, referer=HOME)
        except Exception as e:
            return {"ok": False, "retryable": True,
                    "submitted": True, "install_secs": install_secs,
                    "msg": f"改装已提交但确认页抓取失败: {e}"}
    if not _valid_retrofit_page(confirmation):
        return {"ok": False, "retryable": True,
                "submitted": True, "install_secs": install_secs,
                "msg": "改装已提交但确认页暂不可用，等待安装完成后核对"}
    if not mods.issubset(_retrofit_completed_mods(confirmation)):
        return {"ok": False, "retryable": True,
                "submitted": True, "install_secs": install_secs,
                "msg": "改装已提交但尚未确认全部完成，等待安装完成后核对"}
    return {"ok": True, "msg": "已应用改装：" + "、".join(name_map[m] for m in to_apply),
            "install_secs": install_secs}
