"""Airline Manager 4 综合采集与循环调度器。

提取：枢纽 → 机队（航线/起降/时长/座位/票价） → 改装状态 → 检修
输出：fleet.csv、maintenance_checks.csv

用法：
  python collector.py               # 单次运行（延时 5~10s）
  python collector.py --light       # 调试：只运行一次轻量采集
  python collector.py --loop         # 循环模式（轻量 00/30，低价收尾补仓 29/59）
  python collector.py --loop --interval 1800  # 每 30 分钟循环
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from account_storage import account_key, account_output_dir
from storage_utils import exclusive_file_lock

# Windows 下重定向 stdout 默认用 GBK，emoji/中文可能抛 UnicodeEncodeError；
# 强制 UTF-8 + 替换符，避免账号切换等场景打印警告时崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BJT = timezone(timedelta(hours=8), name="Asia/Shanghai")  # 北京时间（UTC+8，无夏令时）


def _now_bjt() -> datetime:
    """当前北京时间（显式 UTC+8，不依赖机器时区）。"""
    return datetime.now(BJT)


def _format_elapsed(seconds: float) -> str:
    """把单轮采集耗时格式化为紧凑、可读的中文。"""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


BASE = "https://www.airlinemanager.com"
HOME = f"{BASE}/?gameType=web"
LOGIN = f"{BASE}/weblogin/login.php"
FLEET_MAIN = f"{BASE}/fleet.php"
MAINT_MAIN = f"{BASE}/maintenance_main.php"
MAINT_PLAN = f"{BASE}/maint_plan.php"
HUBS = f"{BASE}/hubs.php"
FUEL = f"{BASE}/fuel.php?m=nonav"
CO2 = f"{BASE}/co2.php?nav=1"

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_ROOT = Path(os.environ.get("AM4_OUTPUTS_DIR", str(ROOT / "outputs")))


def _load_env():
    """从项目根 .env 读取环境变量（仅当未显式设置时）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

# 凭据只从环境变量 / .env 读取，不再内置默认凭据（避免泄露进 git 历史）
EMAIL = os.environ.get("AM4_EMAIL", "")
PASSWORD = os.environ.get("AM4_PASSWORD", "")
if not EMAIL or not PASSWORD:
    raise SystemExit(
        "缺少 AM4 账号凭据：请设置环境变量 AM4_EMAIL / AM4_PASSWORD，"
        "或在项目根目录创建 .env 文件（参考 .env.example）。"
    )

# 燃油低于该值时跳过需求刷新与自动起飞（可通过 .env 的 AM4_MIN_FUEL 配置，默认 200000 Lbs）
MIN_FUEL_FOR_TAKEOFF = int(os.environ.get("AM4_MIN_FUEL", "200000"))

# 操作开关（面板「设置」页按账号覆盖；采集进程读取环境变量）
AUTO_MARKETING = os.environ.get("AM4_AUTO_MARKETING", "1") == "1"
AUTO_BUY_FUEL = os.environ.get("AM4_AUTO_BUY_FUEL", "1") == "1"
AUTO_BUY_CO2 = os.environ.get("AM4_AUTO_BUY_CO2", "1") == "1"
AUTO_TAKEOFF = os.environ.get("AM4_AUTO_TAKEOFF", "1") == "1"


# 数据输出按账号隔离：换账号后各自的数据互不覆盖，随时可切回
OUT = account_output_dir(OUTPUTS_ROOT, EMAIL, migrate_legacy=False)

# 并发多账号：登录会话 Cookie 按账号隔离，互不覆盖
_cookie_key = account_key(EMAIL)
# 跨平台临时目录：Linux 下 TEMP 常未设置，不能用 Windows 路径兜底
_TMP_ROOT = Path(tempfile.gettempdir())
COOKIE_JAR = _TMP_ROOT / f"am4_cookiejar_{_cookie_key}.txt"
# 记录当前 cookie 会话归属的 .env 账号，用于检测账号配置变更
ACCOUNT_MARKER = COOKIE_JAR.with_name(f"am4_cookiejar_{_cookie_key}_account.txt")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Windows 自带可执行文件名为 curl.exe，Linux 通常为 curl。
# 部署时按 PATH 自动选择，避免把采集器绑定到单一操作系统。
CURL_BIN = shutil.which("curl.exe") or shutil.which("curl") or "curl"

# 无控制台父进程（如 systemd/隐藏启动的服务）在 Windows 上拉起 curl.exe 时，
# 会为每个子进程新建可见 CMD 窗口；加 CREATE_NO_WINDOW 消除弹窗。
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 飞机结束飞行、维护或改装后，等待游戏状态完成切换再尝试起飞。
TAKEOFF_READY_BUFFER_SECONDS = 120
DAILY_FULL_HOUR_BJT = 6       # 游戏需求日切对应北京时间 06:00

_LEGACY_FLEET_CSV = OUT / "fleet_complete.csv"
FLEET_CSV = OUT / "fleet.csv"
if not FLEET_CSV.exists() and _LEGACY_FLEET_CSV.exists():
    try:
        _LEGACY_FLEET_CSV.replace(FLEET_CSV)
    except OSError:
        FLEET_CSV = _LEGACY_FLEET_CSV
MAINT_CSV = OUT / "maintenance_checks.csv"
MARKET_JSON = OUT / "market_data.json"
HUB_LIST_JSON = OUT / "hub_list.json"
SCHEDULE_STATE_JSON = OUT / "schedule_state.json"

MARKETING = f"{BASE}/marketing.php?nav=1"
MARKETING_NEW = f"{BASE}/marketing_new.php"

# 双模式延时：单次 5~10s，循环 10~120s
_DELAY_LOOP = (2.0, 4.0)
_DELAY_ONCE = (2.0, 4.0)
_current_delay = _DELAY_ONCE

CSV_FIELDNAMES = [
    "飞机ID", "注册号", "航班号", "机型",
    "建设状态",
    "经济舱座位", "商务舱座位", "头等舱座位",
    "经济舱票价", "商务舱票价", "头等舱票价",
    "起飞机场代码", "起飞机场名称",
    "到达机场代码", "到达机场名称",
    "起飞时间UTC", "到达时间UTC", "飞行时长",
    "航距km",
    "枢纽分类",
    "CO2减排放", "飞行速度增加", "耗油量减少",
    "经济舱需求", "商务舱需求", "头等舱需求",
    "大货需求", "重货需求", "大货容量", "重货容量",
    "需求状态",
    "距A-Check小时", "损坏率%",
    "组类型", "客机组数量",
    "最后更新时间",
]

MAINT_FIELDNAMES = ["检修ID", "检修类型", "飞机注册号", "费用", "最后更新时间"]


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def _do_curl(url: str, data: str | None, output: Path | None, referer: str) -> str:
    """执行单次 curl，不处理延时和重试。

    curl 退出码 28 = 操作超时；加 --connect-timeout / --max-time 避免无限等待。
    """
    cmd = [
        CURL_BIN, "-L", "-s",
        "--connect-timeout", "20", "--max-time", "45",
        "-b", str(COOKIE_JAR), "-c", str(COOKIE_JAR),
        "-H", f"User-Agent: {_USER_AGENT}",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if data is not None:
        cmd += ["-X", "POST", "--data", data]

    if output is not None:
        cmd += ["-o", str(output), url]
        subprocess.run(cmd, check=True, capture_output=True,
                       creationflags=_SUBPROCESS_FLAGS)
        return output.read_text(encoding="utf-8", errors="replace")
    else:
        tf = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
        tf.close()
        tmp_path = Path(tf.name)
        cmd += ["-o", str(tmp_path), url]
        try:
            subprocess.run(cmd, check=True, capture_output=True,
                           creationflags=_SUBPROCESS_FLAGS)
        except subprocess.CalledProcessError:
            tmp_path.unlink(missing_ok=True)
            raise
        result = tmp_path.read_text(encoding="utf-8", errors="replace")
        tmp_path.unlink(missing_ok=True)
        return result


def _relogin():
    """重新登录并确认会话确实恢复。"""
    try:
        _do_curl(HOME, data=None, output=None, referer="")
        _do_curl(LOGIN, data=_login_payload(EMAIL, PASSWORD),
                 output=None, referer=HOME)
        verified = _do_curl(HOME, data=None, output=None, referer="")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("AM4 重新登录请求失败") from exc
    if "headerAccount" not in verified:
        raise RuntimeError("AM4 重新登录未成功，请检查账号、密码或登录页面是否变化")
    _mark_account()


def _is_logged_in() -> bool:
    """检测本地 cookie 是否仍为有效登录态。

    抓主页判断是否存在登录后特征（余额 headerAccount）。
    只抓一次主页，不触发登录；失败/未登录均返回 False。
    """
    try:
        html = _do_curl(HOME, data=None, output=None, referer="")
        return "headerAccount" in html
    except subprocess.CalledProcessError:
        return False


def _account_changed() -> bool:
    """本地会话记录的上次账号是否与 .env 当前配置不一致。

    有 cookie 但无账号标记（旧版本升级场景）时保守返回 True，
    强制重新登录一次以写入标记。
    """
    try:
        if ACCOUNT_MARKER.exists():
            return ACCOUNT_MARKER.read_text(encoding="utf-8-sig").strip() != EMAIL
    except Exception:
        pass
    return COOKIE_JAR.exists()


def _mark_account() -> None:
    """记录当前会话归属的 .env 账号，供下次启动检测切换。"""
    try:
        ACCOUNT_MARKER.write_text(EMAIL, encoding="utf-8")
    except Exception:
        pass


def _login_payload(email: str, password: str) -> str:
    """按 application/x-www-form-urlencoded 编码登录表单。"""
    return urlencode({"lEmail": email, "lPass": password, "fbSig": "null"})


def classify_takeoff_response(response: str) -> str:
    """分类起飞接口响应：accepted / no_fuel / not_ready / rejected / unknown。"""
    body = (response or "").strip()
    if not body:
        return "unknown"
    if re.search(r"燃油不足|沒有足夠燃油|不够燃油|沒有燃油|沒有油|燃油耗尽|"
                 r"not enough fuel|insufficient fuel|no fuel", body, re.I):
        return "no_fuel"
    if re.search(r"不能起飛|沒有航線剩下出發|不能起飞|没有航线剩下出发", body):
        return "not_ready"
    if re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I):
        return "unknown"
    if re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout|Just a moment)", body, re.I):
        return "unknown"
    if re.search(r"toast\([^)]*['\"]error['\"]", body, re.I):
        return "rejected"
    if re.search(r"<(?:!doctype|html|body|form)\b", body, re.I):
        return "unknown"
    # 游戏接口正常返回的是短 AJAX/JavaScript 片段；完整页面已在上面拒绝。
    return "accepted"


def _ensure_login() -> bool:
    """确保本地会话属于 .env 配置的账号；账号变更时自动清除旧 cookie 重新登录。"""
    if _account_changed():
        print("⚠ 已更新账号数据", flush=True)
        try:
            COOKIE_JAR.unlink(missing_ok=True)
        except Exception:
            pass
    if _is_logged_in():
        print("Cookie 有效，跳过登录", flush=True)
    else:
        _do_curl(HOME, data=None, output=None, referer="")
        payload = _login_payload(EMAIL, PASSWORD)
        _do_curl(LOGIN, data=payload, output=None, referer=HOME)
        # 登录接口即使拒绝凭据也可能返回 HTTP 200；必须重新读主页验证，
        # 不能把“请求已发送”当作“登录成功”。
        verified = _do_curl(HOME, data=None, output=None, referer="")
        if "headerAccount" not in verified:
            raise RuntimeError("AM4 登录未成功，请检查账号、密码或登录页面是否变化")
        print("Cookie 已刷新，登录验证成功", flush=True)
    _mark_account()
    return True


def fetch(url: str, referer: str = "", label: str = "") -> str:
    """带延时、重试和进度输出的 HTTP GET。

    超时/网络错误最多重试 3 次（每次退避 5~15s），依旧失败则跳过该请求
    返回空串并告警，不让单架飞机超时拖垮整个采集。
    """
    lo, hi = _current_delay
    time.sleep(random.uniform(lo, hi))

    result = ""
    last_err = None
    for attempt in range(3):
        try:
            result = _do_curl(url, data=None, output=None, referer=referer)
            if not result and attempt < 2:
                # 空响应也重试
                time.sleep(random.uniform(5, 10))
                continue
            break
        except subprocess.CalledProcessError as e:
            last_err = e
            print(f"⚠ 请求失败({getattr(e, 'returncode', '?')})，将在重新登录后重试 [{attempt+1}/3]...", flush=True)
            _relogin()
            time.sleep(random.uniform(5, 15))
    else:
        # 3 次尝试全部失败：跳过该请求，不中断整个采集
        print(f"⚠ 多次请求失败: {url} err={last_err}", flush=True)
        return ""

    if label:
        print(label, flush=True)
    return result


# ---------------------------------------------------------------------------
# 1. 枢纽
# ---------------------------------------------------------------------------

def parse_hubs(html_text: str) -> list[dict]:
    hubs = []
    for m in re.finditer(
        r"<div class='row mt-1 opa rounded'[^>]*"
        r"onClick=\"[^\"]*Ajax\('hub_details\.php\?id=(\d+)','hubDetail'[^\"]*\"[^>]*>"
        r"(.*?)</div>\s*</div>\s*</div>\s*</div>",
        html_text, re.S,
    ):
        name_match = re.search(r"<b>([^<]+)</b>", m.group(2))
        name = html.unescape(name_match.group(1)).strip() if name_match else ""
        is_base = "hubBase" in m.group(2) and "基地" in m.group(2)
        hubs.append({"hub_id": m.group(1), "name": name, "is_base": is_base})
    return hubs


# ---------------------------------------------------------------------------
# 2. 机队
# ---------------------------------------------------------------------------

def parse_fleet_types(html_text: str):
    pattern = re.compile(
        r"fleetConcatList' data-pax='(?P<pax>\d+)' data-cargo='(?P<cargo>\d+)' data-charter='(?P<charter>\d+)' "
        r"onClick=\"[^\"]*?Ajax\('fleet\.php\?type=(?P<type>\d+)','fleetDetailList'\);\"",
        re.S,
    )
    return [
        {"type_id": m.group("type"), "pax": int(m.group("pax")),
         "cargo": int(m.group("cargo")), "charter": int(m.group("charter"))}
        for m in pattern.finditer(html_text)
    ]


def parse_fleet_entries(type_html: str):
    rows = []
    for m in re.finditer(
        r"Ajax\('fleet_details\.php\?id=(\d+)&returnType=(\d+)','detailsAction'\);\">([^<]+)</a>",
        type_html, re.S,
    ):
        prefix = type_html[max(0, m.start() - 250):m.start()]
        model_match = re.search(r"<span class='s-text'>([^<]+)</span><br>$", prefix)
        if not model_match:
            model_match = re.search(r"<span class='s-text'>([^<]+)</span><br><a href='#' onClick=.*?$", prefix, re.S)
        if not model_match:
            continue
        rows.append({
            "aircraft_id": m.group(1), "return_type": m.group(2),
            "reg": html.unescape(m.group(3)).strip(),
            "model": html.unescape(model_match.group(1)).strip(),
        })
    return rows


def parse_aircraft_detail(detail_html: str):
    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, detail_html, re.S)
        return html.unescape(m.group(1)).strip() if m else default

    seat_match = re.search(
        r"economy_seat\.png[^<]*<br>\s*(\d+).*?business_seat\.png[^<]*<br>\s*(\d+).*?first_seat\.png[^<]*<br>\s*(\d+)",
        detail_html, re.S,
    )
    ticket_match = re.search(
        r"value='(\d+)'\s+id='eTicket'.*?value='(\d+)'\s+id='bTicket'.*?value='(\d+)'\s+id='fTicket'",
        detail_html, re.S,
    )
    demand_match = re.search(
        r"id='list-demand'[\s\S]*?economy_seat\.png[^<]*<br>\s*(\d+)<span class='s-text'>/(\d+)</span>"
        r"[\s\S]*?business_seat\.png[^<]*<br>\s*(\d+)<span class='s-text'>/(\d+)</span>"
        r"[\s\S]*?first_seat\.png[^<]*<br>\s*(\d+)<span class='s-text'>/(\d+)</span>",
        detail_html, re.S,
    )
    demand_status = ""
    if demand_match:
        # 需求 vs 布局座位：所有有座舱位需求都 > 座位*80% 才判"旺盛"
        seats = [seat_match.group(1) if seat_match else "0",
                 seat_match.group(2) if seat_match else "0",
                 seat_match.group(3) if seat_match else "0"]
        above = 0
        total = 0
        for i in range(3):
            d = _clean_num(demand_match.group(i * 2 + 1))
            s = _clean_num(seats[i])
            if s > 0:
                total += 1
                if d > s * 0.8:
                    above += 1
        if total and above == total:
            demand_status = "旺盛"
        elif total:
            demand_status = "不足"

    # ---- 货机：两种货物（Large 大货 / Heavy 重货）----
    cargo_large_demand = cargo_heavy_demand = ""
    cargo_large_cap = cargo_heavy_cap = ""
    if "Large load" in detail_html:
        cm = re.search(
            r"Large load</div><span[^>]*></span><br>\s*([\d,]+)\s*Lbs"
            r"[\s\S]*?Heavy load</div><span[^>]*></span><br>\s*([\d,]+)\s*Lbs",
            detail_html, re.S,
        )
        if cm:
            cargo_large_cap = cm.group(1).strip()
            cargo_heavy_cap = cm.group(2).strip()
        dm = re.search(
            r"id='list-demand'[\s\S]*?glyphicons-cargo text-warning[^>]*></span><br>\s*([\d,]+)<span class='s-text'>/([\d,]+)</span>"
            r"[\s\S]*?glyphicons-cargo text-danger[^>]*></span><br>\s*([\d,]+)<span class='s-text'>/([\d,]+)</span>",
            detail_html, re.S,
        )
        if dm:
            cargo_large_demand = dm.group(1).strip()
            cargo_heavy_demand = dm.group(3).strip()
        if not demand_match:
            # 货机需求状态：两种货物都 > 容量*80% 才判"旺盛"
            above = 0
            total = 0
            for d, s in [(cargo_large_demand, cargo_large_cap),
                         (cargo_heavy_demand, cargo_heavy_cap)]:
                s_num = _clean_num(s)
                if s_num > 0:
                    total += 1
                    if _clean_num(d) > s_num * 0.8:
                        above += 1
            if total and above == total:
                demand_status = "旺盛"
            elif total:
                demand_status = "不足"
    flight_io = re.search(
        r"<div class='col-5 bg-light p-2'>\s*<span class='l-text exo'>([^<]+)</span><br>\s*<span class='s-text'>([^<]+)</span>.*?"
        r"<div class='col-5 bg-light p-2'>\s*<span class='l-text exo'>([^<]+)</span><br>\s*<span class='s-text'>([^<]+)</span>.*?"
        r"<div class='col-6 bg-white border s-text'>\s*([0-9:]+\s*UTC)\s*</div>\s*<div class='col-6 bg-white border s-text'>\s*([0-9:]+\s*UTC)\s*</div>",
        detail_html, re.S,
    )
    flight_duration = ""
    if flight_io:
        try:
            dep = datetime.strptime(flight_io.group(5).strip(), "%H:%M:%S UTC")
            arr = datetime.strptime(flight_io.group(6).strip(), "%H:%M:%S UTC")
            if arr < dep:
                arr += timedelta(days=1)
            sec = int((arr - dep).total_seconds())
            flight_duration = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
        except ValueError:
            pass

    return {
        "route_reg": grab(r"id='rr-name'>([^<]+)<"),
        "fleet_reg": grab(r"id='ff-name'>([^<]+)<"),
        "aircraft_model": grab(r"<span class='m-text'>([^<]+)</span>"),
        "origin_code": flight_io.group(1) if flight_io else "",
        "origin_name": flight_io.group(2) if flight_io else "",
        "destination_code": flight_io.group(3) if flight_io else "",
        "destination_name": flight_io.group(4) if flight_io else "",
        "departure_utc": flight_io.group(5).strip() if flight_io else "",
        "arrival_utc": flight_io.group(6).strip() if flight_io else "",
        "flight_duration": flight_duration,
        "seat_economy": seat_match.group(1) if seat_match else "",
        "seat_business": seat_match.group(2) if seat_match else "",
        "seat_first": seat_match.group(3) if seat_match else "",
        "ticket_economy": ticket_match.group(1) if ticket_match else "",
        "ticket_business": ticket_match.group(2) if ticket_match else "",
        "ticket_first": ticket_match.group(3) if ticket_match else "",
        "demand_economy": demand_match.group(1) if demand_match else "",
        "demand_business": demand_match.group(3) if demand_match else "",
        "demand_first": demand_match.group(5) if demand_match else "",
        "demand_status": demand_status,
        "cargo_large_demand": cargo_large_demand,
        "cargo_heavy_demand": cargo_heavy_demand,
        "cargo_large_cap": cargo_large_cap,
        "cargo_heavy_cap": cargo_heavy_cap,
    }


# ---------------------------------------------------------------------------
# 2.5 航距解析（来自主页 flightData）
# ---------------------------------------------------------------------------

def parse_flight_distances(home_html: str) -> dict[str, str]:
    """从主页 flightData 中提取 飞机ID -> distance(km)。"""
    dist_map = {}
    for m in re.finditer(
        r"flightData\.push\(\{\s*id:\s*(\d+),\s*lat1:[\d.\-]+,\s*lon1:[\d.\-]+,\s*stopLat:\d+,\s*stopLon:\d+,\s*lat2:[\d.\-]+,\s*lon2:[\d.\-]+,\s*pctPerSec:[0-9.]+,\s*pct:[0-9.]+,\s*distance:(\d+)",
        home_html, re.S,
    ):
        dist_map[m.group(1)] = m.group(2)
    return dist_map


def classify_hub_with(hubs: list[dict], origin: str, dest: str) -> str:
    """判断航线属于哪个枢纽（起点或终点城市匹配枢纽）。"""
    for h in hubs:
        parts = h["name"].split(", ")
        if len(parts) >= 2:
            city = parts[1].strip()
            if city.lower() in origin.lower() or city.lower() in dest.lower():
                return h["name"]
    return "其他"


# ---------------------------------------------------------------------------
# 2.6 检修预警（来自主页 statusData：距A-Check小时 + 损坏率）
# ---------------------------------------------------------------------------

def parse_status_data(home_html: str) -> dict[str, dict]:
    """从主页 statusData 提取飞机身份、检修和当前飞行状态。

    页面结构示例：
        statusData[16230745] = {
                reg: 'MC-21-4-25',
                icon: 9,
                routeId: 24248538,
                hoursToCheck: 45,
                cargo: 0,
                wear: 42.32,
                ...
        };
    """
    result = {}
    for m in re.finditer(r"statusData\[(\d+)\]\s*=\s*\{(.*?)\};", home_html, re.S):
        block = m.group(2)

        def field(name: str, quoted: bool = False) -> str:
            pattern = (rf"\b{name}:\s*'([^']*)'" if quoted
                       else rf"\b{name}:\s*([\d.]+)")
            found = re.search(pattern, block)
            return found.group(1) if found else ""

        reg = field("reg", quoted=True)
        hours = field("hoursToCheck")
        wear = field("wear")
        # 维护/改装中的飞机有时不提供 hoursToCheck 或 wear；仍保留该状态，
        # 否则会漏掉 maintEnd 并无法在完成后自动接管。
        if not reg:
            continue
        result[m.group(1)] = {
            "注册号": html.unescape(reg).strip().upper(),
            "图标": field("icon"),
            "距A-Check小时": hours,
            "损坏率%": wear,
            "航线ID": field("routeId"),
            "维护改装结束时间戳": field("maintEnd"),
            "预计落地时间戳": field("arrived"),
            "剩余飞行秒数": field("end"),
            "停飞": field("grounded"),
        }
    return result


def _maintenance_takeovers(status_map: dict[str, dict], now: float | None = None) -> list[dict]:
    """从主页识别仍在维护/改装中的飞机，生成完成后的接管计划。"""
    now = time.time() if now is None else now
    plans = []
    for fid, status in status_map.items():
        try:
            maint_end = float(status.get("维护改装结束时间戳", 0) or 0)
            arrived = float(status.get("预计落地时间戳", 0) or 0)
        except (TypeError, ValueError):
            continue
        route_id = str(status.get("航线ID", "") or "")
        reg = str(status.get("注册号", "") or "")
        if not route_id or not reg or maint_end <= now + 60:
            continue
        ready_at = max(maint_end, arrived)
        plans.append({
            "fid": str(fid),
            "reg": reg,
            "route_id": route_id,
            "ready_at": ready_at,
            "trigger_at": ready_at + TAKEOFF_READY_BUFFER_SECONDS,
            "cost_index": int(os.environ.get("AM4_COST_INDEX", "200")),
            "reason": "返场结束",
        })
    return plans


def _emit_maintenance_takeovers(status_map: dict[str, dict]) -> None:
    """把主页发现的维护/改装倒计时交给服务端去重；不产生额外请求。"""
    for plan in _maintenance_takeovers(status_map):
        print(f"__TAKEOVER_TAKEOFF__{json.dumps(plan, ensure_ascii=False)}", flush=True)


# ---------------------------------------------------------------------------
# 3. 改装状态
# ---------------------------------------------------------------------------

def parse_modify_page(html_text: str) -> dict | None:
    if not html_text.strip():
        # 请求失败/空响应：无法判断改装状态，返回 None（上层保留旧值/标记无法查询）
        return None
    if not re.search(r"\bid=['\"]typeModify['\"]", html_text, re.I):
        # 页面结构变化/未找到改装区：同样视为无法查询
        return None
    if "飛機不在樞紐" in html_text:
        return None

    result = {"mod1_completed": False, "mod2_completed": False, "mod3_completed": False}
    for mod_id in ["mod1", "mod2", "mod3"]:
        tag = re.search(
            rf"<input\b[^>]*\bid=['\"]{mod_id}['\"][^>]*>", html_text, re.I,
        )
        if tag:
            input_html = tag.group(0)
            result[f"{mod_id}_completed"] = bool(
                re.search(r"\bdisabled(?:\s|=|>)", input_html, re.I)
                and re.search(r"\bchecked(?:\s|=|>)", input_html, re.I)
            )
    return result


# ---------------------------------------------------------------------------
# 营销页面解析与购买（调度由 server 的持久待办负责）
# ---------------------------------------------------------------------------


def _marketing_response_error(response: str) -> str:
    """从购买响应中提取常见失败提示；空串表示未发现明确失败。"""
    if not response:
        return "服务器未返回内容"
    text = html.unescape(re.sub(r"<[^>]+>", " ", response))
    text = re.sub(r"\s+", " ", text).strip()
    markers = (
        "not enough", "insufficient", "failed", "error",
        "沒有足夠", "不足", "失敗", "錯誤", "错误",
    )
    lower = text.lower()
    if any(marker in lower for marker in markers):
        return text[:240] or "购买失败"
    return ""


def _parse_active_marketing(page: str) -> dict[str, int]:
    """解析正在进行的航空声誉/环保营销及剩余秒数。"""
    active: dict[str, int] = {}
    timers = {
        timer_id: int(seconds)
        for timer_id, seconds in re.findall(r"timer\('([^']+)',\s*(\d+)\)", page or "")
    }
    for row in re.findall(r"<tr>(.*?)</tr>", page or "", re.S | re.I):
        timer_match = re.search(r"id=['\"]([^'\"]+timer)['\"]", row, re.I)
        if not timer_match:
            continue
        seconds = timers.get(timer_match.group(1), 0)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", row))
        if "glyphicons-leaf" in row or "環保" in plain or "环保" in plain:
            active["eco"] = max(active.get("eco", 0), seconds)
        elif "glyphicons-star" in row or "航空聲譽" in plain or "航空声誉" in plain:
            active["airline"] = max(active.get("airline", 0), seconds)
    return active


def _valid_marketing_page(page: str) -> bool:
    """只接受可识别的营销页，避免把空页、登录页或网关错误当成“活动已到期”。"""
    body = (page or "").strip()
    if (not body
            or re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
            or re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)):
        return False
    return bool(re.search(
        r"(?:marketing_new\.php|glyphicons-(?:star|leaf)|航空[聲声]譽|環保|环保|marketing)",
        body, re.I,
    ))


def _reconcile_removed_aircraft(existing: dict, live_ids: set[str], expected_total: int) -> list[dict]:
    """仅在官网清单数量完整匹配时删除本地幽灵飞机，并返回被删行。"""
    if expected_total <= 0 or len(live_ids) != expected_total:
        return []
    removed = []
    for aircraft_id in sorted(set(existing) - live_ids):
        row = existing.get(aircraft_id) or {}
        # B-注册号 是购机流程在取得官网飞机 ID 前写入的本地占位键，并不属于
        # 官网机队清单。建设中的真实行也可能短暂处于交付/改装页面，二者都不能
        # 被“完整清单”反向证明为已经售出。
        if (str(aircraft_id).startswith("B-")
                or str(row.get("建设状态", "")).strip()):
            continue
        row = existing.pop(aircraft_id, None)
        if row:
            removed.append(row)
    return removed


def _purchase_marketing(state_key: str, label: str,
                        known_inactive: bool = False) -> tuple[bool, str, int]:
    """发送一次营销购买请求。写请求不自动重试，避免重复扣款。"""
    if state_key == "ad4_24h":
        url = f"{MARKETING_NEW}?type=1&c=4&mode=do&d=6"
        active_key = "airline"
    elif state_key in {"eco_00", "eco_12", "eco_12h"}:
        url = f"{MARKETING_NEW}?type=5&mode=do&c=1"
        active_key = "eco"
    else:
        return False, f"未知营销动作: {state_key}", 0

    try:
        _ensure_login()
        if not known_inactive:
            before_page = fetch(MARKETING, referer=HOME)
            if not _valid_marketing_page(before_page):
                return False, f"{label}页面暂时无法确认，未执行购买", 0
            remaining = _parse_active_marketing(before_page).get(active_key, 0)
            if remaining > 0:
                hours, rem = divmod(remaining, 3600)
                minutes = rem // 60
                return False, f"{label}仍在进行（剩余 {hours}小时{minutes}分钟）", remaining
        lo, hi = _current_delay
        time.sleep(random.uniform(lo, hi))
        response = _do_curl(url, data=None, output=None, referer=MARKETING)
    except subprocess.CalledProcessError as exc:
        return False, f"请求失败（curl {exc.returncode}）", 0
    except Exception as exc:
        return False, str(exc), 0

    # 写请求只发一次；同一活动在游戏侧不可重复购买，因此响应丢失时
    # 直接由任务层退避后重试即可——重试会先读营销页：已生效则按到期
    # 时间调度，未生效才重新购买，天然幂等。
    after_page = fetch(MARKETING, referer=HOME)
    renewed = (_parse_active_marketing(after_page).get(active_key, 0)
               if _valid_marketing_page(after_page) else 0)
    if renewed > 0:
        return True, "购买成功", renewed
    error = _marketing_response_error(response)
    if error:
        return False, error, 0
    return False, "购买请求已发送，结果暂未确认", 0


def fetch_all_mods(mod_ids_at_base: dict[str, str]) -> dict[str, dict | None]:
    """查询在基地飞机的改装状态。

    参数为 mod_id -> 注册号 的映射（注册号缺失时为空串），
    进度日志中显示注册号；返回仍以 mod_id 为键，
    下游 mod_map.get(mod_id) 无需改动。
    """
    mod_map = {}
    total = len(mod_ids_at_base)
    for i, (ac_id, reg) in enumerate(mod_ids_at_base.items(), 1):
        html_text = fetch(
            f"{BASE}/maint_plan_do.php?type=modify&id={ac_id}",
            referer=MAINT_PLAN,
            label=f"已获取 {reg} 的改装信息 [{i}/{total}]",
        )
        mod_map[ac_id] = parse_modify_page(html_text)
    return mod_map


# ---------------------------------------------------------------------------
# 4. 检修
# ---------------------------------------------------------------------------

def parse_maintenance_ids(html_text: str):
    return sorted(set(re.findall(r"maintDetails\((\d+)\)", html_text)))


def parse_maintenance_detail(html_text: str):
    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, html_text, re.S)
        return html.unescape(m.group(1)).strip() if m else default
    return {
        "check_type": grab(r"<b>(A-Check|B-Check|C-Check|D-Check)</b>"),
        "aircraft_reg": grab(r"<div class='text-secondary m-text'>([^<]+)</div>"),
        "cost": grab(r"<div class='text-danger'>\$ ([0-9,]+)</div>"),
    }


# ---------------------------------------------------------------------------
# 增量合并
# ---------------------------------------------------------------------------

def load_existing_csv(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return {}
    pk = "飞机ID" if "飞机ID" in rows[0] else ("检修ID" if "检修ID" in rows[0] else None)
    if pk is None:
        return {}
    return {r[pk]: r for r in rows}


def _atomic_write_csv(path: Path, fieldnames: list[str], rows: list[dict],
                      *, already_locked: bool = False) -> bool:
    """原子写 CSV：先写临时文件再 os.replace，避免写一半崩溃损坏原文件。"""

    def write_locked() -> bool:
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", newline="", encoding="utf-8-sig", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
            return True
        except Exception as e:
            print(f"⚠ CSV 写入失败: {e}", flush=True)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    if already_locked:
        return write_locked()
    with exclusive_file_lock(path):
        return write_locked()


def save_current_csv(path: Path, fieldnames: list[str], rows: list[dict],
                     pk: str, now_ts: str) -> int:
    """保存当前快照；不保留本次来源中已经消失的历史记录。"""
    current: dict[str, dict] = {}
    for row in rows:
        key = row.get(pk, "")
        if not key:
            continue
        row["最后更新时间"] = now_ts
        current[key] = row
    _atomic_write_csv(path, fieldnames, list(current.values()))
    return len(current)


def _write_fleet_snapshot(existing: dict[str, dict], hubs: list[dict],
                          extra: list[dict] | None = None) -> None:
    """增量写机队 CSV（原子写）：运行中每采到一架就落盘一次，
    让服务器/前端始终以 CSV 为唯一数据源，运行中即可读到最新数据。"""
    with exclusive_file_lock(FLEET_CSV):
        # 服务端待办可能刚刷新过某架飞机；以磁盘最新整表为底，只覆盖本轮新详情。
        rows = load_existing_csv(FLEET_CSV) or dict(existing)
        for r in (extra or []):
            aircraft_id = r["飞机ID"]
            current_key = aircraft_id if aircraft_id in rows else None
            if current_key is None and r.get("注册号"):
                current_key = next((key for key, item in rows.items()
                                    if str(item.get("注册号", "")).strip().upper()
                                    == str(r.get("注册号", "")).strip().upper()), None)
            current = rows.get(current_key) if current_key is not None else None
            if current and str(current.get("建设状态", "")).strip():
                # 新购飞机在建线/改装期间，详情页常暂时显示“其他 / 00:00:00”。
                # 保留建设记录已经写入的航线字段，只接收不会破坏建设上下文的状态。
                for key in ("注册号", "距A-Check小时", "损坏率%"):
                    if str(r.get(key, "")).strip():
                        current[key] = r[key]
                # 官网首次列出新购飞机后，把 B-注册号 占位键原地升级为真实 ID；
                # 不能另建一行，否则本轮末尾会把占位行误判为已售出。
                if (current_key != aircraft_id
                        and str(current.get("飞机ID", "")).startswith("B-")):
                    rows.pop(current_key, None)
                    current["飞机ID"] = aircraft_id
                    rows[aircraft_id] = current
                continue
            # 全量轮次的 now_ts 固定在轮次开始；若服务端待办稍后刷新了同一架飞机，
            # 保留时间戳更晚的单机详情，避免后续增量快照把它覆盖回旧需求。
            if not current or _row_updated_at(r) >= _row_updated_at(current):
                rows[aircraft_id] = r
        hub_order = {h["name"]: i for i, h in enumerate(hubs)}

        def sk(r: dict):
            hub = r.get("枢纽分类", "其他")
            return (0 if hub in hub_order else 1, hub_order.get(hub, 999), hub,
                    r.get("注册号", "").strip().upper())

        _atomic_write_csv(
            FLEET_CSV, CSV_FIELDNAMES, sorted(rows.values(), key=sk), already_locked=True)


def _row_updated_at(row: dict) -> str:
    """返回可按字典序比较的标准更新时间；缺失或旧格式视为最早。"""
    value = str((row or {}).get("最后更新时间", "")).strip()
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value) else ""


def _collapse_build_placeholders(rows: list[dict]) -> list[dict]:
    """同注册号已有真实官网 ID 时，删除遗留的 B-注册号建设占位行。"""
    real_regs = {
        str(row.get("注册号", "")).strip().upper()
        for row in rows
        if row.get("注册号") and not str(row.get("飞机ID", "")).startswith("B-")
    }
    return [
        row for row in rows
        if not (str(row.get("飞机ID", "")).startswith("B-")
                and str(row.get("注册号", "")).strip().upper() in real_regs)
    ]


def _write_full_fleet_snapshot(rows: list[dict]) -> bool:
    """写入完整清单，同时保留本轮开始后由服务端刷新得更晚的同机记录。"""
    with exclusive_file_lock(FLEET_CSV):
        latest = load_existing_csv(FLEET_CSV)
        merged = []
        for row in rows:
            current = latest.get(str(row.get("飞机ID", "")))
            if current is None and row.get("注册号"):
                current = next((item for item in latest.values()
                                if str(item.get("注册号", "")).strip().upper()
                                == str(row.get("注册号", "")).strip().upper()), None)
            if current and str(current.get("建设状态", "")).strip():
                combined = dict(current)
                # 按注册号命中 B-占位行时，以本轮官网返回的真实 ID 完成身份升级。
                if (str(combined.get("飞机ID", "")).startswith("B-")
                        and row.get("飞机ID")):
                    combined["飞机ID"] = row["飞机ID"]
                for key in ("注册号", "距A-Check小时", "损坏率%",
                            "CO2减排放", "飞行速度增加", "耗油量减少"):
                    if str(row.get(key, "")).strip():
                        combined[key] = row[key]
                merged.append(combined)
                continue
            if current and _row_updated_at(current) > _row_updated_at(row):
                # 单机详情中的航线/需求更新更晚；全量轮次独有的主页检修和改装
                # 结果仍应提交，二者按字段职责合并而不是整行二选一。
                combined = dict(current)
                for key in ("距A-Check小时", "损坏率%",
                            "CO2减排放", "飞行速度增加", "耗油量减少"):
                    if str(row.get(key, "")).strip():
                        combined[key] = row[key]
                merged.append(combined)
            else:
                merged.append(row)
        return _atomic_write_csv(
            FLEET_CSV, CSV_FIELDNAMES, _collapse_build_placeholders(merged),
            already_locked=True)


def _write_light_fleet_snapshot(existing: dict[str, dict], new_rows: list[dict],
                                removed_rows: list[dict], status_map: dict[str, dict],
                                hubs: list[dict]) -> None:
    """轻量轮次锁内合并：只提交改名、检修、新机和确认删除，不回写旧详情。"""
    with exclusive_file_lock(FLEET_CSV):
        rows = load_existing_csv(FLEET_CSV) or dict(existing)
        for aircraft_id, source in existing.items():
            current = rows.get(aircraft_id)
            if not current:
                continue
            if source.get("注册号"):
                current["注册号"] = source["注册号"]
            status = status_map.get(aircraft_id, {})
            for key in ("距A-Check小时", "损坏率%"):
                if key in status:
                    current[key] = status.get(key, "")
        for row in new_rows:
            aircraft_id = row["飞机ID"]
            current_key = aircraft_id if aircraft_id in rows else None
            if current_key is None and row.get("注册号"):
                current_key = next((key for key, item in rows.items()
                                    if str(item.get("注册号", "")).strip().upper()
                                    == str(row.get("注册号", "")).strip().upper()), None)
            current = rows.get(current_key) if current_key is not None else None
            if current and str(current.get("建设状态", "")).strip():
                for key in ("注册号", "距A-Check小时", "损坏率%"):
                    if str(row.get(key, "")).strip():
                        current[key] = row[key]
                if (current_key != aircraft_id
                        and str(current.get("飞机ID", "")).startswith("B-")):
                    rows.pop(current_key, None)
                    current["飞机ID"] = aircraft_id
                    rows[aircraft_id] = current
            else:
                rows[aircraft_id] = row
        for row in removed_rows:
            rows.pop(str(row.get("飞机ID", "")), None)
        hub_order = {h["name"]: i for i, h in enumerate(hubs)}

        def sk(row: dict):
            hub = row.get("枢纽分类", "其他")
            return (0 if hub in hub_order else 1, hub_order.get(hub, 999), hub,
                    row.get("注册号", "").strip().upper())

        _atomic_write_csv(
            FLEET_CSV, CSV_FIELDNAMES, sorted(rows.values(), key=sk), already_locked=True)


# ---------------------------------------------------------------------------
# 5. 市场数据（余额/燃油价格/CO2价格）
# ---------------------------------------------------------------------------

def _clean_num(s: str) -> float:
    """去逗号转 float。"""
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return 0.0


def parse_market_data(home_html: str, fuel_html: str, co2_html: str) -> dict:
    """从主页、燃油和 CO2 页面提取余额、燃油库存/价格、CO2 价格。"""
    balance = ""
    fuel_qty = ""
    fuel_price = ""
    co2_price = ""

    m = re.search(r"id='headerAccount'>([^<]+)<", home_html)
    if m:
        balance = m.group(1).strip()
    m = re.search(r"id='headerFuel'>([^<]+)<", home_html)
    if m:
        fuel_qty = m.group(1).strip()

    # fuel.php: "現在價格： ... $ 1,770"（每 1000 磅）
    m = re.search(r"現在價格：</span><br><span class='text-danger'><b>\$\s*([\d,]+)</b>", fuel_html)
    if m:
        fuel_price = str(int(_clean_num(m.group(1))))

    # co2.php: "每CO2配額價格 ... $ 174"（每 1000 配额）
    m = re.search(r"每CO2配額價格</span><br><span class='text-danger'><b>\$\s*([\d,]+)</b>", co2_html)
    if m:
        co2_price = str(int(_clean_num(m.group(1))))

    # co2.php: "您現有 <span id='holding'>-1,876,250</span> CO2配額"
    co2_qty = ""
    m = re.search(r"id='holding'[^>]*>([^<]+)</span>\s*CO2配額", co2_html)
    if m:
        co2_qty = m.group(1).strip()

    return {
        "balance": balance,
        "fuel_qty": fuel_qty,
        "fuel_price": fuel_price,
        "co2_price": co2_price,
        "co2_qty": co2_qty,
        "updated_at": _now_bjt().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _market_valid(data: dict) -> bool:
    """市场数据有效性：至少解析出 余额 或 燃油价格 任一关键字段。"""
    return bool(data.get("balance") or data.get("fuel_price"))


def save_market_data(data: dict):
    """写入 market_data.json。

    若本次解析为空（如请求失败返回空响应），不覆盖磁盘上已有的正常数据。
    """
    if not _market_valid(data):
        print("⚠ 市场数据解析失败，将保留旧数据", flush=True)
        return
    try:
        MARKET_JSON.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"⚠ 市场数据保存失败: {e}", flush=True)


def apply_purchase_to_market(market: dict, purchased: dict) -> bool:
    """把已确认买入量立即合并进市场快照并推送，零额外网络请求。"""
    fuel = int(purchased.get("fuel", 0) or 0)
    co2 = int(purchased.get("co2", 0) or 0)
    if fuel <= 0 and co2 <= 0:
        return False

    if fuel > 0:
        market["fuel_qty"] = f"{int(_clean_num(str(market.get('fuel_qty', '0')))) + fuel:,}"
    if co2 > 0:
        market["co2_qty"] = f"{int(_clean_num(str(market.get('co2_qty', '0')))) + co2:,}"

    balance = _clean_num(str(market.get("balance", "0")))
    spent = (
        fuel * _clean_num(str(market.get("fuel_price", "0"))) / 1000.0
        + co2 * _clean_num(str(market.get("co2_price", "0"))) / 1000.0
    )
    if balance > 0:
        market["balance"] = f"{max(0, int(round(balance - spent))):,}"
    market["updated_at"] = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
    save_market_data(market)
    print(f"__MARKET__{json.dumps(market, ensure_ascii=False)}", flush=True)
    return True


def _build_maint_warnings(status_map: dict, now_ts_fmt: str) -> dict:
    """从主页 statusData 生成检修预警（A-Check临近 + 高损坏率），全量/轻量共用。"""
    def _fmt_num(x: float) -> str:
        return f"{x:g}" if x == int(x) else f"{x:.1f}"

    _a_soon = sorted(
        ((v["注册号"], float(v["距A-Check小时"]), float(v["损坏率%"])) for v in status_map.values()
         if v["距A-Check小时"] and _clean_num(v["距A-Check小时"]) < 50),
        key=lambda x: x[1],
    )
    _high_wear = sorted(
        ((v["注册号"], float(v["距A-Check小时"]), float(v["损坏率%"])) for v in status_map.values()
         if v["损坏率%"] and _clean_num(v["损坏率%"]) > 60),
        key=lambda x: x[2], reverse=True,
    )
    _warnings: list[dict] = []
    _seen_regs = set()
    for reg, hrs, wear in _a_soon:
        _seen_regs.add(reg)
        _warnings.append({
            "注册号": reg, "距A-Check小时": _fmt_num(hrs),
            "损坏率%": _fmt_num(wear), "status": "需A-Check",
        })
    for reg, hrs, wear in _high_wear:
        if reg not in _seen_regs:
            _warnings.append({
                "注册号": reg, "距A-Check小时": _fmt_num(hrs),
                "损坏率%": _fmt_num(wear), "status": "高损坏",
            })
    return {"warnings": _warnings, "count": len(_warnings), "updated_at": now_ts_fmt}


# ---------------------------------------------------------------------------
# 全量循环（BJT 06:00/首次启动）：登录、主页、市场、枢纽、检修预警、改装状态、机队快照
# ---------------------------------------------------------------------------

def run_once(takeoff: bool = False):
    started_mono = time.monotonic()
    now_ts = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*30}")
    print(f"全量采集: {now_ts}")
    print(f"{'='*30}")

    print("=== 登录 ===", flush=True)
    _ensure_login()
    print("===========", flush=True)

    # ---- 0. 检修预警 ----
    print("\n=== 飞机维护检查 ===", flush=True)
    home_html = fetch(HOME, label="刷新主页")
    dist_map = parse_flight_distances(home_html)
    print(f"现有 {len(dist_map)} 架飞机正在飞行")
    status_map = parse_status_data(home_html)
    print(f"已对 {len(status_map)} 架飞机进行维修检查", flush=True)
    _emit_maintenance_takeovers(status_map)
    # 检修预警区（距A-Check<50h 升序、损坏率>60% 降序）
    now_ts_fmt = _now_bjt().strftime("%H:%M:%S")
    _a_soon = sorted(
        ((v["注册号"], float(v["距A-Check小时"]), float(v["损坏率%"])) for v in status_map.values()
         if v["距A-Check小时"] and _clean_num(v["距A-Check小时"]) < 50),
        key=lambda x: x[1],
    )
    if _a_soon:
        for reg, hrs, wear in _a_soon:
            print(f"⚠ {reg} 需在 {hrs:g} h 后进行 A 级检查", flush=True)
    else:
        print("暂无飞机需要进行 A 级检查", flush=True)
    _high_wear = sorted(
        ((v["注册号"], float(v["距A-Check小时"]), float(v["损坏率%"])) for v in status_map.values()
         if v["损坏率%"] and _clean_num(v["损坏率%"]) > 60),
        key=lambda x: x[2], reverse=True,
    )
    if _high_wear:
        for reg, hrs, wear in _high_wear:
            print(f"⚠ {reg} 损坏率已达 {wear:g}%，注意进行维修", flush=True)
    else:
        print("暂无损坏率大于 60% 的飞机", flush=True)
    print("=== 预警结束 ===", flush=True)

    # 输出检修预警 JSON 行（A-Check 优先、高损坏率次之），让 server 实时推送到检修页
    _maint_req = _build_maint_warnings(status_map, now_ts_fmt)
    print(f"__MAINT__{json.dumps(_maint_req, ensure_ascii=False)}", flush=True)
    # 服务端起飞待办复用本次主页检修状态，避免紧接着再抓一次主页。
    print(f"__STATUS__{json.dumps(status_map, ensure_ascii=False)}", flush=True)

    # --- 1. 市场数据 ---
    print("\n=== 获取市场数据 ===", flush=True)

    fuel_html = fetch(FUEL, referer=HOME, label="正在获取燃油价格")
    co2_html = fetch(CO2, referer=HOME, label="正在获取 CO₂ 价格")
    market = parse_market_data(home_html, fuel_html, co2_html)
    save_market_data(market)
    if market["balance"]:
        print(f"当前余额: ${market['balance']}", flush=True)
        print(f"CO₂配额: {market['co2_qty']};\n燃油库存: {market['fuel_qty']} Lbs;", flush=True)
        print(f"CO₂价格: ${market['co2_price']}/1000;\n燃油价格: ${market['fuel_price']}/1000 Lbs;", flush=True)
    else:
        print("⚠ 市场数据解析失败", flush=True)
    # 全量刷新同样推送市场数据，让前端 market-bar 即时更新（与轻量循环一致）
    print(f"__MARKET__{json.dumps(market, ensure_ascii=False)}", flush=True)

    # 启动/全量刷新也立即检查补货；复用刚抓的页面，零额外请求。
    try:
        from auto_buy import auto_buy
        purchased = auto_buy(fuel_html, co2_html, market.get("balance"),
                             buy_fuel=AUTO_BUY_FUEL, buy_co2=AUTO_BUY_CO2)
        apply_purchase_to_market(market, purchased)
    except Exception as e:
        print(f"⚠ 自动补货异常: {e}", flush=True)

    # ---- 2. 枢纽 ----
    print("=== 获取枢纽列表 ===", flush=True)
    hubs = parse_hubs(fetch(HUBS, label="正在获取枢纽信息"))
    try:
        HUB_LIST_JSON.write_text(
            json.dumps(hubs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    print(f"__HUBS__{json.dumps([h['name'] for h in hubs], ensure_ascii=False)}", flush=True)
    print(f"当前共有 {len(hubs)} 个枢纽")
    for h in hubs:
        print(f" {h['name']} {'★' if h['is_base'] else ''}")

    # ---- 3. 机队 ----
    print("\n=== 获取机队信息 ===", flush=True)
    existing_fleet = load_existing_csv(FLEET_CSV)
    fleet_main_html = fetch(FLEET_MAIN, label="正在获取机队信息")
    # 客机 + 货机 + 包机全部纳入统计（如 B757-2F-1 货机分组 pax=0/cargo=1）
    fleet_types = parse_fleet_types(fleet_main_html)
    total_aircraft = sum(t["pax"] + t["cargo"] + t["charter"] for t in fleet_types)
    print(f"机型分组: {len(fleet_types)} 类, 共 {total_aircraft} 架")

    fleet_rows = []
    detail_count = 0
    live_aircraft_ids: set[str] = set()

    for group in fleet_types:
        type_html = fetch(
            f"{BASE}/fleet.php?type={group['type_id']}", referer=FLEET_MAIN,
            label=f"正在查询分组 {group['type_id']}",
        )
        entries = parse_fleet_entries(type_html)
        for entry in entries:
            detail_count += 1
            ac_id = entry["aircraft_id"]
            live_aircraft_ids.add(ac_id)
            reg = entry["reg"]
            detail_html = fetch(
                f"{BASE}/fleet_details.php?id={ac_id}&returnType={entry['return_type']}",
                referer=f"{BASE}/fleet.php?type={group['type_id']}",
                label=f"正在查询 {reg} [{detail_count}/{total_aircraft}]",
            )
            if not detail_html.strip():
                print(f"⚠ {reg} 信息获取失败，跳过", flush=True)
                continue
            detail = parse_aircraft_detail(detail_html)
            hub = classify_hub_with(hubs, detail["origin_name"], detail["destination_name"])
            old_row = existing_fleet.get(ac_id, {})
            row = {
                    "飞机ID": ac_id,
                    "注册号": (detail["fleet_reg"] or entry["reg"]).upper(),
                    "航班号": detail["route_reg"] or entry["reg"],
                    "机型": detail["aircraft_model"] or entry["model"],
                    "经济舱座位": detail["seat_economy"],
                    "商务舱座位": detail["seat_business"],
                    "头等舱座位": detail["seat_first"],
                    "经济舱票价": detail["ticket_economy"],
                    "商务舱票价": detail["ticket_business"],
                    "头等舱票价": detail["ticket_first"],
                    "起飞机场代码": detail["origin_code"],
                    "起飞机场名称": detail["origin_name"],
                    "到达机场代码": detail["destination_code"],
                    "到达机场名称": detail["destination_name"],
                    "起飞时间UTC": detail["departure_utc"],
                    "到达时间UTC": detail["arrival_utc"],
                    "飞行时长": detail["flight_duration"],
                    "航距km": dist_map.get(ac_id, ""),
                    "枢纽分类": hub,
                    "距A-Check小时": status_map.get(ac_id, {}).get("距A-Check小时", ""),
                    "损坏率%": status_map.get(ac_id, {}).get("损坏率%", ""),
                    "CO2减排放": old_row.get("CO2减排放", "未查询"),
                    "飞行速度增加": old_row.get("飞行速度增加", "未查询"),
                    "耗油量减少": old_row.get("耗油量减少", "未查询"),
                    "经济舱需求": detail["demand_economy"],
                    "商务舱需求": detail["demand_business"],
                    "头等舱需求": detail["demand_first"],
                    "大货需求": detail["cargo_large_demand"],
                    "重货需求": detail["cargo_heavy_demand"],
                    "大货容量": detail["cargo_large_cap"],
                    "重货容量": detail["cargo_heavy_cap"],
                    "需求状态": detail["demand_status"],
                    "组类型": group["type_id"],
                    "客机组数量": str(group["pax"]),
                    "最后更新时间": now_ts,
                }
            fleet_rows.append(row)
            # 增量落盘：即使中途停止/刷新，前端也能读到已采集部分
            _write_fleet_snapshot(existing_fleet, hubs, fleet_rows)
            # 文件先落盘再通知前端，确保随后的机队总数刷新能读到本架飞机。
            print(f"__AIRCRAFT__{json.dumps(row, ensure_ascii=False)}", flush=True)

    print(f"已更新 {len(fleet_rows)} 架 (总共 {total_aircraft} 架)")

    # ---- 4. 改装状态 ----
    print("\n=== 获取改装状态 ===", flush=True)
    # 已完全改装（改装后不可取消）的注册号集合，后续跳过改装查询
    fully_modified_regs = set()
    for r in existing_fleet.values():
        if (r.get("CO2减排放") == "已改装"
                and r.get("飞行速度增加") == "已改装"
                and r.get("耗油量减少") == "已改装"):
            fully_modified_regs.add(r.get("注册号", "").strip().upper())

    print(f"已有 {len(fully_modified_regs)} 架飞机完成改装，将跳过查询")
    maint_plan_html = fetch(MAINT_PLAN, label="正在获取改装列表")
    parts = re.split(r"(<div class='row (?:not-at-base|at-base) p-1 mt-2 maint-list-sort')", maint_plan_html)

    reg_to_mod_id = {}
    at_base_mod_ids = []
    for part in parts:
        reg_match = re.search(r"data-reg=\"([^\"]+)\"", part)
        modify_match = re.search(r"type=modify&(?:amp;)?id=(\d+)", part)
        if reg_match and modify_match:
            reg = html.unescape(reg_match.group(1)).strip().upper()
            reg_to_mod_id[reg] = modify_match.group(1)
            if "at-base" in part and "not-at-base" not in part:
                at_base_mod_ids.append(modify_match.group(1))

    if at_base_mod_ids:
        first_mod_id = at_base_mod_ids[0]
    elif reg_to_mod_id:
        first_mod_id = list(reg_to_mod_id.values())[0]
    else:
        first_mod_id = None

    at_base_from_selector = []
    if first_mod_id:
        first_html = fetch(
            f"{BASE}/maint_plan_do.php?type=modify&id={first_mod_id}",
            referer=MAINT_PLAN, label="查询当前可查询的飞机",
        )
        for m in re.finditer(r"<option value='(\d+)'>([^<]*（在基地）)</option>", first_html):
            at_base_from_selector.append(m.group(1))

    # 跳过已完全改装飞机的 mod_id，不再重复查询
    skip_mod_ids = {reg_to_mod_id[r] for r in fully_modified_regs if r in reg_to_mod_id}
    # mod_id -> 注册号 反向映射，供 fetch_all_mods 在进度日志中显示注册号
    mod_id_to_reg = {reg_to_mod_id[r]: r for r in reg_to_mod_id}
    at_base_to_query = {
        mid: mod_id_to_reg.get(mid, "")
        for mid in at_base_from_selector
        if mid not in skip_mod_ids
    }
    print(f"当前共有 {len(at_base_to_query)} 架飞机可被查询")
    mod_map = fetch_all_mods(at_base_to_query) if at_base_to_query else {}


    # ---- 4. 合并输出机队 ----
    print("\n=== 合并机队数据 ===", flush=True)

    def apply_mod_status(row: dict) -> None:
        """填充改装状态：已改装/未改装/无法查询/未查询。"""
        # 已完全改装的飞机保持现状（改装不可取消）
        if (row.get("CO2减排放") == "已改装"
                and row.get("飞行速度增加") == "已改装"
                and row.get("耗油量减少") == "已改装"):
            return
        reg = row.get("注册号", "").strip().upper()
        mod_id = reg_to_mod_id.get(reg, "")
        mod_data = mod_map.get(mod_id) if mod_id else None
        if isinstance(mod_data, dict):
            row["CO2减排放"] = "已改装" if mod_data["mod1_completed"] else "未改装"
            row["飞行速度增加"] = "已改装" if mod_data["mod2_completed"] else "未改装"
            row["耗油量减少"] = "已改装" if mod_data["mod3_completed"] else "未改装"
        elif mod_id:
            # 有 mod_id 但查询失败/不在基地（无法查询）：已改装不可取消，保留；其余清空
            for k in ("CO2减排放", "飞行速度增加", "耗油量减少"):
                if row.get(k) != "已改装":
                    row[k] = ""
        else:
            # 未在改装列表中（从未查询）
            if row.get("CO2减排放") != "未查询":
                row["CO2减排放"] = "未查询"
                row["飞行速度增加"] = "未查询"
                row["耗油量减少"] = "未查询"

    removed_rows = _reconcile_removed_aircraft(existing_fleet, live_aircraft_ids, total_aircraft)
    if removed_rows:
        removed_payload = [
            {"飞机ID": row.get("飞机ID", ""), "注册号": row.get("注册号", "")}
            for row in removed_rows
        ]
        for n, row in enumerate(removed_payload, 1):
            reg = row.get("注册号", "")
            print(f"{reg} 已售出，将从数据库移除 [{n}/{len(removed_payload)}]", flush=True)
        print(f"__FLEET_REMOVE__{json.dumps(removed_payload, ensure_ascii=False)}", flush=True)
    elif total_aircraft > 0 and len(live_aircraft_ids) != total_aircraft:
        print(
            f"⚠ 数据异常 ({len(live_aircraft_ids)}/{total_aircraft})，跳过检查",
            flush=True,
        )

    all_fleet = list(existing_fleet.values())
    for row in all_fleet:
        row["枢纽分类"] = classify_hub_with(hubs, row.get("起飞机场名称", ""), row.get("到达机场名称", ""))
        # 旧行同步刷新检修状态
        st = status_map.get(row.get("飞机ID", ""), {})
        if "距A-Check小时" in st or "损坏率%" in st:
            row["距A-Check小时"] = st.get("距A-Check小时", "")
            row["损坏率%"] = st.get("损坏率%", "")
        apply_mod_status(row)
        row["最后更新时间"] = now_ts

    for row in fleet_rows:
        row["枢纽分类"] = classify_hub_with(hubs, row.get("起飞机场名称", ""), row.get("到达机场名称", ""))
        apply_mod_status(row)

    # 合并全部行
    all_rows = {r.get("飞机ID", ""): r for r in all_fleet}
    for row in fleet_rows:
        all_rows[row["飞机ID"]] = row

    # 按枢纽排序后写回单文件
    hub_order = {h["name"]: i for i, h in enumerate(hubs)}
    def sort_key(r: dict):
        hub = r.get("枢纽分类", "其他")
        return (0 if hub in hub_order else 1, hub_order.get(hub, 999), hub,
                r.get("注册号", "").strip().upper())

    sorted_rows = sorted(all_rows.values(), key=sort_key)

    _write_full_fleet_snapshot(sorted_rows)
    total_count = len(sorted_rows)
    print(f"已更新 {total_count} 条数据")

    # ---- 5. 检修 ----
    print("\n=== 获取检修数据 ===", flush=True)
    existing_maint = load_existing_csv(MAINT_CSV)
    maint_html = fetch(MAINT_MAIN, label="正在查询检修事件")
    maint_ids = parse_maintenance_ids(maint_html)
    maint_rows = []
    total_maint = len(maint_ids)
    for i, mid in enumerate(maint_ids, 1):
        if mid in existing_maint:
            cached = dict(existing_maint[mid])
            reg = cached.get("飞机注册号", "")
            print(f"{reg} 数据已存在 [{i}/{total_maint}]", flush=True)
            maint_rows.append(cached)
        else:
            detail_html = fetch(
                f"{BASE}/maintenance_details.php?id={mid}",
                referer=MAINT_MAIN,
            )
            detail = parse_maintenance_detail(detail_html)
            reg = detail["aircraft_reg"]
            print(f"已获取 {reg} 信息 [{i}/{total_maint}]", flush=True)
            maint_rows.append({
                "检修ID": mid, "检修类型": detail["check_type"],
                "飞机注册号": detail["aircraft_reg"], "费用": detail["cost"],
            })

    maint_total = save_current_csv(
        MAINT_CSV, MAINT_FIELDNAMES, maint_rows, "检修ID", now_ts)
    print(f"已更新 {maint_total} 条检修数据")

    # ---- 5.5 登记起飞待办（当日补做和 06:00 全量刷新时执行）----
    print("\n=== 登记起飞待办 ===", flush=True)
    if takeoff:
        if not AUTO_TAKEOFF:
            print("自动起飞已关闭，跳过登记起飞待办", flush=True)
        else:
            try:
                from fresh_demand import enqueue_strong_demand
                strong = [r for r in all_rows.values() if r.get("需求状态") == "旺盛"]
                print(f"将为需求充裕的 {len(strong)} 架飞机登记起飞待办", flush=True)
                enqueue_strong_demand(strong, status_map=status_map)
            except Exception as e:
                print(f"⚠ 起飞待办登记异常: {e}", flush=True)

    # ---- 6. 摘要 ----
    known_mods = sum(1 for v in mod_map.values() if isinstance(v, dict))
    co2 = sum(1 for v in mod_map.values() if isinstance(v, dict) and v["mod1_completed"])
    speed = sum(1 for v in mod_map.values() if isinstance(v, dict) and v["mod2_completed"])
    fuel = sum(1 for v in mod_map.values() if isinstance(v, dict) and v["mod3_completed"])

    finished_ts = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
    elapsed = _format_elapsed(time.monotonic() - started_mono)

    print(f"已更新 {total_count} 架飞机的数据")
    print(f"已查询 {known_mods} 架飞机的改装状态")
    print(f"已获取 {maint_total} 条检修数据")

    print(f"\n{'='*30}")
    print(f"更新完成 ({finished_ts})")
    print(f"耗时 {elapsed}")
    print(f"{'='*30}")


# ---------------------------------------------------------------------------
# 轻量循环（BJT 00/30 分）：燃油/CO2 价 + 新飞机登记
# ---------------------------------------------------------------------------

def run_cycle():
    """轻量采集：市场价 + 检测未登记飞机并登记（BJT 00/30 分）。

    不逐架刷新已有机队详情；起飞待办会在执行前只刷新对应飞机。
    """
    started_mono = time.monotonic()
    now_ts = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*30}")
    print(f"轻量采集: {now_ts}")
    print(f"{'='*30}")

    # 登录（账号变更时自动清除旧 cookie 重新登录）
    _ensure_login()

    # 主页：余额/燃油库存 + 航距 + 检修预警
    home_html = fetch(HOME, label="刷新主页")
    dist_map = parse_flight_distances(home_html)
    status_map = parse_status_data(home_html)
    _emit_maintenance_takeovers(status_map)
    if status_map:
        # 注意：货机/部分机型可能无 hoursToCheck/wear 字段，空值跳过防止 ValueError
        _a = [v for v in status_map.values()
              if v.get("距A-Check小时") and _clean_num(v["距A-Check小时"]) < 50]
        _w = [v for v in status_map.values()
              if v.get("损坏率%") and _clean_num(v["损坏率%"]) > 60]
        print(f"已对 {len(status_map)} 架飞机进行维修检查")
        print(f"当前有 {len(_a)} 架飞机需要进行 A 级检查", flush=True)
        print(f"当前有 {len(_w)} 架飞机损坏率过高", flush=True)

    # 检修预告数据来自主页（轻量循环已抓取），顺带推送：零额外请求，前端检修页保持最新
    now_ts_fmt = _now_bjt().strftime("%H:%M:%S")
    print(f"__MAINT__{json.dumps(_build_maint_warnings(status_map, now_ts_fmt), ensure_ascii=False)}", flush=True)
    print(f"__STATUS__{json.dumps(status_map, ensure_ascii=False)}", flush=True)

    # 燃油/CO2 价格（30 分钟整点变动）
    print("\n=== 获取市场数据 ===", flush=True)
    fuel_html = fetch(FUEL, referer=HOME, label="正在获取燃油价格")
    co2_html = fetch(CO2, referer=HOME, label="正在获取 CO₂ 价格")
    market = parse_market_data(home_html, fuel_html, co2_html)
    save_market_data(market)
    if market["balance"]:
        print(f"当前余额: ${market['balance']}", flush=True)
        print(f"CO₂配额: {market['co2_qty']};\n燃油库存: {market['fuel_qty']} Lbs;", flush=True)
        print(f"CO₂价格: ${market['co2_price']}/1000;\n燃油价格: ${market['fuel_price']}/1000 Lbs;", flush=True)
    else:
        print("⚠ 市场数据解析失败", flush=True)

    # 输出市场数据行，让 server 实时推送前端 market-bar（价格/余额即时更新）
    print(f"__MARKET__{json.dumps(market, ensure_ascii=False)}", flush=True)

    # 自动补货：复用本次已抓 fuel/co2 页面，在现金安全垫、单轮预算和容量内购买。
    try:
        from auto_buy import auto_buy
        purchased = auto_buy(fuel_html, co2_html, market.get("balance"),
                             buy_fuel=AUTO_BUY_FUEL, buy_co2=AUTO_BUY_CO2)
        apply_purchase_to_market(market, purchased)
    except Exception as e:
        print(f"⚠ 自动补货异常: {e}", flush=True)

    # 枢纽
    hubs = parse_hubs(fetch(HUBS, label="正在获取枢纽信息"))
    try:
        HUB_LIST_JSON.write_text(json.dumps(hubs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(f"__HUBS__{json.dumps([h['name'] for h in hubs], ensure_ascii=False)}", flush=True)
    print(f"当前共有 {len(hubs)} 个枢纽")

    # 检测新增飞机：对比现有 CSV 的飞机ID，只给新飞机登记详情
    existing_fleet = load_existing_csv(FLEET_CSV)
    fleet_main_html = fetch(FLEET_MAIN, label="正在获取机队信息")
    # 轻量循环同样纳入货机/包机分组
    fleet_types = parse_fleet_types(fleet_main_html)
    expected_total = sum(t["pax"] + t["cargo"] + t["charter"] for t in fleet_types)

    new_rows = []
    detail_count = 0
    live_aircraft_ids: set[str] = set()
    inventory_changed = False
    renamed_count = 0
    for group in fleet_types:
        type_html = fetch(
            f"{BASE}/fleet.php?type={group['type_id']}", referer=FLEET_MAIN,
            label=f"正在查询分组 {group['type_id']}",
        )
        for entry in parse_fleet_entries(type_html):
            ac_id = entry["aircraft_id"]
            live_aircraft_ids.add(ac_id)
            detail_count += 1
            if ac_id in existing_fleet:
                # 轻量轮次也同步注册号；飞机 ID 稳定，因此改名不会产生重复行。
                current = existing_fleet[ac_id]
                live_reg = (entry.get("reg") or "").strip().upper()
                if live_reg and live_reg != current.get("注册号", "").strip().upper():
                    old_reg = current.get("注册号", "")
                    current["注册号"] = live_reg
                    current["最后更新时间"] = now_ts
                    inventory_changed = True
                    renamed_count += 1
                    print(f"原注册号 {old_reg} 现已变更为 {live_reg}", flush=True)
                    print(f"__AIRCRAFT__{json.dumps(current, ensure_ascii=False)}", flush=True)
                continue
            detail_html = fetch(
                f"{BASE}/fleet_details.php?id={ac_id}&returnType={entry['return_type']}",
                referer=f"{BASE}/fleet.php?type={group['type_id']}",
                label=f"正在查询新飞机 {entry['reg']} ({detail_count})",
            )
            if not detail_html.strip():
                print(f"⚠ {entry['reg']} 信息获取失败，留待下轮登记", flush=True)
                continue
            detail = parse_aircraft_detail(detail_html)
            row = {
                "飞机ID": ac_id,
                "注册号": (detail["fleet_reg"] or entry["reg"]).upper(),
                "航班号": detail["route_reg"] or entry["reg"],
                "机型": detail["aircraft_model"] or entry["model"],
                "经济舱座位": detail["seat_economy"],
                "商务舱座位": detail["seat_business"],
                "头等舱座位": detail["seat_first"],
                "经济舱票价": detail["ticket_economy"],
                "商务舱票价": detail["ticket_business"],
                "头等舱票价": detail["ticket_first"],
                "起飞机场代码": detail["origin_code"],
                "起飞机场名称": detail["origin_name"],
                "到达机场代码": detail["destination_code"],
                "到达机场名称": detail["destination_name"],
                "起飞时间UTC": detail["departure_utc"],
                "到达时间UTC": detail["arrival_utc"],
                "飞行时长": detail["flight_duration"],
                "航距km": dist_map.get(ac_id, ""),
                "枢纽分类": classify_hub_with(hubs, detail["origin_name"], detail["destination_name"]),
                "距A-Check小时": status_map.get(ac_id, {}).get("距A-Check小时", ""),
                "损坏率%": status_map.get(ac_id, {}).get("损坏率%", ""),
                "CO2减排放": "未查询", "飞行速度增加": "未查询", "耗油量减少": "未查询",
                "经济舱需求": detail["demand_economy"],
                "商务舱需求": detail["demand_business"],
                "头等舱需求": detail["demand_first"],
                "大货需求": detail["cargo_large_demand"],
                "重货需求": detail["cargo_heavy_demand"],
                "大货容量": detail["cargo_large_cap"],
                "重货容量": detail["cargo_heavy_cap"],
                "需求状态": detail["demand_status"],
                "组类型": group["type_id"],
                "客机组数量": str(group["pax"]),
                "最后更新时间": now_ts,
            }
            new_rows.append(row)
            # 增量落盘：轻量循环登记新飞机时同样实时可见
            _write_fleet_snapshot(existing_fleet, hubs, new_rows)
            print(f"__AIRCRAFT__{json.dumps(row, ensure_ascii=False)}", flush=True)

    removed_rows = _reconcile_removed_aircraft(existing_fleet, live_aircraft_ids, expected_total)
    if removed_rows:
        inventory_changed = True
        removed_payload = [
            {"飞机ID": row.get("飞机ID", ""), "注册号": row.get("注册号", "")}
            for row in removed_rows
        ]
        for n, row in enumerate(removed_payload, 1):
            reg = row.get("注册号", "")
            print(f"{reg} 已售出，将从数据库移除 [{n}/{len(removed_payload)}]", flush=True)
        print(f"__FLEET_REMOVE__{json.dumps(removed_payload, ensure_ascii=False)}", flush=True)
    elif expected_total > 0 and len(live_aircraft_ids) != expected_total:
        print(
            f"⚠ 数据异常 ({len(live_aircraft_ids)}/{expected_total})，跳过检查",
            flush=True,
        )

    if new_rows:
        for r in new_rows:
            existing_fleet[r["飞机ID"]] = r
        _write_light_fleet_snapshot(
            existing_fleet, new_rows, removed_rows, status_map, hubs)
        print(f"本次采集新增 {len(new_rows)} 架飞机，数据库已更新", flush=True)
    else:
        # 无新增也要刷新已有机队的检修状态字段（status_map 更新）
        changed = inventory_changed
        for r in existing_fleet.values():
            st = status_map.get(r.get("飞机ID", ""), {})
            if ("距A-Check小时" in st or "损坏率%" in st) and (
                r.get("距A-Check小时", "") != st.get("距A-Check小时", "")
                or r.get("损坏率%", "") != st.get("损坏率%", "")
            ):
                r["距A-Check小时"] = st.get("距A-Check小时", "")
                r["损坏率%"] = st.get("损坏率%", "")
                changed = True
        if changed:
            _write_light_fleet_snapshot(
                existing_fleet, [], removed_rows, status_map, hubs)
        suffix = f"，已同步 {renamed_count} 条注册号更新" if renamed_count else ""
        print(f"未发现新增飞机{suffix}", flush=True)

    finished_ts = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
    elapsed = _format_elapsed(time.monotonic() - started_mono)

    print(f"\n{'='*30}")
    print(f"更新完成 ({finished_ts})")
    print(f"耗时 {elapsed}")
    print(f"{'='*30}")

def run_preclose_topup() -> None:
    """在价格周期结束前补一次低价资源；缓存过期时先补读本周期价格。"""
    print("\n=== 检查低价资源 ===", flush=True)
    try:
        market = json.loads(MARKET_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print("缓存不可用，等待下一次检查", flush=True)
        return

    from auto_buy import _CO2_THRESHOLD, _FUEL_THRESHOLD, auto_buy

    now_bjt = _now_bjt()
    try:
        cached_at = datetime.strptime(
            str(market.get("updated_at", "")), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=BJT)
    except (TypeError, ValueError):
        cached_at = None
    cycle_minute = 30 if now_bjt.minute >= 30 else 0
    cycle_start = now_bjt.replace(minute=cycle_minute, second=0, microsecond=0)
    if cached_at is None or cached_at < cycle_start:
        print("缓存已过期，刷新中……", flush=True)
        home_html = fetch(HOME, label="刷新主页")
        fuel_html = fetch(FUEL, referer=HOME, label="正在更新燃油信息")
        co2_html = fetch(CO2, referer=HOME, label="正在更新 CO2 信息")
        fresh_market = parse_market_data(home_html, fuel_html, co2_html)
        if not _market_valid(fresh_market):
            print("刷新失败，等待下一次检查", flush=True)
            return
        market = fresh_market
        save_market_data(market)
        print(f"__MARKET__{json.dumps(market, ensure_ascii=False)}", flush=True)
        purchased = auto_buy(fuel_html, co2_html, market.get("balance"),
                             buy_fuel=AUTO_BUY_FUEL, buy_co2=AUTO_BUY_CO2)
        if not apply_purchase_to_market(market, purchased):
            print("本周期资源无需补仓，等待下一次检查", flush=True)
        return

    fuel_price = _clean_num(str(market.get("fuel_price", "0")))
    co2_price = _clean_num(str(market.get("co2_price", "0")))
    check_fuel = AUTO_BUY_FUEL and 0 < fuel_price < _FUEL_THRESHOLD
    check_co2 = AUTO_BUY_CO2 and 0 < co2_price < _CO2_THRESHOLD
    if not check_fuel and not check_co2:
        print("价格高于采购阈值，等待下一次检查", flush=True)
        return

    print(f"CO₂ 价格: ${co2_price:g}/1000 配额;\n燃油价格: ${fuel_price:g}/1000 Lbs;", flush=True)

    fuel_html = (fetch(FUEL, referer=HOME, label="正在更新燃油信息")
                 if check_fuel else "")
    co2_html = (fetch(CO2, referer=HOME, label="正在更新 CO₂ 信息")
                if check_co2 else "")
    purchased = auto_buy(fuel_html, co2_html, market.get("balance"),
                         buy_fuel=AUTO_BUY_FUEL, buy_co2=AUTO_BUY_CO2)
    if not apply_purchase_to_market(market, purchased):
        print("低价资源已满，等待下一次检查", flush=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _next_cycle_ts(now: datetime) -> datetime:
    """下个调度槽位：00/30 正常轻量，29/59 低价周期收尾补仓。"""
    for minute in (0, 29, 30, 59):
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _is_preclose_slot(now: datetime) -> bool:
    return now.minute in (29, 59)


def _load_last_full_date() -> str:
    try:
        state = json.loads(SCHEDULE_STATE_JSON.read_text(encoding="utf-8"))
        return str(state.get("last_full_date", "")) if isinstance(state, dict) else ""
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ""


def _save_last_full_date(day: str) -> None:
    tmp = SCHEDULE_STATE_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"last_full_date": day}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, SCHEDULE_STATE_JSON)


def main():
    parser = argparse.ArgumentParser(description="Airline Manager 4 数据提取")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="循环运行")
    mode.add_argument("--light", action="store_true", help="调试：只运行一次轻量采集")
    parser.add_argument("--interval", type=int, default=None, help="循环间隔（秒）；不传则轻量对齐 00/30、收尾补仓对齐 29/59")
    args = parser.parse_args()

    global _current_delay

    if args.light:
        _current_delay = _DELAY_ONCE
        run_cycle()
    elif args.loop:
        _current_delay = _DELAY_LOOP
        # 用户显式传 --interval：按固定秒数间隔循环；否则轻量 00/30，收尾补仓 29/59。
        use_fixed_interval = args.interval is not None and args.interval > 0
        print("=== 循环模式启动 ===", flush=True)
        print("将在 30/00 分执行轻量刷新，29/59 分最终补仓", flush=True)
        print("将在 06:00 将全量刷新航线、改装与检修信息", flush=True)
        if use_fixed_interval:
            print(f"当前使用固定间隔：{args.interval} 秒/轮", flush=True)
        startup_now = _now_bjt()
        completed_today = (
            startup_now.hour >= DAILY_FULL_HOUR_BJT
            and _load_last_full_date() == startup_now.date().isoformat()
        )
        _last_full_date = startup_now.date() if completed_today else None
        _first_iteration = not completed_today
        if completed_today:
            print("本次重启以轻量模式启动", flush=True)
            nxt = _next_cycle_ts(startup_now)
            wait = max(5, (nxt - _now_bjt()).total_seconds())
            print(f"__SLEEP__{int(wait)}", flush=True)
            time.sleep(wait)
        while True:
            try:
                # 槽位调度（按 BJT 日期去重，容忍启动/耗时漂移）：
                #   轻量 = 其余时刻（市场价/补货/新机登记/检修状态）
                #   BJT 06:00 = 全面刷新（全部详情/改装/检修）+ 登记起飞待办，
                #   此时游戏刚重置当天乘客需求，全量抓取让页面显示当天最新需求
                now_bjt = _now_bjt()  # 北京时间（显式 UTC+8）
                if _first_iteration:
                    # 当日尚未成功全量时立即补做（含改装/检修/全部详情）。
                    run_once(takeoff=True)
                    # 只在正常返回后提交标记；异常会在 60 秒后重试全量。
                    _first_iteration = False
                    # 06:00 前启动不能吞掉当天的需求重置后全量刷新。
                    _last_full_date = (now_bjt.date()
                                       if now_bjt.hour >= DAILY_FULL_HOUR_BJT else None)
                    if _last_full_date is not None:
                        _save_last_full_date(_last_full_date.isoformat())
                elif (now_bjt.hour == DAILY_FULL_HOUR_BJT
                      and _last_full_date != now_bjt.date()):
                    # 06:00 全面刷新，并为需求旺盛飞机登记待办
                    run_once(takeoff=True)
                    _last_full_date = now_bjt.date()
                    _save_last_full_date(_last_full_date.isoformat())
                elif _is_preclose_slot(now_bjt):
                    run_preclose_topup()
                else:
                    run_cycle()
                if use_fixed_interval:
                    print(f"\n等待 {args.interval} 秒执行下一次...", flush=True)
                    print(f"__SLEEP__{max(5, args.interval)}", flush=True)
                    time.sleep(max(5, args.interval))
                else:
                    # 睡到下一个 00/30 分轻量槽位或 29/59 分收尾补仓槽位。
                    nxt = _next_cycle_ts(_now_bjt())
                    wait = (nxt - _now_bjt()).total_seconds()
                    local = nxt.strftime("%H:%M")
                    print(f"\n等待 {int(wait)} 秒到 {local} 执行下一次...", flush=True)
                    print(f"__SLEEP__{int(max(5, wait))}", flush=True)
                    time.sleep(max(5, wait))
            except KeyboardInterrupt:
                print("\n用户中断，退出。")
                sys.exit(0)
            except Exception as e:
                print(f"执行异常: {e}，60 秒后重试...", flush=True)
                time.sleep(60)
    else:
        _current_delay = _DELAY_ONCE
        run_once()


if __name__ == "__main__":
    main()
