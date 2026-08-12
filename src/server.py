"""Airline Manager 4 数据展示后端服务。"""
from __future__ import annotations

import csv
import json
import os
import queue
import random
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, abort, jsonify, request, render_template, Response

from account_storage import account_key, account_output_dir, normalize_account
from storage_utils import atomic_write_json, exclusive_file_lock

# Windows 下重定向 stdout 默认用 GBK，emoji/中文可能抛 UnicodeEncodeError；
# 强制 UTF-8 + 替换符，避免打印含 ⚠/✈ 等字符时崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_ROOT = Path(os.environ.get("AM4_OUTPUTS_DIR", str(ROOT / "outputs")))


def _load_env():
    """从项目根 .env 读取环境变量（若未显式设置）。"""
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

BJT = timezone(timedelta(hours=8), name="Asia/Shanghai")  # 北京时间（UTC+8，无夏令时）
_TAKEOFF_READY_BUFFER_SECONDS = 120


def _now_bjt() -> datetime:
    """当前北京时间（显式 UTC+8，不依赖机器时区）。"""
    return datetime.now(BJT)


def _current_env_credentials() -> tuple[str, str]:
    """实时读取 .env 中的 AM4_EMAIL / AM4_PASSWORD（不依赖进程启动时的缓存值）。"""
    email, password = "", ""
    try:
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("AM4_EMAIL="):
                    email = line.split("=", 1)[1].strip()
                elif line.startswith("AM4_PASSWORD="):
                    password = line.split("=", 1)[1].strip()
    except Exception:
        pass
    return (email or os.environ.get("AM4_EMAIL", ""),
            password or os.environ.get("AM4_PASSWORD", ""))


def _paths_for_account(email: str) -> dict:
    """指定账号的数据文件路径。"""
    d = account_output_dir(OUTPUTS_ROOT, email)
    legacy_fleet = d / "fleet_complete.csv"
    fleet = d / "fleet.csv"
    if not fleet.exists() and legacy_fleet.exists():
        try:
            legacy_fleet.replace(fleet)
        except OSError:
            fleet = legacy_fleet
    return {
        "fleet": fleet,
        "maint": d / "maintenance_checks.csv",
        "market": d / "market_data.json",
        "hubs": d / "hub_list.json",
        "log": d / "run_log.txt",
        "pending": d / "pending_tasks.json",
        "builds": d / "builds.csv",
    }


_initial_email, _initial_password = _current_env_credentials()
_account_lock = threading.RLock()
_active_account_email = _initial_email
_active_account_password = _initial_password
_active_account_key = account_key(_initial_email)


def _active_credentials() -> tuple[str, str]:
    with _account_lock:
        return _active_account_email, _active_account_password


def _paths() -> dict:
    """已激活账号的数据路径；运行中修改 .env 不会令一次操作跨账号。"""
    with _account_lock:
        email = _active_account_email
    return _paths_for_account(email)


def _migrate_legacy_outputs() -> None:
    """旧版本数据位于 outputs/ 根目录：迁移到当前账号子目录（仅当子目录为空时）。"""
    try:
        legacy = OUTPUTS_ROOT
        d = account_output_dir(OUTPUTS_ROOT, _current_env_credentials()[0])
        if legacy != d and legacy.exists() and not any(d.iterdir()):
            for f in legacy.iterdir():
                if f.is_file():
                    f.replace(d / f.name)
    except Exception:
        pass


_migrate_legacy_outputs()


# 页面级 CSRF 令牌：由 / 路由注入前端，POST 写操作（/api/run、/api/stop）必须匹配。
# 令牌持久化到磁盘：服务重启后不失效，已打开的页面无需刷新。
_CSRF_TOKEN_FILE = ROOT / "work" / ".csrf_token"


def _load_csrf_token() -> str:
    try:
        tok = _CSRF_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        _CSRF_TOKEN_FILE.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok


_csrf_token = _load_csrf_token()


def _require_csrf():
    """POST 写操作保护：要求携带与页面令牌匹配的 X-CSRF-Token 头。

    浏览器同源策略使跨源页面无法读取本站 HTML（拿不到令牌），
    而跨源 fetch 携带自定义头会触发 CORS 预检并被拒 —— 双保险。
    """
    if request.headers.get("X-CSRF-Token", "") != _csrf_token:
        # 抛出携带 JSON 响应体的 403，避免返回 HTML 错误页（前端解析失败），
        # 同时确保调用方不 return 也会生效
        abort(Response(
            json.dumps({"ok": False, "error": "页面令牌已过期，请刷新页面后重试"}),
            status=403, mimetype="application/json",
        ))

app = Flask(__name__)


@app.before_request
def _activate_configured_account():
    _sync_account_context()

# 航线开辟规划器（懒加载，避免与采集子进程 Cookie 竞争时初始化）
_route_planner = None
_online_session_lock = threading.RLock()


def _serialized_online(fn):
    """让服务进程内所有使用共享 Cookie 的在线操作串行执行。"""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _online_session_lock:
            return fn(*args, **kwargs)
    return wrapped


def _get_route_planner(require_login: bool = False):
    """获取航线引擎；本地筛选不登录，实时精算/建设时才初始化在线会话。"""
    global _route_planner
    if _route_planner is None:
        import route_planner as rp
        _route_planner = rp
    if require_login:
        tmp_dir = Path(os.environ.get("TEMP", str(Path.home() / "AppData" / "Local" / "Temp")))
        email, password = _active_credentials()
        # _do_curl 读取的是 extract 模块的全局 Cookie，必须与 market worker 同一套会话
        import collector as ext
        ext.COOKIE_JAR = tmp_dir / "am4_cookiejar_worker.txt"
        ext.ACCOUNT_MARKER = ext.COOKIE_JAR.with_name("am4_cookiejar_worker_account.txt")
        ext.EMAIL, ext.PASSWORD = email, password
        ext._ensure_login()
    return _route_planner

_run_lock = threading.Lock()
_run_status = {
    "running": False,
    "mode": "",
    "last_run": None,
    "error": None,
    "progress_total": 0,
    "progress_current": 0,
}
_run_proc = None
_sse_clients: list[queue.Queue] = []
_stop_requested = False  # 用户手动停止标记：区分主动停止与脚本异常退出

# 最新检修需求缓存（采集脚本 __MAINT__ 行写入，供实时展示）
_maint_cache: dict | None = None
_maint_cache_lock = threading.Lock()
_home_status_cache: dict[str, dict] | None = None
_home_status_ts: float = 0.0

# 余额/燃油库存实时缓存：页面访问立即返回旧值，后台异步刷新
_market_rt_cache: dict | None = None
_market_rt_lock = threading.Lock()
_market_rt_ts: float = 0.0
_market_rt_min_interval = 120.0  # 完整抓取耗时较长；两分钟内复用缓存，避免刚成功就再次登录
_market_rt_worker_lock = threading.Lock()  # 防止并发的 market worker 重复抓取
_market_rt_failures = 0
_market_rt_retry_after = 0.0
_market_rt_last_error = ""


def _market_retry_failure(message: str) -> int:
    """记录市场刷新失败并返回退避秒数（60/120/300/600，最高10分钟）。"""
    global _market_rt_failures, _market_rt_retry_after, _market_rt_last_error
    with _market_rt_lock:
        _market_rt_failures += 1
        delay = (60, 120, 300, 600)[min(_market_rt_failures - 1, 3)]
        _market_rt_retry_after = time.time() + delay
        _market_rt_last_error = message
    return delay


def _market_retry_success() -> None:
    global _market_rt_failures, _market_rt_retry_after, _market_rt_last_error
    with _market_rt_lock:
        _market_rt_failures = 0
        _market_rt_retry_after = 0.0
        _market_rt_last_error = ""


def _refresh_market_after_spend() -> None:
    """扣款成功后只刷新主页余额，避免额外读取燃油/CO₂页面。"""
    global _market_rt_cache, _market_rt_ts, _home_status_cache, _home_status_ts
    try:
        _get_route_planner(require_login=True)
        import collector as ext
        page = ext._do_curl(ext.HOME, data=None, output=None, referer=ext.HOME)
        match = re.search(r"id='headerAccount'>([^<]+)<", page or "")
        if not match:
            raise ValueError("主页未返回可识别余额")
        balance = match.group(1).strip()
        status_map = ext.parse_status_data(page)
        with _market_rt_lock:
            cache = dict(_market_rt_cache or {})
            cache["balance"] = balance
            cache["updated_at"] = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
            _market_rt_cache = cache
            _market_rt_ts = time.time()
        if status_map:
            with _maint_cache_lock:
                _home_status_cache = status_map
                _home_status_ts = time.time()
        _broadcast_sse({"type": "market", "data": cache})
    except Exception as exc:
        _append_log(f"⚠ 扣款成功，但余额即时刷新失败：{exc}")


def _refresh_market_rt_worker():
    """后台刷新市场数据：复用采集脚本的登录/抓取/解析逻辑。

    使用服务端 Cookie 会话（am4_cookiejar_worker.txt），与采集子进程隔离；
    与航线/待办在线操作通过统一锁串行，登录态过期由 _ensure_login 自动重登。
    """
    global _market_rt_cache, _market_rt_ts, _home_status_cache, _home_status_ts
    """
    try:
        _append_log("[market-worker] 开始抓取余额")
    except Exception:
        pass
    """
    try:
        env_email, env_password = _active_credentials()
        if not env_email or not env_password:
            delay = _market_retry_failure(".env 缺少凭据")
            _append_log(f"[market-worker] .env 缺少凭据，{delay} 秒后重试")
            return
        import collector as ext
        # 服务端会话：与采集子进程分离，与航线/待办通过在线会话锁串行。
        tmp_dir = Path(os.environ.get("TEMP", str(Path.home() / "AppData" / "Local" / "Temp")))
        ext.COOKIE_JAR = tmp_dir / "am4_cookiejar_worker.txt"
        ext.ACCOUNT_MARKER = ext.COOKIE_JAR.with_name("am4_cookiejar_worker_account.txt")
        # 使用已经原子激活的账号凭据。
        ext.EMAIL, ext.PASSWORD = env_email, env_password
        ext._ensure_login()  # 登录态过期自动重新登录

        home_html = ext._do_curl(ext.HOME, data=None, output=None, referer="")
        status_map = ext.parse_status_data(home_html)
        if status_map:
            with _maint_cache_lock:
                _home_status_cache = status_map
                _home_status_ts = time.time()
        fuel_html = ext._do_curl(ext.FUEL, data=None, output=None, referer=ext.HOME)
        co2_html = ext._do_curl(ext.CO2, data=None, output=None, referer=ext.HOME)
        market = ext.parse_market_data(home_html, fuel_html, co2_html)
        if not ext._market_valid(market):
            delay = _market_retry_failure("解析结果无效")
            _append_log(f"[market-worker] 解析结果无效，保留旧缓存，{delay} 秒后重试")
            return
        with _market_rt_lock:
            _market_rt_cache = market
            _market_rt_ts = time.time()
        _market_retry_success()
        # 不写回磁盘：避免用"只有余额/燃油"的实时快照覆盖采集脚本的完整市场快照（含价格/配额）
        _broadcast_sse({"type": "market", "data": market})
        # _append_log(f"[market-worker] 成功更新余额 {market.get('balance')}")
    except Exception as e:
        try:
            if isinstance(e, subprocess.CalledProcessError):
                reason = ("无法连接游戏服务器（curl 7）" if e.returncode == 7
                          else f"curl 失败（退出码 {e.returncode}）")
            else:
                reason = f"{type(e).__name__}: {e}"
            delay = _market_retry_failure(reason)
            _append_log(f"[market-worker] {reason}，保留旧数据，{delay} 秒后重试")
        except Exception:
            pass

# ===== 日志持久化 =====


def _read_log_lines() -> list[str]:
    """从磁盘读取全部日志行（日志以磁盘文件为唯一数据源，不驻留内存）。"""
    try:
        lf = _paths()["log"]
        if lf.exists():
            return lf.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        pass
    return []


def _load_log_history():
    """启动时：将上次运行的日志另存为带时间戳的 .bak，然后清空日志区。

    重启后刷新页面应显示"等待首次运行…"，不应看到旧日志；
    旧日志保留在 run_log_YYYYmmdd_HHMMSS.txt.bak 供日后查阅。
    """
    try:
        lf = _paths()["log"]
        if lf.exists():
            content = lf.read_text(encoding="utf-8")
            if content.strip():
                ts = _now_bjt().strftime("%Y%m%d_%H%M%S")
                bak = lf.with_name(f"run_log_{ts}.txt.bak")
                bak.write_text(content, encoding="utf-8")
            lf.write_text("", encoding="utf-8")
    except Exception:
        pass


_load_log_history()


def _append_log(line: str):
    # 追加写盘，保留完整历史供动态加载
    try:
        with _paths()["log"].open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ===== 待定任务队列 =====
_pending_lock = threading.Lock()
_pending_tasks: list[dict] = []
_pending_seq = 0


def _is_recoverable_network_error(error) -> bool:
    text = str(error or "")
    return bool(re.search(
        r"(?:exit status (?:6|7|28)\b|could not resolve|failed to connect|"
        r"connection (?:reset|refused)|timed? out)", text, re.I))


def _load_pending_tasks(path: Path | None = None, owner: str | None = None) -> None:
    """启动时恢复未完成的待定任务（跨重启保留）。"""
    global _pending_tasks, _pending_seq
    _pending_tasks = []
    _pending_seq = 0
    try:
        p = path or _paths()["pending"]
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("待办文件顶层必须是数组")
        loaded_tasks = [t for t in data if isinstance(t, dict)]
        # 恢复 pending + running（上次进程可能中断在执行中）：running 重置为 pending 以便重试
        _pending_tasks = [t for t in loaded_tasks
                          if (t.get("status") in ("pending", "running")
                              or (t.get("kind") == "retrofit"
                                  and t.get("status") == "failed")
                              or (t.get("kind") == "takeoff"
                                  and t.get("status") == "failed"
                                  and _is_recoverable_network_error(t.get("error"))))]
        fixed = False
        owned_tasks = []
        expected_owner = str(owner or _active_account_key)
        recovered_network = 0
        recovery_base = time.time() + 120
        for t in _pending_tasks:
            if not t.get("account"):
                fixed = True
            stored_owner = str(t.get("account") or expected_owner)
            if stored_owner != expected_owner:
                fixed = True
                continue
            t["account"] = stored_owner
            if t.get("status") == "running":
                t["status"] = "pending"
                t["trigger_at"] = time.time()
            elif (t.get("status") == "failed" and t.get("kind") == "takeoff"
                  and _is_recoverable_network_error(t.get("error"))):
                # 断网期间到期的飞机错峰恢复；每 30 秒放行一架，避免联网后集中访问。
                t["status"] = "pending"
                t["trigger_at"] = recovery_base + recovered_network * 30
                t["error"] = "网络恢复后等待重新确认"
                t["retry"] = {"category": "network_recovery", "attempts": 0}
                recovered_network += 1
                fixed = True
            owned_tasks.append(t)
        _pending_tasks = owned_tasks
        if recovered_network:
            _append_log(
                f"🌐 已恢复 {recovered_network} 条断网失败待办，"
                f"将在约 {max(2, (recovered_network - 1) // 2 + 2)} 分钟内重新确认"
            )
        # 重启后 _pending_seq 归零会与旧任务 id 撞号（取消/操作会误伤同名任务）；
        # 按已加载任务的最大序号续排，并给重复/缺失 id 重新分配
        seen: set[str] = set()
        for raw in loaded_tasks:
            match = re.match(r"^t(\d+)$", str(raw.get("id", "")))
            if match:
                _pending_seq = max(_pending_seq, int(match.group(1)))
        for t in _pending_tasks:
            tid = t.get("id", "")
            if not tid or tid in seen:
                fixed = True
                _pending_seq += 1
                t["id"] = f"t{_pending_seq}"
            else:
                seen.add(tid)
                m = re.match(r"^t(\d+)$", tid)
                if m:
                    _pending_seq = max(_pending_seq, int(m.group(1)))
        # 历史版本可能为同一飞机/航线累计多条起飞任务。成功起飞后应以
        # 最新预计落地时间为准，而不是让旧任务提前重复请求。
        unique: dict[tuple, dict] = {}
        cleaned: list[dict] = []
        for t in _pending_tasks:
            keys = {
                "takeoff": ("route_id", "reg"),
                "takeoff_reconcile": ("route_id", "reg"),
                "retrofit": ("route_id", "reg"),
                "delivery_continue": ("fid", "reg"),
            }.get(t.get("kind"), ())
            if not keys:
                cleaned.append(t)
                continue
            ident = (t.get("kind"),) + tuple(
                str((t.get("params") or {}).get(k, "")) for k in keys)
            old = unique.get(ident)
            if old is None:
                unique[ident] = t
                cleaned.append(t)
                continue
            fixed = True
            if t.get("kind") == "takeoff":
                # 后创建的任务对应更新的一次成功起飞，保留其落地时间。
                if float(t.get("created_at", 0)) >= float(old.get("created_at", 0)):
                    old.update(t)
            elif float(t.get("trigger_at", 0)) < float(old.get("trigger_at", 0)):
                old.update(t)
        _pending_tasks = cleaned
        repaired = _repair_legacy_doubled_takeoffs(
            _pending_tasks, _read_csv(_paths()["fleet"]))
        if repaired:
            fixed = True
            _append_log(f"🔧 已校准 {repaired} 条错误待办")
        # 旧版本在新航线首次起飞前读到 00:00:00 时，会漏掉下一班待办。
        # 启动时为这类已成功首航、仍无后续任务的飞机补一条只读对账任务。
        fleet_by_reg = {
            str(row.get("注册号", "")).strip().upper(): row
            for row in _read_csv(_paths()["fleet"])
        }
        active_followups = {
            str((t.get("params") or {}).get("reg", "")).strip().upper()
            for t in _pending_tasks
            if (t.get("kind") in {"takeoff", "takeoff_reconcile"}
                and t.get("status") in {"pending", "running"})
        }
        for old in loaded_tasks:
            params = old.get("params") or {}
            reg = str(params.get("reg", "")).strip().upper()
            if (old.get("kind") != "takeoff" or old.get("status") != "done"
                    or params.get("reason") != "改装完成" or not reg
                    or str(old.get("account") or expected_owner) != expected_owner
                    or reg in active_followups
                    or _flight_duration_seconds(
                        (fleet_by_reg.get(reg) or {}).get("飞行时长", "")) > 0):
                continue
            _pending_seq += 1
            _pending_tasks.append({
                "id": f"t{_pending_seq}", "account": expected_owner,
                "kind": "takeoff_reconcile",
                "title": f"检查 {reg} 首航时长（航线 {params.get('route_id', '')}）",
                "trigger_at": time.time() + 15, "status": "pending",
                "created_at": time.time(), "error": None,
                "params": {
                    "route_id": str(params.get("route_id", "")), "reg": reg,
                    "cost_index": int(params.get("cost_index", 200)),
                    "fid": str(params.get("fid", "")),
                    "hub_id": str(params.get("hub_id", "")),
                    "started_at": float(old.get("completed_at", old.get("trigger_at", 0)) or 0),
                },
            })
            active_followups.add(reg)
            fixed = True
        if fixed:
            _save_pending_tasks(p)
    except Exception as e:
        _pending_tasks = []
        _append_log(f"⚠ 待办任务加载失败，未执行任何旧任务：{e}")


def _save_pending_tasks(path: Path | None = None) -> None:
    try:
        atomic_write_json(path or _paths()["pending"], _pending_tasks)
    except Exception as e:
        _append_log(f"⚠ 待办任务保存失败，保留上一份完整文件：{e}")
    # 待办有变化即推 SSE，前端实时刷新清单与状态
    try:
        _broadcast_sse({"type": "pending"})
    except Exception:
        pass


def _ensure_marketing_tasks() -> None:
    """确保两种营销各有一个长期待办；首次执行会只读活动剩余时间。"""
    now = time.time()
    with _pending_lock:
        existing = {
            str((task.get("params") or {}).get("campaign", ""))
            for task in _pending_tasks
            if task.get("kind") == "marketing" and task.get("status") in {"pending", "running"}
        }
    if "airline" not in existing:
        _add_pending_task("marketing", "广告 4 自动续期（24 小时）", now + 15,
                          {"campaign": "airline"})
    if "eco" not in existing:
        _add_pending_task("marketing", "环保营销自动续期（12 小时）", now + 30,
                          {"campaign": "eco"})


_AIRCRAFT_OWNED_TASK_KINDS = {
    "takeoff", "takeoff_reconcile", "retrofit", "delivery_continue",
}


def _removed_aircraft_guard(task: dict) -> bool:
    """飞机已从机队移除时，阻止并发中的旧任务恢复为待执行状态。"""
    params = task.get("params") or {}
    if not params.get("aircraft_removed"):
        return False
    task["status"] = "cancelled"
    task["error"] = "飞机已售出或移除，停止自动运营"
    task.pop("retry", None)
    return True


def _cancel_removed_aircraft_tasks(removed: list[dict]) -> int:
    """取消已售出飞机的所有自动任务，并关闭对应建设记录。"""
    fid_to_reg = {
        str(item.get("飞机ID", "")).strip():
            str(item.get("注册号", "")).strip().upper()
        for item in removed
        if (isinstance(item, dict) and item.get("飞机ID") and item.get("注册号"))
    }
    regs = {
        str(item.get("注册号", "")).strip().upper()
        for item in removed if isinstance(item, dict) and item.get("注册号")
    }
    fids = {
        str(item.get("飞机ID", "")).strip()
        for item in removed if isinstance(item, dict) and item.get("飞机ID")
    }
    if not regs and not fids:
        return 0
    cancelled: list[tuple[str, str]] = []
    with _pending_lock:
        for task in _pending_tasks:
            if (task.get("kind") not in _AIRCRAFT_OWNED_TASK_KINDS
                    or task.get("status") not in {"pending", "running", "failed"}):
                continue
            params = task.get("params") or {}
            reg = str(params.get("reg", "")).strip().upper()
            fid = str(params.get("fid", "")).strip()
            if not ((reg and reg in regs) or (fid and fid in fids)):
                continue
            params["aircraft_removed"] = True
            task["params"] = params
            _removed_aircraft_guard(task)
            canonical_reg = fid_to_reg.get(fid) or reg
            cancelled.append((canonical_reg, str(task.get("kind", ""))))
        if cancelled:
            _save_pending_tasks()
    # 已建线飞机售出后不应再由 builds.csv 合并回机队页面，也不应继续
    # 占用候选航线排除集合。
    for reg in regs:
        _mark_build(reg, status="sold")
    if cancelled:
        per_reg: dict[str, int] = {}
        for reg, _kind in cancelled:
            per_reg[reg] = per_reg.get(reg, 0) + 1
        for reg, count in sorted(per_reg.items()):
            _publish_log(
                f"🗑️ {reg} 已移除，已取消 {count} 项自动任务"
            )
    return len(cancelled)


def _sync_account_context() -> bool:
    """空闲时原子切换账号状态；运行中或在线操作中保持原账号不变。"""
    global _active_account_email, _active_account_password, _active_account_key
    global _pending_tasks, _pending_seq, _market_rt_cache, _market_rt_ts
    global _market_rt_failures, _market_rt_retry_after, _market_rt_last_error, _maint_cache
    global _home_status_cache, _home_status_ts

    desired_email, desired_password = _current_env_credentials()
    with _account_lock:
        if normalize_account(desired_email) == normalize_account(_active_account_email):
            _active_account_password = desired_password
            return True
    with _run_lock:
        if _run_status.get("running"):
            return False
    if not _online_session_lock.acquire(blocking=False):
        return False
    try:
        # 与新增/保存待办保持相同锁顺序：pending → account，避免互相等待。
        with _pending_lock, _account_lock:
            # 等锁期间配置可能再次变化，重新读取最终目标。
            desired_email, desired_password = _current_env_credentials()
            if normalize_account(desired_email) == normalize_account(_active_account_email):
                _active_account_password = desired_password
                return True
            old_paths = _paths_for_account(_active_account_email)
            _save_pending_tasks(old_paths["pending"])
            _active_account_email = desired_email
            _active_account_password = desired_password
            _active_account_key = account_key(desired_email)
            _load_pending_tasks(
                _paths_for_account(desired_email)["pending"], _active_account_key)
            with _market_rt_lock:
                _market_rt_cache = None
                _market_rt_ts = 0.0
                _market_rt_failures = 0
                _market_rt_retry_after = 0.0
                _market_rt_last_error = ""
            with _maint_cache_lock:
                _maint_cache = None
                _home_status_cache = None
                _home_status_ts = 0.0
        _broadcast_sse({"type": "account", "account": desired_email})
        return True
    finally:
        _online_session_lock.release()


def _add_pending_task(kind: str, title: str, trigger_at: float, params: dict,
                      jitter: float = 0.0) -> dict:
    """新增一条待定任务（在 trigger_at 时刻由后台调度器执行）。

    jitter：在 [0, jitter] 秒内随机抖动触发时间，避免多个任务同时打到游戏。
    """
    global _pending_seq
    if jitter > 0:
        trigger_at += random.uniform(0, jitter)
    owner = _active_account_key
    with _pending_lock:
        # 玩家可能连续点击建设、刷新任务或重启服务；同一业务动作只保留一条。
        identity_keys = {
            "takeoff": ("route_id", "reg"),
            "takeoff_reconcile": ("route_id", "reg"),
            "retrofit": ("route_id", "reg"),
            "delivery_continue": ("fid", "reg"),
            "marketing": ("campaign",),
        }.get(kind, ())
        if identity_keys:
            identity = tuple(str(params.get(k, "")) for k in identity_keys)
            for old in _pending_tasks:
                old_identity = tuple(str((old.get("params") or {}).get(k, ""))
                                     for k in identity_keys)
                if (old.get("kind") == kind and old.get("status") in ("pending", "running")
                        and identity == old_identity):
                    previous_trigger = float(old.get("trigger_at", trigger_at))
                    if old.get("status") == "pending":
                        if kind == "takeoff":
                            old_params = old.get("params") or {}
                            old_ready = float(old_params.get("ready_at", 0) or 0)
                            new_ready = float(params.get("ready_at", 0) or 0)
                            if old_ready > 0 and new_ready > 0 and old_ready > new_ready:
                                # 落地、维护和改装是并列就绪约束；保留其中最晚者。
                                trigger_at = max(float(old.get("trigger_at", trigger_at)), trigger_at)
                                params = {**params, "ready_at": old_ready,
                                          "reason": old_params.get("reason", params.get("reason", ""))}
                                title = old.get("title", title)
                            old["trigger_at"] = trigger_at
                        else:
                            old["trigger_at"] = min(
                                float(old.get("trigger_at", trigger_at)), trigger_at)
                        old["title"] = title
                        old["params"] = params
                        old["created_at"] = time.time()
                        if kind == "takeoff" and params.get("reason") == "全量扫描发现":
                            old.pop("retry", None)
                            old["error"] = None
                        _save_pending_tasks()
                    result = dict(old)
                    result["deduplicated"] = True
                    result["trigger_changed"] = abs(
                        float(old.get("trigger_at", previous_trigger)) - previous_trigger
                    ) > 1
                    return result
        _pending_seq += 1
        task = {
            "id": f"t{_pending_seq}",
            "account": owner,
            "kind": kind,
            "title": title,
            "trigger_at": trigger_at,
            "status": "pending",  # pending / running / done / failed / cancelled
            "created_at": time.time(),
            "params": params,
            "error": None,
        }
        _pending_tasks.append(task)
        _save_pending_tasks()
    result = dict(task)
    result["deduplicated"] = False
    result["trigger_changed"] = True
    return result


def _schedule_takeoff(route_id: str, reg: str, cost_index: int,
                      fid: str = "", hub_id: str = "",
                      delay: float = _TAKEOFF_READY_BUFFER_SECONDS) -> None:
    """把起飞排入待办任务：默认在飞机就绪 2 分钟后自动执行。"""
    now = time.time()
    _add_takeoff_task(
        reg, route_id, cost_index, now + delay,
        f"{reg} 准备起飞（航线 {route_id}）", fid=fid, hub_id=hub_id,
        ready_at=now + max(0, delay - _TAKEOFF_READY_BUFFER_SECONDS),
        reason="改装完成",
    )


def _add_takeoff_task(reg: str, route_id: str | None, cost_index: int,
                      trigger_at: float, title: str, jitter: float = 0.0,
                      fid: str = "", hub_id: str = "", ready_at: float = 0.0,
                      reason: str = "") -> dict:
    return _add_pending_task(
        "takeoff", title, trigger_at,
        {"route_id": route_id, "reg": reg, "cost_index": int(cost_index),
         "fid": str(fid or ""), "hub_id": str(hub_id or ""),
         "ready_at": float(ready_at or 0), "reason": reason},
        jitter=jitter,
    )


def _add_retrofit_task(reg: str, route_id: str | None, cost_index: int,
                       trigger_at: float, title: str, jitter: float = 45.0,
                       fid: str = "", hub_id: str = "", retrofit: str | None = "all",
                       economy: str = "", business: str = "0", first: str = "0",
                       cargo_l: str = "", cargo_h: str = "") -> None:
    _add_pending_task("retrofit", title, trigger_at,
                      {"route_id": route_id, "reg": reg, "cost_index": int(cost_index),
                       "fid": str(fid or ""), "hub_id": str(hub_id or ""),
                       "retrofit": retrofit, "economy": economy,
                       "business": business, "first": first,
                       "cargo_l": cargo_l, "cargo_h": cargo_h},
                      jitter=jitter)


def _preschedule_retrofit(reg: str, arr_id: str, cost_index: int, trigger_at: float,
                          fid: str = "", hub_id: str = "", retrofit: str | None = "all",
                          economy: str = "", business: str = "0", first: str = "0",
                          cargo_l: str = "", cargo_h: str = "") -> None:
    """下单买机时同步预排改装任务：建线完成后自动填充航线 ID 并执行，再排起飞。"""
    _add_retrofit_task(reg, None, cost_index, trigger_at,
                       f"{reg} 将在航线建设后进行改装（→ 机场{arr_id}）",
                       jitter=120, fid=fid, hub_id=hub_id, retrofit=retrofit,
                       economy=economy, business=business, first=first,
                       cargo_l=cargo_l, cargo_h=cargo_h)


def _create_retrofit_task(reg: str, route_id: str, cost_index: int,
                          fid: str, hub_id: str, retrofit: str | None,
                          economy: str, business: str, first: str,
                          cargo_l: str = "", cargo_h: str = "") -> None:
    """已交付飞机建线后：直接创建改装任务（建线已完成）。"""
    with _pending_lock:
        changed = False
        for old in _pending_tasks:
            if (old.get("kind") == "retrofit"
                    and str((old.get("params") or {}).get("reg", "")).upper() == str(reg).upper()
                    and old.get("status") == "failed"):
                old["status"] = "superseded"
                changed = True
        if changed:
            _save_pending_tasks()
    _add_retrofit_task(reg, route_id, cost_index, time.time() + 15,
                       f"{reg} 将在航线建设后进行改装（航线 {route_id}）",
                       fid=fid, hub_id=hub_id, retrofit=retrofit,
                       economy=economy, business=business, first=first,
                       cargo_l=cargo_l, cargo_h=cargo_h)


def _arm_retrofit(reg: str, route_id: str, cost_index: int,
                  fid: str = "", hub_id: str = "", retrofit: str | None = "all",
                  economy: str = "", business: str = "0", first: str = "0",
                  cargo_l: str = "", cargo_h: str = "") -> None:
    """建线完成：给预排的改装任务填充航线 ID，并把触发时间校准到 15~60 秒后。"""
    with _pending_lock:
        for t in _pending_tasks:
            if (t.get("kind") == "retrofit" and t.get("status") == "pending"
                    and not t.get("params", {}).get("route_id")
                    and t.get("params", {}).get("reg") == reg):
                t["params"]["route_id"] = route_id
                t["params"]["cost_index"] = int(cost_index)
                if fid:
                    t["params"]["fid"] = str(fid)
                if hub_id:
                    t["params"]["hub_id"] = str(hub_id)
                t["title"] = f"{reg} 将在航线建设后进行改装（航线 {route_id}）"
                t["trigger_at"] = time.time() + 15 + random.uniform(0, 45)
                _save_pending_tasks()
                return
    # 没有预排任务（例如直接对已交付飞机建设）则现建一条
    _create_retrofit_task(reg, route_id, cost_index, fid, hub_id, retrofit,
                          economy, business, first, cargo_l, cargo_h)


def _fail_takeoff(reg: str, reason: str) -> None:
    """建线失败：取消该飞机的预排起飞任务。"""
    with _pending_lock:
        for t in _pending_tasks:
            if (t.get("kind") == "takeoff" and t.get("status") == "pending"
                    and not t.get("params", {}).get("route_id")
                    and t.get("params", {}).get("reg") == reg):
                t["status"] = "failed"
                t["error"] = reason
                _save_pending_tasks()
                return


def _retrofit_mods(value) -> set[str]:
    if not value:
        return set()
    if str(value).strip().lower() in {"all", "全部"}:
        return {"co2", "speed", "fuel"}
    return {item.strip().lower() for item in str(value).replace(" ", "").split(",")
            if item.strip()}


def _fleet_retrofit_satisfied(reg: str, retrofit) -> bool:
    wanted = _retrofit_mods(retrofit)
    if not wanted:
        return True
    row = next((item for item in _read_csv(_paths()["fleet"])
                if item.get("注册号", "").strip().upper() == str(reg).strip().upper()), None)
    if not row:
        return False
    fields = {"co2": "CO2减排放", "speed": "飞行速度增加", "fuel": "耗油量减少"}
    if not wanted.issubset(fields):
        return False
    return all(row.get(fields[mod]) == "已改装" for mod in wanted)


def _retrofit_blocks_takeoff(reg: str) -> str | None:
    """要求的改装未成功时，阻止全量扫描绕过建设前置条件。"""
    target = str(reg or "").strip().upper()
    reconciled = False
    with _pending_lock:
        for task in _pending_tasks:
            params = task.get("params") or {}
            if (task.get("kind") == "retrofit"
                    and str(params.get("reg", "")).strip().upper() == target
                    and params.get("retrofit")):
                if task.get("status") in {"pending", "running"}:
                    return "要求的改装尚未成功"
                if task.get("status") == "failed":
                    if not _fleet_retrofit_satisfied(reg, params.get("retrofit")):
                        return "要求的改装尚未成功"
                    task["status"] = "done"
                    task["error"] = None
                    reconciled = True
    for build in _load_builds():
        if (str(build.get("reg", "")).strip().upper() == target
                and build.get("status") == "retrofit_failed"):
            if not _fleet_retrofit_satisfied(reg, build.get("retrofit", "all")):
                return "改装失败状态尚未解除"
            _mark_build(reg, status="routed")
            reconciled = True
    if reconciled:
        with _pending_lock:
            _save_pending_tasks()
        _publish_log(f"🔧 {reg} 所需改装已完成，解除起飞阻断")
    return None


def _defer_online_failure(task: dict, category: str, message: str) -> bool:
    """有限指数退避；运营任务交给每日发现，建设任务保留低频恢复。"""
    state = task.setdefault("retry", {})
    if state.get("category") != category:
        state.clear()
        state["category"] = category
        state["attempts"] = 0
    state["attempts"] = int(state.get("attempts", 0)) + 1
    attempts = state["attempts"]
    delays = (300, 900, 1800)
    if attempts > len(delays):
        if task.get("kind") in {"delivery_continue", "retrofit", "marketing"}:
            delay = 6 * 3600
            task["trigger_at"] = time.time() + delay
            task["status"] = "pending"
            task["error"] = f"{message}\n连续失败 {attempts} 次，改为每 6 小时重试"
            _publish_log(f"⏳ {_pending_log_label(task)}\n   {task['error']}")
            return True
        task["status"] = "done"
        task["error"] = f"{message}\n连续失败 {attempts} 次，暂停至每日 06:00 重新评估"
        _publish_log(f"⏸️ {_pending_log_label(task)}\n   {task['error']}")
        return False
    delay = delays[attempts - 1]
    task["trigger_at"] = time.time() + delay
    task["status"] = "pending"
    task["error"] = f"{message}\n第 {attempts} 次，{delay // 60} 分钟后重试"
    _publish_log(f"⏳ {_pending_log_label(task)}\n   {task['error']}")
    return True


def _pending_log_label(task: dict) -> str:
    """给移动端日志提供短而明确的任务名称。"""
    params = task.get("params") or {}
    reg = str(params.get("reg", "")).strip()
    if task.get("kind") == "takeoff" and reg:
        return f"{reg} 起飞"
    return str(task.get("title") or "待办")


def _defer_retrofit_confirmation(task: dict, reg: str, route_id: str, message: str) -> None:
    """改装写请求已提交后的只读确认退避；不再调用改装写流程。"""
    state = task.setdefault("retry", {})
    if state.get("category") != "retrofit_confirm":
        state.clear()
        state.update({"category": "retrofit_confirm", "attempts": 0})
    state["attempts"] = int(state.get("attempts", 0)) + 1
    attempts = state["attempts"]
    delays = (300, 900, 1800)
    delay = delays[attempts - 1] if attempts <= len(delays) else 6 * 3600
    task["trigger_at"] = time.time() + delay
    task["status"] = "pending"
    wait_text = f"{delay // 60} 分钟后" if delay < 3600 else "6 小时后"
    task["error"] = f"改装已提交，{message}；{wait_text}再次确认"
    _publish_log(
        f"🔧 {reg}（航线 {route_id}）的改装请求已提交，{message}；"
        f"{wait_text}再次确认"
    )


def _defer_for_market(task: dict, message: str) -> None:
    """燃油状态不允许起飞时只等本地下次市场轮次，不产生游戏请求。"""
    now = _now_bjt()
    if now.minute < 30:
        nxt = now.replace(minute=30, second=0, microsecond=0)
    else:
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    task["trigger_at"] = nxt.timestamp() + _TAKEOFF_READY_BUFFER_SECONDS
    task["status"] = "pending"
    task["error"] = message
    _publish_log(f"⛽ {task.get('title', '起飞待办')}：{message}")


@_serialized_online
def _run_pending_task(task: dict) -> None:
    """执行一条到期的待定任务；未就绪的任务会顺延。"""
    owner = str(task.get("account") or _active_account_key)
    task["account"] = owner
    if owner != _active_account_key:
        task["status"] = "cancelled"
        task["error"] = "账号已切换，旧账号任务未执行"
        return
    kind = task.get("kind")
    params = task.get("params") or {}
    if _removed_aircraft_guard(task):
        return
    rp = None

    def online_planner():
        nonlocal rp
        if rp is None:
            rp = _get_route_planner(require_login=True)
        return rp

    if kind == "marketing":
        campaign = str(params.get("campaign", ""))
        if campaign not in {"airline", "eco"}:
            task["status"] = "failed"
            task["error"] = f"未知营销类型：{campaign or '空'}"
            return
        state_key = "ad4_24h" if campaign == "airline" else "eco_12h"
        label = "广告 4（24 小时）" if campaign == "airline" else "环保营销（12 小时）"
        _get_route_planner(require_login=True)  # 配置当前账号的服务端 Cookie 会话
        import collector as ext
        page = ext.fetch(ext.MARKETING, referer=ext.HOME)
        if not ext._valid_marketing_page(page):
            _defer_online_failure(task, "marketing", f"{label}页面暂时无法确认，未执行购买")
            return
        remaining = int(ext._parse_active_marketing(page).get(campaign, 0) or 0)
        if remaining > 0:
            task["trigger_at"] = time.time() + remaining + 60
            task["status"] = "pending"
            task["error"] = None
            return
        ok, message, renewed = ext._purchase_marketing(state_key, label, known_inactive=True)
        if not ok:
            _defer_online_failure(task, "marketing", f"{label}：{message}")
            return
        _publish_log(f"📣 {message}，已按活动到期时间安排下次续期")
        _refresh_market_after_spend()
        task["trigger_at"] = time.time() + max(300, renewed or (86400 if campaign == "airline" else 43200)) + 60
        task["status"] = "pending"
        task["error"] = None
        task.pop("retry", None)
        return

    if kind == "takeoff_reconcile":
        reg = str(params.get("reg", ""))
        route_id = str(params.get("route_id", ""))
        planner = online_planner()
        now = time.time()
        started_at = float(params.get("started_at", 0) or 0)
        ready_at = 0.0
        duration = 0

        # 主页在飞机起飞后会给出预计落地时间；优先用它恢复首航时长，
        # 只需共享主页请求，不会误发第二次起飞。
        latest = _latest_home_maintenance(planner, reg)
        if latest is None:
            _defer_online_failure(task, "takeoff_reconcile", f"无法确认 {reg} 的首航状态")
            return
        try:
            arrived = float(latest.get("预计落地时间戳", 0) or 0)
        except (TypeError, ValueError):
            arrived = 0.0
        if started_at > 0 and arrived > started_at:
            duration = max(1, int(round(arrived - started_at)))
            ready_at = arrived

        # 若主页已不再展示该次飞行，详情页可能已经生成稳定航程时长。
        if duration <= 0:
            refreshed = _refresh_fleet_row(
                reg, str(params.get("fid", "")), str(params.get("hub_id", "")))
            duration = _flight_duration_seconds((refreshed or {}).get("飞行时长", ""))
            if duration > 0:
                ready_at = max(now, started_at + duration if started_at > 0 else now)
        if duration <= 0:
            _defer_online_failure(task, "takeoff_reconcile", f"{reg} 的首航时长尚未获取")
            return

        _set_fleet_duration_if_missing(reg, duration)
        final_row = _refresh_fleet_row(
            reg, str(params.get("fid", "")), str(params.get("hub_id", "")),
            finalize_build=True,
        )
        if final_row:
            _mark_build(reg, status="done")
        _add_takeoff_task(
            reg, route_id, int(params.get("cost_index", 200)),
            max(now + _TAKEOFF_READY_BUFFER_SECONDS,
                ready_at + _TAKEOFF_READY_BUFFER_SECONDS),
            f"飞机 {reg} 下次起飞（航线 {route_id}）",
            fid=params.get("fid", ""), hub_id=params.get("hub_id", ""),
            jitter=0, ready_at=ready_at, reason="本次飞行落地",
        )
        task["status"] = "done"
        task["error"] = None
        task.pop("retry", None)
        _publish_log(f"{reg} 首航时长已确认，下一班已登记")
        return

    if kind == "delivery_continue":
        fid = str(params.get("fid", ""))
        reg = params.get("reg", "")
        planner = online_planner()
        if not fid:
            try:
                fid = planner._fleet_aircraft_id(
                    str((params.get("aircraft") or {}).get("id", "")), reg)
            except planner.FleetLookupError as e:
                _defer_online_failure(
                    task, "fleet_lookup", f"{reg} 机队状态查询失败，未重复购机：{e}")
                return
            if not fid:
                checks = int(params.get("fleet_absent_checks", 0) or 0) + 1
                params["fleet_absent_checks"] = checks
                if checks < 3:
                    task["trigger_at"] = time.time() + checks * 600
                    task["status"] = "pending"
                    task["error"] = f"未在机队中发现 {reg}，将再次进行确认（{checks}/3）"
                else:
                    task["status"] = "failed"
                    task["error"] = f"已连续三次未在机队中发现 {reg}；请手动重新发起建设"
                    _mark_build(reg, status="failed")
                    _publish_log(f"🏗 {task['error']}")
                return
            params["fid"] = str(fid)
            params.pop("fleet_absent_checks", None)
            task.pop("retry", None)
            _mark_build(reg, fid=str(fid), status="delivering")
            _publish_log(f"🏗 已恢复{reg}（ID {fid}），继续进行交付确认")
        delivered, remain = planner._delivery_status(fid)
        if delivered is not True:
            # 交付倒计时还没走完（估算偏差），顺延后再查
            if delivered is None:
                _defer_online_failure(
                    task, "delivery", f"{reg} 交付状态查询失败，不执行建线")
                return
            delay = max(300, min(remain or 600, 1800))
            task["trigger_at"] = time.time() + delay
            task["status"] = "pending"
            task["error"] = f"交付未完成（剩余约 {max(1, remain // 60)} 分钟），已顺延"
            return
        _publish_log(
            f"🏗 [{reg}] 交付完成，继续建设：机队ID {fid} → 机场ID {params.get('arr_id', '')}"
        )
        res = planner.build_route(
            params["aircraft"], params["hub_id"], params["arr_id"], reg,
            economy=params.get("economy"), business=params.get("business", 0),
            first=params.get("first", 0), engine=params.get("engine"),
            cargo_l=params.get("cargo_l"), cargo_h=params.get("cargo_h"),
            amount=params.get("amount", 1),
            cost_index=params.get("cost_index", 200),
            origin_airport_id=params.get("origin_airport_id"),
            retrofit=params.get("retrofit", "all"),  # 默认改装；null 表示跳过
            confirmed_fid=fid,
            delivery_confirmed=True,
            after_spend=_refresh_market_after_spend,
        )
        for s in res.get("steps", []):
            _publish_log(_route_step_log(reg, s))
        if res.get("waiting_route_lookup"):
            _defer_online_failure(
                task, "route_lookup", f"{reg} 已有航线状态未能恢复，未执行重复建线")
            return
        if res.get("waiting_delivery"):
            task["trigger_at"] = time.time() + max(300, int(res.get("remain_sec", 300) or 300))
            task["status"] = "pending"
            task["error"] = "交付状态再次变为未就绪，已保留任务并顺延"
            return
        if res.get("route_id"):
            _arm_retrofit(reg, res["route_id"], params.get("cost_index", 200),
                          fid=params.get("fid", ""), hub_id=params.get("hub_id", ""),
                          retrofit=params.get("retrofit", "all"),
                          economy=params.get("economy", ""),
                          business=params.get("business", "0"),
                          first=params.get("first", "0"),
                          cargo_l=params.get("cargo_l", ""),
                          cargo_h=params.get("cargo_h", ""))
            _mark_build(reg, status="routed", route_id=res["route_id"])
        else:
            _fail_takeoff(reg, "建线未完成，起飞任务取消")
            _mark_build(reg, status="failed")
        task["status"] = "done"
        task["error"] = None
        return

    if kind == "retrofit":
        reg = params.get("reg", "")
        route_id = str(params.get("route_id", ""))
        if not route_id:
            # 航线还没建好，顺延再查
            task["trigger_at"] = time.time() + 120
            task["status"] = "pending"
            task["error"] = "等待建线完成，已顺延"
            return
        fid = str(params.get("fid", ""))
        retrofit = params.get("retrofit")
        want = None
        if retrofit:
            mods = {"all": {"co2", "speed", "fuel"}}
            want = mods.get(str(retrofit).lower()) or {
                x.strip().lower()
                for x in str(retrofit).lower().replace(" ", "").split(",") if x.strip()}
        if want and not want.issubset({"co2", "speed", "fuel"}):
            task["status"] = "failed"
            task["error"] = f"改装配置无效：{retrofit}"
            _mark_build(reg, status="retrofit_failed")
            _publish_log(f"🔧 {reg} 改装失败：{task['error']}，未安排起飞")
            return
        install = 0
        if want and not fid:
            for build in _load_builds():
                if build.get("reg", "").upper() == str(reg).upper():
                    fid = str(build.get("fid", "") or "")
                    if fid:
                        params["fid"] = fid
                    break
        if want and not fid:
            task["status"] = "failed"
            task["error"] = "要求改装但缺少机队 ID"
            _mark_build(reg, status="retrofit_failed")
            _publish_log(f"🔧 {reg} 改装失败：{task['error']}，未安排起飞")
            return
        if want:
            planner = online_planner()
            confirming = bool(params.get("retrofit_submitted"))
            if confirming:
                rf = {"ok": False, "retryable": True,
                      "msg": "主页尚未确认改装待定时间"}
            else:
                rf = planner._apply_retrofit(
                    fid, params.get("economy", 0), params.get("business", 0),
                    params.get("first", 0), want,
                    cargo_l=params.get("cargo_l"), cargo_h=params.get("cargo_h"))
            if _removed_aircraft_guard(task):
                return
            # 改装写请求一旦已经发出，优先只补读一个主页。主页 statusData 的
            # maintEnd 就是“待定”栏结束时间：只要出现，就足以证明游戏已接受
            # 改装，无需再轮询暂时为空的改装页。起飞任务本身仍会在到点时用
            # 最新主页状态兜底，维护/改装尚未结束就继续顺延。
            if confirming or (not rf.get("ok") and rf.get("submitted")):
                home_status = _latest_home_maintenance(
                    planner, reg, force_refresh=True)
                home_ready_at = _home_maintenance_ready_at(home_status)
                if home_ready_at > time.time():
                    rf = {
                        "ok": True,
                        "msg": "主页待定栏已确认改装安排",
                        "install_secs": max(0.0, home_ready_at - time.time()),
                    }
                elif confirming:
                    # 旧任务或主页尚未同步时才退回一次只读改装页确认；绝不重发写请求。
                    rf = planner._confirm_retrofit(fid, want)
            if not rf.get("ok"):
                reason = str(rf.get("msg") or "改装失败")
                if confirming:
                    _defer_retrofit_confirmation(task, reg, route_id, reason)
                elif rf.get("submitted"):
                    # 游戏在改装安装期间可能不再返回改装表单。这不是在线失败：
                    # 按页面给出的安装时长等待，完成后再做一次只读确认，避免
                    # 5/15/30 分钟重试风暴以及把成功提交显示成“改装失败”。
                    install = float(rf.get("install_secs", 0) or 0)
                    wait_seconds = int(max(
                        _TAKEOFF_READY_BUFFER_SECONDS,
                        install + _TAKEOFF_READY_BUFFER_SECONDS,
                    ))
                    ready_at = time.time() + wait_seconds
                    params["retrofit_submitted"] = True
                    params["retrofit_ready_at"] = ready_at
                    task["trigger_at"] = ready_at
                    task["status"] = "pending"
                    task["error"] = f"{reason}；约 {max(1, (wait_seconds + 59) // 60)} 分钟后确认"
                    task.pop("retry", None)
                    _publish_log(
                        f"🔧 {reg}（航线 {route_id}）的改装请求已提交\n"
                        f" 等待约 {max(1, (wait_seconds + 59) // 60)} 分钟后确认完成状态"
                    )
                elif rf.get("retryable"):
                    _defer_online_failure(task, "retrofit", f"改装 {reg} 失败：{reason}")
                else:
                    task["status"] = "failed"
                    task["error"] = reason
                    _mark_build(reg, status="retrofit_failed")
                    _publish_log(f"🔧 {reg}（航线 {route_id}）改装失败：{reason}，未安排起飞")
                return
            install = float(rf.get("install_secs", 0) or 0)
            params.pop("retrofit_submitted", None)
            params.pop("retrofit_ready_at", None)
            task.pop("retry", None)
            _mark_build(reg, status="routed")
            _publish_log(f"🔧 {reg}（航线 {route_id}）改装完成: ✓ {rf.get('msg', '')}")
            task["error"] = None
        else:
            msg = f"🔧 {reg} 未要求改装，跳过"
            _append_log(msg)
            _broadcast_sse({"type": "log", "line": msg})
            task["error"] = None
        task["status"] = "done"
        # 改装完成（按真实安装时间 + 2 分钟缓冲）后排起飞；若飞机还没就绪，5 分钟后重试
        _schedule_takeoff(route_id, reg, params.get("cost_index", 200),
                          fid=fid, hub_id=params.get("hub_id", ""),
                          delay=max(_TAKEOFF_READY_BUFFER_SECONDS,
                                    install + _TAKEOFF_READY_BUFFER_SECONDS))
        return

    if kind == "takeoff":
        route_id = str(params.get("route_id", ""))
        if not route_id:
            # 航线还没建好（交付/建线耗时超过预估），顺延再查
            task["trigger_at"] = time.time() + 120
            task["status"] = "pending"
            task["error"] = "等待建线完成，已顺延"
            return
        ci = int(params.get("cost_index", 200))
        reg = params.get("reg", "")
        retrofit_block = _retrofit_blocks_takeoff(reg)
        if retrofit_block:
            task["status"] = "cancelled"
            task["error"] = retrofit_block
            _publish_log(
                f"🔧 取消 {reg} 的起飞待办（航线 {route_id}）：{retrofit_block}"
            )
            return
        # 燃油门槛：少于阈值时不发起起飞请求。
        try:
            from collector import MIN_FUEL_FOR_TAKEOFF as _MIN_FUEL
        except Exception:
            _MIN_FUEL = 200000
        fuel = _current_fuel_lbs()
        if fuel is None:
            _defer_for_market(task, "燃油状态未知")
            return
        if fuel < _MIN_FUEL:
            _defer_for_market(task, f"燃油不足（{int(fuel):,} Lbs < {_MIN_FUEL:,}）")
            return
        # 每个待办只刷新本机详情，以最新需求决定是否起飞；不再依赖四小时全机队扫描。
        planner = online_planner()
        latest_status = _latest_home_maintenance(planner, reg)
        if latest_status is None:
            _defer_online_failure(task, "takeoff", f"起飞前无法确认 {reg} 的最新检修状态")
            return
        maintenance_ready_at = _home_maintenance_ready_at(latest_status)
        if maintenance_ready_at > time.time():
            # 主页 statusData 已包含批量检修/改装的 maintEnd；直接复用这一份
            # 状态顺延，不访问维护面板，也不浪费一次单机详情请求。
            trigger_at = maintenance_ready_at + _TAKEOFF_READY_BUFFER_SECONDS
            params["ready_at"] = maintenance_ready_at
            params["reason"] = "返场结束"
            task["title"] = f"飞机 {reg} 返场结束后接管起飞（航线 {route_id}）"
            task["trigger_at"] = trigger_at
            task["status"] = "pending"
            task["error"] = None
            task.pop("retry", None)
            remaining_minutes = max(1, int((trigger_at - time.time() + 59) // 60))
            _publish_log(
                f"🔧 {reg} 已排定维护/改装\n"
                f" 顺延到完成后约 {remaining_minutes} 分钟再检查需求并尝试起飞"
            )
            return
        if str(latest_status.get("停飞", "0") or "0").strip() not in {"", "0", "false", "False"}:
            task["status"] = "done"
            task["error"] = None
            task["completed_at"] = time.time()
            task.pop("retry", None)
            _publish_log(f"{reg} 已人工停飞，自动起飞待办已结束。")
            return
        refreshed = _refresh_fleet_row(
            reg, str(params.get("fid", "")), str(params.get("hub_id", "")))
        if _removed_aircraft_guard(task):
            return
        if not refreshed:
            _defer_online_failure(task, "takeoff", f"起飞前无法刷新 {reg} 的详情")
            return
        for key in ("距A-Check小时", "损坏率%"):
            if str(latest_status.get(key, "")).strip():
                refreshed[key] = latest_status[key]
        latest_maint_reason = _takeoff_maintenance_block(reg, row=refreshed)
        if latest_maint_reason:
            task["status"] = "failed"
            task["error"] = f"检修保护：{latest_maint_reason}"
            _publish_log(
                f"🛡️ 检修保护：已终止 {reg}（航线 {route_id}）的起飞安排\n"
                f" {latest_maint_reason}"
            )
            return
        demand_status = str(refreshed.get("需求状态", "")).strip()
        if demand_status not in {"旺盛", "不足"}:
            _defer_online_failure(task, "takeoff", f"{reg} 的需求状态无法确认")
            return
        if demand_status == "不足":
            task["status"] = "done"
            task["error"] = None
            _publish_log(f"⏸️ {reg} 需求不足，将等待需求重置。")
            return
        try:
            planner = online_planner()
            resp = planner.takeoff_route(route_id, ci)
        except Exception as e:
            _defer_online_failure(
                task, "takeoff", f"起飞请求异常：{reg}（航线 {route_id}）：{e}")
            return
        response_state = planner.classify_takeoff_response(resp)
        if _removed_aircraft_guard(task):
            return
        if response_state != "accepted":
            response_label = {
                "not_ready": "游戏暂不允许起飞（not_ready）",
                "rejected": "游戏拒绝起飞（rejected）",
                "unknown": "起飞响应无法确认（unknown）",
            }.get(response_state, f"起飞响应无法确认（{response_state}）")
            _defer_online_failure(
                task, "takeoff", response_label)
            return
        task["status"] = "done"
        task["error"] = None
        task["completed_at"] = time.time()
        task.pop("retry", None)
        # 起飞响应确认后再读一次本机，提交航班号、起降机场、时长与最新需求，
        # 并由该真实刷新清除主表中的“建设中”。
        final_row = _refresh_fleet_row(
            reg, str(params.get("fid", "")), str(params.get("hub_id", "")),
            finalize_build=True,
        )
        if final_row:
            _mark_build(reg, status="operating")
        # 起飞前已经读取了最新详情；该时长是本次飞行从起飞到落地的单程时长。
        duration = _flight_duration_seconds((final_row or refreshed).get("飞行时长", ""))
        if duration > 0:
            # 接管后的每次成功起飞都继续排下一班，形成持续运营闭环。
            ready_at = time.time() + duration
            _add_takeoff_task(
                reg, route_id, ci,
                ready_at + _TAKEOFF_READY_BUFFER_SECONDS,
                f"{reg} 下次起飞（航线 {route_id}）",
                fid=params.get("fid", ""), hub_id=params.get("hub_id", ""),
                jitter=0, ready_at=ready_at, reason="本次飞行落地",
            )
        else:
            # 新建航线在首航前可能暂时返回 00:00:00。使用独立的只读任务
            # 对账预计落地时间，避免重复起飞，并保证首航后运营链不断掉。
            _add_pending_task(
                "takeoff_reconcile",
                f"检查 {reg} 首航时长（航线 {route_id}）",
                time.time() + 300,
                {"route_id": route_id, "reg": reg, "cost_index": ci,
                 "fid": str(params.get("fid", "")),
                 "hub_id": str(params.get("hub_id", "")),
                 "started_at": float(task["completed_at"])},
                jitter=0,
            )
        msg = f"🛫 {reg} 已放行（航线 {route_id}，CI {ci}）"
        _append_log(msg)
        _broadcast_sse({"type": "log", "line": msg})
        return

    # 未知任务类型：标记失败
    task["status"] = "failed"
    task["error"] = f"未知任务类型: {kind}"


def _pending_scheduler_loop() -> None:
    """后台调度线程：每 20 秒检查一次到期任务并执行。"""
    while True:
        try:
            _sync_account_context()
            with _pending_lock:
                due = [
                    t for t in _pending_tasks
                    if t.get("status") == "pending" and t.get("trigger_at", 0) <= time.time()
                ]
            for t in due:
                with _pending_lock:
                    if t.get("status") != "pending":
                        continue
                    t["status"] = "running"
                try:
                    _run_pending_task(t)
                except Exception as e:
                    if _is_recoverable_network_error(e):
                        retry = t.setdefault("retry", {})
                        if retry.get("category") != "network_outage":
                            retry.clear()
                            retry.update({"category": "network_outage", "attempts": 0})
                        retry["attempts"] = int(retry.get("attempts", 0)) + 1
                        delay = min(3600, 300 * (2 ** min(retry["attempts"] - 1, 4)))
                        t["status"] = "pending"
                        t["trigger_at"] = time.time() + delay + random.uniform(0, 300)
                        t["error"] = f"网络不可用，约 {max(1, delay // 60)} 分钟后错峰重试"
                        msg = f"🌐 {t.get('title')}：{t['error']}"
                    else:
                        _defer_online_failure(t, "scheduler", f"待办执行异常：{e}")
                        msg = f"⏳ {t.get('title')}：{t.get('error', e)}"
                    _append_log(msg)
                    _broadcast_sse({"type": "log", "line": msg})
                _save_pending_tasks()
                time.sleep(3)  # 多个任务同时到期时逐个执行并间隔，避免同时请求游戏
        except Exception:
            pass
        time.sleep(20)


# ===== CSV 读取 =====

def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ===== 建设记录（防重复开辟） =====
_builds_lock = threading.Lock()
_BUILD_CSV_FIELDS = [
    ("注册号", "reg"), ("机型", "aircraft"), ("枢纽ID", "hub_id"),
    ("出发机场ID", "origin_airport_id"), ("到达机场ID", "dest_airport_id"),
    ("经济舱座位", "economy"), ("商务舱座位", "business"), ("头等舱座位", "first"),
    ("大货比例", "cargo_l"), ("重货比例", "cargo_h"), ("发动机ID", "engine"),
    ("要求改装", "retrofit"),
    ("状态", "status"), ("机队ID", "fid"), ("航线ID", "route_id"),
    ("创建时间", "created_at"), ("更新时间", "updated_at"),
]


def _load_builds() -> list[dict]:
    """读取建设记录 CSV（builds.csv）；旧版 builds.json 自动迁移。"""
    p = _paths()["builds"]
    jp = p.with_suffix(".json")
    if not p.exists() and jp.exists():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                _save_builds(data)
            jp.unlink(missing_ok=True)
        except Exception:
            pass
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        out = []
        for r in rows:
            row = {}
            for zh, en in _BUILD_CSV_FIELDS:
                row[en] = r.get(zh, "")
            out.append(row)
        return out
    except Exception:
        return []


def _save_builds(builds: list[dict]) -> None:
    """原子写建设记录 CSV（builds.csv）。"""
    p = _paths()["builds"]
    tmp = p.with_suffix(".csv.tmp")
    try:
        headers = [zh for zh, _ in _BUILD_CSV_FIELDS]
        with tmp.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for b in builds:
                row = {}
                for zh, en in _BUILD_CSV_FIELDS:
                    row[zh] = b.get(en, "")
                writer.writerow(row)
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _record_build(reg: str, aircraft: str, hub_id: str, origin_airport_id: str,
                  dest_airport_id: str, status: str,
                  fid: str = "", route_id: str = "",
                  economy: str = "", business: str = "0", first: str = "0",
                  cargo_l: str = "", cargo_h: str = "", engine: str = "",
                  retrofit: str | None = "all") -> None:
    """记录/更新一次建设：按注册号 upsert，供开辟候选排除已下单/已建线航线。"""
    with _builds_lock:
        builds = _load_builds()
        target_reg = str(reg).strip().upper()
        found = next((b for b in builds
                      if str(b.get("reg", "")).strip().upper() == target_reg), None)
        now = time.time()
        if found is None:
            found = {"reg": reg, "created_at": now}
            builds.append(found)
        found.update({
            "aircraft": aircraft,
            "hub_id": hub_id,
            "origin_airport_id": origin_airport_id,
            "dest_airport_id": dest_airport_id,
            "cargo_l": cargo_l,
            "cargo_h": cargo_h,
            "engine": engine,
            "retrofit": retrofit or "",
            "status": status,
            "updated_at": now,
        })
        if fid:
            found["fid"] = str(fid)
        if route_id:
            found["route_id"] = str(route_id)
        if economy != "":
            found["economy"] = economy
            found["business"] = business
            found["first"] = first
        _save_builds(builds)
    if status not in ("failed", "cancelled", "sold"):
        _merge_build_into_fleet(found, building=status not in ("done", "operating"))
    else:
        _clear_building_fleet_row(reg, remove_placeholder=not bool(found.get("fid")))


def _mark_build(reg: str, **fields) -> None:
    """按注册号更新建设记录的字段。"""
    with _builds_lock:
        builds = _load_builds()
        target_reg = str(reg).strip().upper()
        found = next((b for b in builds
                      if str(b.get("reg", "")).strip().upper() == target_reg), None)
        if found is None:
            return
        found.update(fields)
        found["updated_at"] = time.time()
        _save_builds(builds)
    if fields.get("status") and fields.get("status") not in ("failed", "cancelled", "sold"):
        _merge_build_into_fleet(
            found, building=fields.get("status") not in ("done", "operating"))
    elif fields.get("status") in ("failed", "cancelled", "sold"):
        _clear_building_fleet_row(reg, remove_placeholder=not bool(found.get("fid")))


def _build_exclude_set() -> set[str]:
    """候选排除集合：机队已运营 + 建设中/已建线的航线（含双方向、ICAO/IATA/ID 格式）。"""
    exclude: set[str] = set()
    for row in _read_csv(_paths()["fleet"]):
        o, d = row.get("起飞机场代码", ""), row.get("到达机场代码", "")
        if o and d:
            exclude.add(f"{o}:{d}")
    try:
        import route_planner as _rp
    except Exception:
        return exclude
    for b in _load_builds():
        if b.get("status") in ("ordered", "delivering", "routed", "retrofit_failed", "done"):
            oid = b.get("origin_airport_id")
            did = b.get("dest_airport_id")
            if not oid or not did:
                continue
            oa = _rp.airport_by_id(str(oid))
            da = _rp.airport_by_id(str(did))
            if not oa or not da:
                continue
            oi = (oa.get("iata", "") or "").strip() or str(oid)
            di = (da.get("iata", "") or "").strip() or str(did)
            oc = (oa.get("icao", "") or "").strip() or str(oid)
            dc = (da.get("icao", "") or "").strip() or str(did)
            for a, b2 in ((oi, di), (di, oi), (oc, dc), (dc, oc),
                          (str(oid), str(did)), (str(did), str(oid))):
                exclude.add(f"{a}:{b2}")
    return exclude


def _hub_name_by_id(hub_id: str) -> str:
    """按枢纽 ID 反查枢纽名称（供建设记录在机队页显示）。"""
    try:
        for h in json.loads(_paths()["hubs"].read_text(encoding="utf-8")):
            if str(h.get("hub_id", "")) == str(hub_id):
                return h.get("name", "")
    except Exception:
        pass
    return ""


def _build_to_fleet_rows() -> list[dict]:
    """把建设记录转成机队行（尚未进入机队 CSV 的新飞机/新航线，机队页可见）。"""
    label = {"ordered": "已下单", "delivering": "交付中", "waiting": "等待建线",
             "routed": "改装/首航中", "retrofit_failed": "改装失败"}
    try:
        import route_planner as _rp
    except Exception:
        _rp = None
    rows = []
    for b in _load_builds():
        if b.get("status") not in label:
            continue
        reg = b.get("reg", "")
        ac = _rp.aircraft_by_name(b.get("aircraft", "")) if (_rp and b.get("aircraft")) else None
        is_cargo = bool(ac and str(ac.get("type", "0")) == "1")
        rows.append({
            "飞机ID": b.get("fid") or f"B-{reg}",
            "注册号": reg,
            "机型": b.get("aircraft", ""),
            "客机组数量": "0" if is_cargo else "1",
            "枢纽分类": _hub_name_by_id(b.get("hub_id", "")),
            "飞行时长": label.get(b.get("status", ""), b.get("status", "")),
            "CO2减排放": "", "飞行速度增加": "", "耗油量减少": "",
            "经济舱需求": "", "商务舱需求": "", "头等舱需求": "",
            "经济舱座位": "", "商务舱座位": "", "头等舱座位": "",
            "起飞机场名称": "", "到达机场名称": "", "航距km": "",
            "需求状态": "",
        })
    return rows


def _fleet_rows() -> list[dict]:
    """机队 CSV + 建设记录合并（按注册号去重），机队页/统计立即可见新建设。"""
    rows = _read_csv(_paths()["fleet"])
    have = {r.get("注册号", "").upper() for r in rows}
    # 建设中/未回填的飞机标记 _pending_build，前端黄色高亮区分
    build_regs = {b.get("reg", "").upper() for b in _load_builds()
                  if b.get("status") in ("ordered", "delivering", "waiting", "routed", "retrofit_failed")}
    for r in rows:
        if (r.get("注册号", "").upper() in build_regs
                or str(r.get("建设状态", "")).strip()):
            r["_pending_build"] = "1"
    for br in _build_to_fleet_rows():
        if br["注册号"] and br["注册号"].upper() not in have:
            br["_pending_build"] = "1"
            rows.append(br)
    with _maint_cache_lock:
        statuses = dict(_home_status_cache or {})
    status_by_reg = {
        str(item.get("注册号", "")).strip().upper(): item
        for item in statuses.values() if isinstance(item, dict)
    }
    for row in rows:
        status = status_by_reg.get(str(row.get("注册号", "")).strip().upper(), {})
        _decorate_operation_state(row, status)
    return rows


def _decorate_operation_state(row: dict, status: dict | None = None) -> dict:
    """用主页状态装饰机队行；只添加前端字段，不写入 CSV。"""
    if row.get("_pending_build") or str(row.get("建设状态", "")).strip():
        row["_operation_state"] = "building"
        return row
    status = status if isinstance(status, dict) else {}
    try:
        maintenance_end = float(status.get("维护改装结束时间戳", 0) or 0)
    except (TypeError, ValueError):
        maintenance_end = 0.0
    try:
        arrival = float(status.get("预计落地时间戳", 0) or 0)
    except (TypeError, ValueError):
        arrival = 0.0
    row["_operation_state"] = (
        "maintenance" if maintenance_end > time.time()
        else "flying" if arrival > time.time()
        else "grounded"
    )
    return row


def _write_fleet_csv(rows: list[dict], *, already_locked: bool = False) -> bool:
    """原子写回 fleet.csv（保留原表头，兼容新增字段）。"""
    p = _paths()["fleet"]

    def write_locked() -> bool:
        tmp_path: Path | None = None
        try:
            try:
                with p.open("r", encoding="utf-8-sig") as f:
                    fieldnames = next(csv.reader(f))
            except Exception:
                fieldnames = list(rows[0].keys()) if rows else []
            for row in rows:
                for key in row:
                    if not key.startswith("_") and key not in fieldnames:
                        fieldnames.append(key)
            with tempfile.NamedTemporaryFile(
                    mode="w", newline="", encoding="utf-8-sig", dir=p.parent,
                    prefix=f".{p.name}.", suffix=".tmp", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, p)
            return True
        except Exception as e:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            _append_log(f"⚠ 机队数据库更新失败：{e}")
            return False

    if already_locked:
        return write_locked()
    with exclusive_file_lock(p):
        return write_locked()


def _merge_build_into_fleet(b: dict, *, building: bool = True) -> None:
    """从购机起把已知建线信息写入主表；首航确认后清除建设标记。"""
    if not b.get("reg"):
        return
    try:
        import route_planner as _rp
        oa = _rp.airport_by_id(str(b.get("origin_airport_id", ""))) if b.get("origin_airport_id") else None
        da = _rp.airport_by_id(str(b.get("dest_airport_id", ""))) if b.get("dest_airport_id") else None
        ac = _rp.aircraft_by_name(b.get("aircraft", "")) if b.get("aircraft") else None
    except Exception:
        oa = da = ac = None
    is_cargo = bool(ac and str(ac.get("type", "0")) == "1")
    row = {
        "飞机ID": b.get("fid") or f"B-{b.get('reg', '')}",
        "注册号": b.get("reg", ""),
        "航班号": "",
        "机型": b.get("aircraft", ""),
        "建设状态": "建设中" if building else "",
        "经济舱座位": b.get("economy", ""),
        "商务舱座位": b.get("business", "0"),
        "头等舱座位": b.get("first", "0"),
        "经济舱票价": "", "商务舱票价": "", "头等舱票价": "",
        "起飞机场代码": (oa or {}).get("icao", "") if oa else "",
        "起飞机场名称": (oa or {}).get("name", "") if oa else "",
        "到达机场代码": (da or {}).get("icao", "") if da else "",
        "到达机场名称": (da or {}).get("name", "") if da else "",
        "起飞时间UTC": "", "到达时间UTC": "", "飞行时长": "00:00:00",
        "航距km": "",
        "枢纽分类": _hub_name_by_id(b.get("hub_id", "")),
        "距A-Check小时": "", "损坏率%": "",
        "CO2减排放": "未查询", "飞行速度增加": "未查询", "耗油量减少": "未查询",
        "经济舱需求": "", "商务舱需求": "", "头等舱需求": "",
        "大货需求": "", "重货需求": "", "大货容量": "", "重货容量": "",
        "需求状态": "",
        "组类型": str(ac.get("id", "")) if ac else "",
        "客机组数量": "0" if is_cargo else "1",
        "最后更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    fleet_path = _paths().get("fleet")
    if fleet_path is None:
        return
    try:
        with exclusive_file_lock(fleet_path):
            rows = _read_csv(fleet_path)
            idx = next((i for i, r in enumerate(rows)
                        if r.get("注册号", "").upper() == row["注册号"].upper()), None)
            if idx is None:
                rows.append(row)
            else:
                current = rows[idx]
                # 建设上下文是用户选定的目标，优先于交付/改装期间详情页的临时值。
                authoritative = {
                    "飞机ID", "注册号", "机型", "建设状态", "经济舱座位", "商务舱座位",
                    "头等舱座位", "起飞机场代码", "起飞机场名称", "到达机场代码",
                    "到达机场名称", "枢纽分类", "组类型", "客机组数量",
                }
                for key, value in row.items():
                    if key == "建设状态" or (key in authoritative and str(value).strip()):
                        current[key] = value
                    elif not str(current.get(key, "")).strip() and str(value).strip():
                        current[key] = value
            # 旧版本可能同时留下 B-注册号占位行和真实机队行；以本次合并的
            # 记录为准，按注册号收敛为一行，避免机队总数虚增。
            target = row["注册号"].upper()
            seen = False
            deduped = []
            for item in rows:
                if str(item.get("注册号", "")).upper() != target:
                    deduped.append(item)
                elif not seen:
                    deduped.append(item)
                    seen = True
            rows = deduped
            _write_fleet_csv(rows, already_locked=True)
    except Exception as e:
        # 建设/起飞的在线写操作可能已经成功；本地展示合并失败不得反向把任务
        # 标成在线失败或诱发重复请求，留待下一次采集自然对账。
        _append_log(f"⚠ 建设记录暂未进入数据库：{e}")


def _clear_building_fleet_row(reg: str, *, remove_placeholder: bool = False) -> None:
    """建设终止时清理黄色标记；尚未获得机队 ID 的纯占位行直接移除。"""
    fleet_path = _paths().get("fleet")
    if fleet_path is None:
        return
    try:
        with exclusive_file_lock(fleet_path):
            rows = _read_csv(fleet_path)
            target = str(reg).strip().upper()
            if remove_placeholder:
                rows = [row for row in rows if not (
                    str(row.get("注册号", "")).strip().upper() == target
                    and str(row.get("飞机ID", "")).startswith("B-")
                )]
            else:
                for row in rows:
                    if str(row.get("注册号", "")).strip().upper() == target:
                        row["建设状态"] = ""
            _write_fleet_csv(rows, already_locked=True)
    except Exception as exc:
        _append_log(f"⚠ {reg} 的建设标记尚未清理：{exc}")


def _refresh_fleet_row(reg: str, fid: str, hub_id: str, *,
                       finalize_build: bool = False) -> dict | None:
    """抓取一架飞机详情；建设期预检不覆盖主表，首航确认后才最终回填。"""
    if not reg:
        return None
    try:
        import route_planner as _rp
        fleet_path = _paths()["fleet"]
        rows = _read_csv(fleet_path)
        idx = next((i for i, row in enumerate(rows)
                    if row.get("注册号", "").upper() == reg.upper()), None)
        current = rows[idx] if idx is not None else None
        if not fid:
            # 旧任务未携带机队 ID 时，先从本地机队、再从建设记录补查。
            if current:
                fid = current.get("飞机ID", "")
        if not fid:
            for b in _load_builds():
                if b.get("reg", "").upper() == reg.upper():
                    fid = b.get("fid", "")
                    hub_id = hub_id or b.get("hub_id", "")
                    break
        if not fid:
            return None
        hub_name = _hub_name_by_id(hub_id) or (current or {}).get("枢纽分类", "")
        fr = _rp.fetch_aircraft_fleet_row(str(fid), hub_name, reg)
        if not fr:
            return None
        with exclusive_file_lock(fleet_path):
            # 网络请求期间采集进程可能已更新整表；锁内重新读取并只替换目标飞机。
            rows = _read_csv(fleet_path)
            idx = next((i for i, row in enumerate(rows)
                        if row.get("注册号", "").upper() == reg.upper()), None)
            current = rows[idx] if idx is not None else current
            if current:
                # 单机详情页不提供检修/改装状态；不得用空值或“未查询”覆盖主页和全量扫描结果。
                for key in ("距A-Check小时", "损坏率%"):
                    if not str(fr.get(key, "")).strip():
                        fr[key] = current.get(key, "")
                for key in ("CO2减排放", "飞行速度增加", "耗油量减少"):
                    if str(fr.get(key, "")).strip() in {"", "未查询"}:
                        fr[key] = current.get(key, fr.get(key, ""))
                if str(current.get("建设状态", "")).strip() and not finalize_build:
                    # 详情仍供本次需求/检修判断使用，但交付或改装期间的临时
                    # “其他 / 00:00:00”不得覆盖用户已经选定的建线信息。
                    return fr
            fr["建设状态"] = ""
            if idx is not None:
                rows[idx] = fr
            else:
                rows.append(fr)
            if not _write_fleet_csv(rows, already_locked=True):
                return None
            return fr
    except Exception:
        return None


def _latest_home_maintenance(planner, reg: str,
                             force_refresh: bool = False) -> dict | None:
    """读取共享的主页检修状态；改装提交后可强制补读一次最新“待定”时间。"""
    global _home_status_cache, _home_status_ts
    now = time.time()
    with _maint_cache_lock:
        cached = _home_status_cache
        cached_at = _home_status_ts
    if force_refresh or cached is None or now - cached_at > 120:
        try:
            page = planner._do_curl(planner.HOME, data=None, output=None, referer=planner.HOME)
            body = (page or "").strip()
            if (not body
                    or re.search(r"(?:name=['\"]lEmail|weblogin/login\.php|loginForm)", body, re.I)
                    or re.search(r"(?:Bad Gateway|Service Unavailable|Gateway Timeout)", body, re.I)):
                return None
            fresh = planner.parse_status_data(body)
            if not fresh:
                return None
        except Exception:
            return None
        with _maint_cache_lock:
            _home_status_cache = fresh
            _home_status_ts = now
        cached = fresh
    target = str(reg or "").strip().upper()
    return next((status for status in cached.values()
                 if str(status.get("注册号", "")).strip().upper() == target), {})


def _home_maintenance_ready_at(status: dict | None) -> float:
    """返回主页已知的维护/改装就绪时刻；无有效未来约束时返回 0。"""
    if not isinstance(status, dict):
        return 0.0
    try:
        maint_end = float(status.get("维护改装结束时间戳", 0) or 0)
        arrived = float(status.get("预计落地时间戳", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if maint_end <= 0:
        return 0.0
    return max(maint_end, arrived)


def _flight_duration_seconds(value) -> int:
    try:
        parts = str(value).strip().split(":")
        if len(parts) != 3:
            return 0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (TypeError, ValueError):
        return 0


def _set_fleet_duration_if_missing(reg: str, seconds: int) -> None:
    """用首航预计落地时间补齐暂为 00:00:00 的本地航程时长。"""
    if seconds <= 0:
        return
    fleet_path = _paths()["fleet"]
    try:
        with exclusive_file_lock(fleet_path):
            rows = _read_csv(fleet_path)
            row = next((item for item in rows
                        if item.get("注册号", "").strip().upper() == reg.strip().upper()), None)
            if not row or _flight_duration_seconds(row.get("飞行时长", "")) > 0:
                return
            hours, remain = divmod(int(seconds), 3600)
            minutes, secs = divmod(remain, 60)
            row["飞行时长"] = f"{hours:02d}:{minutes:02d}:{secs:02d}"
            row["最后更新时间"] = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
            _write_fleet_csv(rows, already_locked=True)
    except Exception as e:
        _append_log(f"⚠ {reg} 首航时长暂未写回机队数据库：{e}")


def _repair_legacy_doubled_takeoffs(tasks: list[dict], fleet: list[dict]) -> int:
    """把旧版按“飞行时长 × 2”生成的待办改回实际落地时间 + 2 分钟。"""
    durations = {
        str(row.get("注册号", "")).strip().upper():
            _flight_duration_seconds(row.get("飞行时长", ""))
        for row in fleet
    }
    repaired = 0
    for task in tasks:
        params = task.get("params") or {}
        if (task.get("kind") != "takeoff" or task.get("status") != "pending"
                or params.get("reason") != "往返完成"):
            continue
        reg = str(params.get("reg", "")).strip().upper()
        duration = durations.get(reg, 0)
        created_at = float(task.get("created_at", 0) or 0)
        old_trigger = float(task.get("trigger_at", 0) or 0)
        if duration <= 0 or created_at <= 0:
            continue
        ready_at = created_at + duration
        trigger_at = ready_at + _TAKEOFF_READY_BUFFER_SECONDS
        # 仅迁移确实比单程落地时间多出接近一整个航程的旧任务。
        if old_trigger < trigger_at + max(60, duration * 0.5):
            continue
        params["ready_at"] = ready_at
        params["reason"] = "本次飞行落地"
        task["params"] = params
        task["trigger_at"] = trigger_at
        repaired += 1
    return repaired


def _current_fuel_lbs() -> float | None:
    """当前燃油（Lbs）；文件/字段无效返回 None，真实零库存仍返回 0。"""
    try:
        m = json.loads(_paths()["market"].read_text(encoding="utf-8"))
        raw = str(m.get("fuel_qty", "")).replace(",", "").strip()
        if not raw:
            return None
        value = float(raw)
        return value if value >= 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _current_balance() -> float | None:
    """返回当前账号已知余额；优先实时缓存，未知与真实零余额严格区分。"""
    raw = None
    try:
        market = json.loads(_paths()["market"].read_text(encoding="utf-8"))
        raw = market.get("balance")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    with _market_rt_lock:
        if _market_rt_cache is not None and str(_market_rt_cache.get("balance", "")).strip():
            raw = _market_rt_cache.get("balance")
    try:
        text = re.sub(r"[^0-9.-]", "", str(raw or ""))
        if not text:
            return None
        value = float(text)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _takeoff_maintenance_block(reg: str, row: dict | None = None) -> str | None:
    """起飞前的玩家保护：A-Check过近或损坏过高时阻止自动起飞。"""
    min_a_hours = max(0.0, float(os.environ.get("AM4_MIN_A_CHECK_HOURS", "5")))
    max_wear = min(100.0, max(0.0, float(os.environ.get("AM4_MAX_WEAR_FOR_TAKEOFF", "80"))))
    try:
        if row is None:
            row = next((r for r in _read_csv(_paths()["fleet"])
                        if r.get("注册号", "").strip().upper() == reg.strip().upper()), None)
        if not row:
            return None
        a_raw, wear_raw = row.get("距A-Check小时", ""), row.get("损坏率%", "")
        if str(a_raw).strip() and float(str(a_raw).replace(",", "")) <= min_a_hours:
            return f"距A-Check仅 {a_raw} 小时（保护阈值 {min_a_hours:g} 小时）"
        if str(wear_raw).strip() and float(str(wear_raw).replace(",", "")) >= max_wear:
            return f"损坏率 {wear_raw}%（保护阈值 {max_wear:g}%）"
    except (OSError, ValueError, TypeError):
        return None
    return None


def _natural_key(s: str):
    """自然排序：让 MC-21-4-2 排在 MC-21-4-10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def _broadcast_sse(data: dict):
    dead = []
    for q in list(_sse_clients):  # 快照迭代，避免并发连接增删导致 RuntimeError
        try:
            q.put_nowait(data)
        except queue.Full:
            dead.append(q)  # 客户端消费太慢：移除，避免内存无限增长
        except Exception:
            dead.append(q)
    for q in dead:
        if q in _sse_clients:
            _sse_clients.remove(q)


def _publish_log(line: str) -> None:
    """写入运行日志并立即推送到前端，供交互操作共用。"""
    _append_log(line)
    _broadcast_sse({"type": "log", "line": line})


_ROUTE_STEP_LABELS = {
    "buy": "购买飞机",
    "aircraft": "确认飞机",
    "deliver": "交付检查",
    "route": "建立航线",
    "retrofit": "安排改装",
    "takeoff": "安排起飞",
}


def _airport_log_label(airport: dict | None, airport_id: str) -> str:
    if not airport:
        return f"机场ID {airport_id}"
    name = str(airport.get("name") or airport.get("city") or "未知机场")
    code = str(airport.get("iata") or airport.get("icao") or "").upper()
    return f"{name}{f' ({code})' if code else ''} [ID {airport_id}]"


def _route_step_log(reg: str, step: dict) -> str:
    key = str(step.get("step", ""))
    label = _ROUTE_STEP_LABELS.get(key, key or "执行")
    icon = "✓" if step.get("ok") else "✗"
    return f"🏗 [{reg}] {label} {icon}：{step.get('msg', '')}"


# ===== 从脚本日志行解析飞机数据 =====

# 示例行: "飞机详情 [3/71] MC-21-4-3"
_AIR_DETAIL_RE = re.compile(r"飞机详情\s*\[(\d+)/(\d+)\]\s+(.+)")

# ===== 路由 =====

@app.route("/")
def index():
    return render_template("index.html", csrf_token=_csrf_token)


@app.route("/api/status")
def api_status():
    p = _paths()
    fleet = _read_csv(p["fleet"])
    maint = _read_csv(p["maint"])
    with _run_lock:
        status = {
            "running": _run_status["running"],
            "mode": _run_status["mode"],
            "last_run": _run_status["last_run"],
            "error": _run_status["error"],
            "fleet_count": len(fleet),
            "maint_count": len(maint),
            "account": _active_credentials()[0],
            "progress_total": _run_status["progress_total"],
            "progress_current": _run_status["progress_current"],
        }
    return jsonify(status)


@app.route("/api/stream")
def api_stream():
    q = queue.Queue(maxsize=200)  # 有界：慢/死连接不无限累积
    _sse_clients.append(q)

    def generate():
        try:
            # 发送初始化：包含最近日志（从磁盘读取）
            lines = _read_log_lines()
            _log_total = len(lines)
            with _maint_cache_lock:
                _maint_init = _maint_cache
            initial = {
                "type": "init",
                "running": _run_status["running"],
                "mode": _run_status.get("mode", ""),
                "account": _active_credentials()[0],
                "log": lines[-50:],  # 最近50条，更早的可上翻滚动加载
                "log_start": max(0, _log_total - 50),
                "log_total": _log_total,
                "progress_total": _run_status.get("progress_total", 0),
                "progress_current": _run_status.get("progress_current", 0),
                "maint": _maint_init,
            }
            yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"

            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/log")
def api_log():
    """分页加载历史日志：offset = 当前已显示最早一行的 index，向前取 limit 条。"""
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    lines = _read_log_lines()
    total = len(lines)
    start = max(0, min(offset, total))
    slice_lines = lines[max(0, start - limit):start]
    return jsonify({"total": total, "start": max(0, start - limit), "lines": slice_lines})


@app.route("/api/log/download")
def api_log_download():
    """下载原始完整日志（run_log.txt）。"""
    text = "\n".join(_read_log_lines())
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=run_log.txt"},
    )


@app.route("/api/fleet")
def api_fleet():
    hub = request.args.get("hub", "")
    search = request.args.get("search", "").strip().lower()
    sort_by = request.args.get("sort", "注册号")
    sort_dir = request.args.get("dir", "asc")
    cli = request.args.get("cli", "")  # pax=仅客机 cargo=仅货机（按客机组数量区分）

    rows = _fleet_rows()
    if cli:
        # 货机分组客机组数量=0（如 B757-2F-1）；客机 >0（MC-21=70, A380=1）
        if cli == "cargo":
            rows = [r for r in rows if str(r.get("客机组数量", "0")).strip() == "0"]
        elif cli == "pax":
            rows = [r for r in rows if str(r.get("客机组数量", "0")).strip() != "0"]
    if hub:
        rows = [r for r in rows if r.get("枢纽分类", "") == hub]
    if search:
        rows = [r for r in rows if search in r.get("注册号", "").lower()
                or search in r.get("航班号", "").lower()
                or search in r.get("机型", "").lower()
                or search in r.get("起飞机场代码", "").lower()
                or search in r.get("到达机场代码", "").lower()]

    reverse = sort_dir == "desc"

    def _mod_count(r: dict) -> int:
        return sum(1 for k in ("CO2减排放", "飞行速度增加", "耗油量减少")
                   if r.get(k) == "已改装")

    def _duration_secs(v) -> int:
        try:
            p = str(v).split(":")
            return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])
        except (ValueError, IndexError):
            return -1

    _demand_priority = {"不足": 1, "旺盛": 2}
    try:
        if sort_by == "注册号":
            rows.sort(key=lambda r: _natural_key(r.get(sort_by, "")), reverse=reverse)
        elif sort_by == "改装状态":
            rows.sort(key=_mod_count, reverse=reverse)
        elif sort_by == "飞行时长":
            rows.sort(key=lambda r: _duration_secs(r.get("飞行时长", "")), reverse=reverse)
        elif sort_by == "需求状态":
            rows.sort(key=lambda r: _demand_priority.get(r.get("需求状态", ""), 0), reverse=reverse)
        else:
            rows.sort(key=lambda r: r.get(sort_by, ""), reverse=reverse)
    except Exception:
        pass

    return jsonify(rows)


@app.route("/api/fleet/stats")
def api_fleet_stats():
    rows = _fleet_rows()
    hub_counts = {}
    models = {}
    for r in rows:
        h = r.get("枢纽分类", "其他")
        hub_counts[h] = hub_counts.get(h, 0) + 1
        m = r.get("机型", "未知")
        models[m] = models.get(m, 0) + 1

    co2_done = sum(1 for r in rows if r.get("CO2减排放") == "已改装")
    speed_done = sum(1 for r in rows if r.get("飞行速度增加") == "已改装")
    fuel_done = sum(1 for r in rows if r.get("耗油量减少") == "已改装")

    return jsonify({
        "total": len(rows),
        "hubs": hub_counts,
        "models": models,
        "mods": {"CO2减排放": co2_done, "飞行速度增加": speed_done, "耗油量减少": fuel_done},
    })


@app.route("/api/hubs")
def api_hubs():
    p = _paths()
    if p["hubs"].exists():
        try:
            hubs = json.loads(p["hubs"].read_text(encoding="utf-8"))
            names = sorted((h.get("name", "") for h in hubs if h.get("name", "")))
            return jsonify(names)
        except Exception:
            pass
    rows = _read_csv(p["fleet"])
    hubs = sorted(set(r.get("枢纽分类", "其他") for r in rows))
    return jsonify(hubs)


# ---------------------------------------------------------------------------
# 航线开辟规划器 API
# ---------------------------------------------------------------------------

@app.route("/api/route/airports")
def api_route_airports():
    """机场搜索：?q=名称/IATA/ICAO/国家，用于出发/到达下拉。"""
    rp = _get_route_planner()
    q = request.args.get("q", "")
    limit = min(int(request.args.get("limit", "100")), 500)
    return jsonify(rp.search_airports(q, limit))


@app.route("/api/route/aircrafts")
def api_route_aircrafts():
    """机型列表（含 MC-21-400）。"""
    rp = _get_route_planner()
    return jsonify(rp._load_aircraft_models())


@app.route("/api/route/hubs")
def api_route_hubs():
    """当前账号枢纽（带机场ID），供出发下拉使用。"""
    rp = _get_route_planner()
    p = _paths()
    hubs = []
    if p["hubs"].exists():
        try:
            for h in json.loads(p["hubs"].read_text(encoding="utf-8")):
                ap_id = rp.hub_airport_id(h.get("name", ""))
                hubs.append({
                    "hub_id": h.get("hub_id", ""),
                    "name": h.get("name", ""),
                    "is_base": h.get("is_base", False),
                    "airport_id": ap_id,
                })
        except Exception:
            pass
    return jsonify(hubs)


@app.route("/api/route/estimate")
@_serialized_online
def api_route_estimate():
    """收益预估：
    ?ac=<机型名>&tpd=<每日班次>&dep=<机场ID>&arr=<机场ID>
    （可选覆盖参数：pax_load/cargo_load/fuel_price/co2_price/cost_index）
    """
    rp = _get_route_planner(require_login=True)
    ac_name = request.args.get("ac", "")
    tpd = int(request.args.get("tpd", "1") or 1)
    dep_id = request.args.get("dep", "")
    arr_id = request.args.get("arr", "")
    aircraft = rp.aircraft_by_name(ac_name, request.args.get("engine"))
    origin = rp.airport_by_id(dep_id)
    dest = rp.airport_by_id(arr_id)
    if not aircraft:
        return jsonify({"ok": False, "error": f"未知机型: {ac_name}"}), 400
    if not origin or not dest:
        return jsonify({"ok": False, "error": "出发或到达机场无效"}), 400
    try:
        est = rp.estimate_route(
            aircraft, tpd, origin, dest,
            pax_load=float(request.args.get("pax_load", rp.DEFAULT_PAX_LOAD)),
            cargo_load=float(request.args.get("cargo_load", rp.DEFAULT_CARGO_LOAD)),
            fuel_price=float(request.args.get("fuel_price", rp.DEFAULT_FUEL_PRICE)),
            co2_price=float(request.args.get("co2_price", rp.DEFAULT_CO2_PRICE)),
            cost_index=int(request.args.get("cost_index", rp.DEFAULT_COST_INDEX)),
        )
        est["ok"] = True
        est["aircraft"] = {
            "name": aircraft["name"], "capacity": aircraft.get("capacity", 0),
            "range": aircraft.get("range", 0), "cost": aircraft.get("cost", 0),
            "speed": aircraft.get("speed", 0), "fuel": aircraft.get("fuel", 0),
        }
        est["origin"] = {"id": origin["id"], "name": origin["name"], "iata": origin.get("iata", "")}
        est["dest"] = {"id": dest["id"], "name": dest["name"], "iata": dest.get("iata", "")}
        return jsonify(est)
    except Exception as e:
        return jsonify({"ok": False, "error": f"预估失败: {e}"}), 500


@app.route("/api/route/candidates")
def api_route_candidates():
    """按 机型+班次+出发 自动筛选可飞航线（纯筛选，不含收益）：
    ?ac=<机型名>&tpd=<每日班次>&dep=<机场ID>
    规则：每段航程 <= 24/tpd 小时（如 6 班 → 4 小时）且不超机型航程。
    """
    rp = _get_route_planner()
    ac_name = request.args.get("ac", "")
    maximize = request.args.get("maximize", "").strip().lower() in {"1", "true", "yes"}
    try:
        tpd = int(request.args.get("tpd", "6") or 6)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "每日班次必须是整数"}), 400
    if not 1 <= tpd <= 20:
        return jsonify({"ok": False, "error": "每日班次必须在 1~20 之间"}), 400
    dep_id = request.args.get("dep", "")
    aircraft = rp.aircraft_by_name(ac_name, request.args.get("engine"))
    origin = rp.airport_by_id(dep_id)
    if not aircraft:
        return jsonify({"ok": False, "error": f"未知机型: {ac_name}"}), 400
    if not origin:
        return jsonify({"ok": False, "error": "出发机场无效"}), 400
    try:
        exclude = _build_exclude_set()
        cands = rp.candidate_routes(
            aircraft, origin, tpd,
            cost_index=int(request.args.get("cost_index", rp.DEFAULT_COST_INDEX)),
            exclude=exclude,
            limit=min(int(request.args.get("limit", "300")), 500),
            maximize=maximize,
            max_tpd=20,
        )
        return jsonify({
            "ok": True,
            "aircraft": aircraft["name"],
            "origin": {"id": origin["id"], "name": origin["name"], "iata": origin.get("iata", "")},
            "tpd": tpd,
            "maximize": maximize,
            "max_hours": None if maximize else round(24.0 / tpd, 2),
            "count": len(cands),
            "candidates": cands,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"筛选失败: {e}"}), 500


@app.route("/api/route/rank")
def api_route_rank():
    """候选航线纯本地估算净收益并按每日净利润降序（零网络请求，秒级）：
    ?ac=<机型名>&tpd=<每日班次>&dep=<机场ID>&limit=<最多条数，默认300>
    """
    rp = _get_route_planner()
    ac_name = request.args.get("ac", "")
    tpd = int(request.args.get("tpd", "6") or 6)
    dep_id = request.args.get("dep", "")
    limit = min(int(request.args.get("limit", "300")), 500)
    aircraft = rp.aircraft_by_name(ac_name, request.args.get("engine"))
    origin = rp.airport_by_id(dep_id)
    if not aircraft:
        return jsonify({"ok": False, "error": f"未知机型: {ac_name}"}), 400
    if not origin:
        return jsonify({"ok": False, "error": "出发机场无效"}), 400
    try:
        exclude = _build_exclude_set()
        results = rp.rank_routes(
            aircraft, origin, tpd,
            cost_index=int(request.args.get("cost_index", rp.DEFAULT_COST_INDEX)),
            pax_load=float(request.args.get("pax_load", rp.DEFAULT_PAX_LOAD)),
            fuel_price=float(request.args.get("fuel_price", rp.DEFAULT_FUEL_PRICE)),
            co2_price=float(request.args.get("co2_price", rp.DEFAULT_CO2_PRICE)),
            exclude=exclude,
            limit=limit,
        )
        return jsonify({
            "ok": True,
            "aircraft": aircraft["name"],
            "origin": {"id": origin["id"], "name": origin["name"], "iata": origin.get("iata", "")},
            "tpd": tpd,
            "count": len(results),
            "results": results,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"计算失败: {e}"}), 500


@app.route("/api/route/build", methods=["POST"])
@_serialized_online
def api_route_build():
    """一键建设：买飞机（带布局）→ 等待交付 → 定航线 → 排入改装/起飞待办。
    body: {ac, dep_hub_id, dep_airport_id, arr_airport_id, reg, economy?, business?, first?,
           engine?, amount?, cost_index?}
    """
    _require_csrf()
    rp = _get_route_planner()
    data = request.get_json(silent=True)
    if request.is_json and data is None:
        return jsonify({"ok": False, "msg": "请求 JSON 无效"}), 400
    data = data or {}
    ac_name = data.get("ac", "")
    hub_id = str(data.get("dep_hub_id", ""))
    origin_airport_id = str(data.get("dep_airport_id", "") or "")
    arr_id = str(data.get("arr_airport_id", ""))
    reg = str(data.get("reg", "")).strip()
    engine_raw = str(data.get("engine", "") or "").strip()
    aircraft = rp.aircraft_by_name(ac_name, engine_raw or None)
    if not aircraft:
        return jsonify({"ok": False, "error": (
            f"机型 {ac_name} 不支持发动机 {engine_raw}" if engine_raw else f"未知机型: {ac_name}"
        )}), 400
    if not hub_id or not origin_airport_id or not arr_id:
        return jsonify({"ok": False, "error": "缺少出发枢纽、出发机场或到达机场"}), 400
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,14}", reg):
        return jsonify({"ok": False, "error": "注册号仅允许字母、数字、-、_、.，长度 1~14"}), 400
    try:
        try:
            amount = int(data.get("amount", 1))
            cost_index = int(data.get("cost_index", rp.DEFAULT_COST_INDEX))
            tpd = int(data.get("tpd", 1))
            business = int(data.get("business", 0))
            first = int(data.get("first", 0))
            economy_raw = data.get("economy")
            economy = None if economy_raw is None else int(economy_raw)
            cargo_l_raw = data.get("cargo_l")
            cargo_h_raw = data.get("cargo_h")
            cargo_l = None if cargo_l_raw is None else int(cargo_l_raw)
            cargo_h = None if cargo_h_raw is None else int(cargo_h_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "购买数量、班次、成本指数和舱位必须是整数"}), 400
        if amount != 1:
            return jsonify({"ok": False, "error": "单次建设目前仅允许购买 1 架，避免注册号和待办冲突"}), 400
        if not 0 <= cost_index <= 200:
            return jsonify({"ok": False, "error": "成本指数必须在 0~200 之间"}), 400
        if not 1 <= tpd <= 20:
            return jsonify({"ok": False, "error": "每日班次必须在 1~20 之间"}), 400
        if any(v < 0 for v in (business, first)) or (economy is not None and economy < 0):
            return jsonify({"ok": False, "error": "舱位数量不能为负数"}), 400
        capacity = int(aircraft.get("capacity", 0) or 0)
        is_cargo = str(aircraft.get("type", "0")) == "1"
        if is_cargo:
            if (cargo_l is None or cargo_h is None or cargo_l < 0 or cargo_h < 0
                    or cargo_l > 100 or cargo_h > 100 or cargo_l + cargo_h != 100):
                return jsonify({"ok": False, "error": "货机必须提供合计 100% 的 L/H 货舱比例"}), 400
        elif economy is not None:
            used = economy + business * 2 + first * 3
            if used != capacity:
                return jsonify({"ok": False, "error": f"舱位布局占用 {used}，必须恰好等于机型容量 {capacity}"}), 400
        if origin_airport_id == arr_id:
            return jsonify({"ok": False, "error": "出发和到达机场不能相同"}), 400
        dest_ap = rp.airport_by_id(arr_id)
        if not dest_ap:
            return jsonify({"ok": False, "error": "到达机场无效"}), 400
        origin_ap = rp.airport_by_id(origin_airport_id)
        if not origin_ap:
            return jsonify({"ok": False, "error": "出发机场无效"}), 400
        eco_val = ("" if is_cargo else (economy if economy is not None
                   else max(0, capacity - 2 * business - 3 * first)))
        retrofit_val = data.get("retrofit", "全部")
        retrofit_mods = _retrofit_mods(retrofit_val)
        if retrofit_val and not retrofit_mods.issubset({"co2", "speed", "fuel"}):
            return jsonify({"ok": False, "error": f"改装配置无效: {retrofit_val}"}), 400
        engine_val = str(aircraft.get("eid") or "默认")
        origin_label = _airport_log_label(origin_ap, origin_airport_id)
        dest_label = _airport_log_label(dest_ap, arr_id)
        _publish_log(
            f"🏗 正在建设航线。机型：{ac_name} 注册号：{reg}\n"
            f" 始发：{origin_label} 终到：{dest_label}\n"
        )
        layout_text = (f"L{cargo_l}%/H{cargo_h}%" if is_cargo
                       else f"Y{eco_val}/J{business}/F{first}")
        _publish_log(
            f"💺 仓位配置：{layout_text}｜引擎型号 {aircraft.get('ename') or engine_val} ({engine_val})\n"
            f" {retrofit_val or '跳过'} 改装"
        )
        # 建设前资金闸门：以当前余额和本地收益引擎估算总投入，保留运营现金。
        preflight = rp.estimate_route_local(
            aircraft, tpd, origin_ap, dest_ap, cost_index=cost_index)
        if not preflight.get("feasible"):
            _publish_log(
                f"🏗 {reg} 建设失败 ✗：{preflight.get('reason') or '班次、航程或需求不满足'}"
            )
            return jsonify({"ok": False, "error": f"航线不可建设：{preflight.get('reason') or '班次、航程或需求不满足'}"}), 400
        investment = float(preflight.get("initial_investment", 0) or 0)
        reserve = max(0.0, float(os.environ.get("AM4_CASH_RESERVE", "5000000")))
        stopover = preflight.get("stopover") or {}
        stopover_text = (f"经停 {stopover.get('name') or stopover.get('iata')}"
                         if stopover else "直达")
        payback = preflight.get("payback_days")
        _publish_log(
            f"📊 航程预计 {float(preflight.get('distance_km', 0) or 0):,.0f} km"
            f"｜航线{stopover_text}｜单程 {float(preflight.get('flight_hours', 0) or 0):.2f} 小时｜\n"
            f" 预估日收益 ${float(preflight.get('revenue_per_day', 0) or 0):,.0f}｜"
            f"净利 ${float(preflight.get('net_per_day', 0) or 0):,.0f}｜\n"
            f" 预计投入 ${investment:,.0f}｜{payback if payback is not None else '未知'} 天回本"
        )
        # 先只读确认是否属于“已购机后的恢复”。已有飞机不再重复计入投资，
        # 这样响应丢失后的低余额状态仍能继续建线；查询未知则不执行任何写操作。
        _get_route_planner(require_login=True)
        try:
            existing_fid = rp._fleet_aircraft_id(str(aircraft.get("id", "")), reg)
        except rp.FleetLookupError as e:
            _publish_log(f"❌ 注册号检查失败：{e}，未执行购机")
            return jsonify({
                "ok": False,
                "error": "暂时无法确认该注册号是否已经存在；为避免重复购机，请稍后重试",
            }), 503
        if existing_fid:
            _publish_log(
                f"♻️ {reg} 已在机队中（ID {existing_fid}），进入幂等恢复；不再要求购机资金"
            )
            balance = None
        else:
            balance = _current_balance()
            if balance is None:
                _publish_log(f"💰 资金检查 ✗：余额尚未确认，未执行购机")
                return jsonify({
                    "ok": False,
                    "error": "余额尚未确认，为保护运营资金暂不建设；请等待资产刷新后重试",
                }), 409
            if investment > max(0.0, balance - reserve):
                _publish_log(
                    f"💰 资金检查 ✗：余额 ${balance:,.0f}｜保留 ${reserve:,.0f}｜\n"
                    f" 可投入 ${max(0, balance-reserve):,.0f}｜预计需要 ${investment:,.0f}"
                )
                return jsonify({
                    "ok": False,
                    "error": (f"资金保护：预计投入 ${investment:,.0f}，当前可用 ${max(0, balance-reserve):,.0f}"
                              f"（已保留 ${reserve:,.0f} 运营现金）"),
                    "investment": round(investment), "balance": round(balance),
                    "cash_reserve": round(reserve),
                }), 409
            _publish_log(
                f"💰 资金检查 ✓：余额 ${balance:,.0f}｜建设后剩余 ${balance-investment:,.0f}"
            )
        # 在第一笔真实写请求前就把目标航线写入主表。这样即使恰逢半点采集，
        # 采集器也会识别“建设中”并保留用户选定的枢纽/起降机场信息。
        _record_build(
            reg, ac_name, hub_id, str(data.get("dep_airport_id", "") or ""), arr_id,
            "delivering" if existing_fid else "ordered",
            fid=str(existing_fid or ""), economy=eco_val, business=business, first=first,
            cargo_l=cargo_l if is_cargo else "", cargo_h=cargo_h if is_cargo else "",
            engine=engine_val, retrofit=retrofit_val,
        )
        result = rp.build_route(
            aircraft, hub_id, arr_id, reg,
            economy=economy, business=business,
            first=first, engine=engine_val, cargo_l=cargo_l, cargo_h=cargo_h,
            amount=amount, cost_index=cost_index,
            origin_airport_id=origin_airport_id or None,
            retrofit=data.get("retrofit", "all"),  # 默认改装；显式 null 表示跳过
            confirmed_fid=str(existing_fid) if existing_fid else None,
            fleet_absence_confirmed=not bool(existing_fid),
            delivery_confirmed=False,
            after_spend=_refresh_market_after_spend,
        )
        b_val = business
        f_val = first
        for step in result.get("steps", []):
            _publish_log(_route_step_log(reg, step))
        waiting_delivery = bool(result.get("waiting_delivery"))
        waiting_route_lookup = bool(result.get("waiting_route_lookup"))
        waiting_fleet_lookup = bool(result.get("waiting_fleet_lookup"))
        if waiting_delivery or waiting_route_lookup or waiting_fleet_lookup:
            _add_pending_task(
                "delivery_continue",
                (f"将在 {reg} 交付后继续建设航线"
                 if waiting_delivery else
                 (f"飞机 {reg} 恢复已有航线信息"
                  if waiting_route_lookup else
                  f"飞机 {reg} 只读恢复购机结果")),
                time.time() + max(60, int(result.get("remain_sec", 600))),
                {
                    "fid": result.get("fid"),
                    "aircraft": aircraft, "hub_id": hub_id, "arr_id": arr_id, "reg": reg,
                    "economy": economy, "business": business,
                    "first": first, "engine": engine_val,
                    "cargo_l": cargo_l, "cargo_h": cargo_h,
                    "amount": amount, "cost_index": cost_index,
                    "origin_airport_id": origin_airport_id or None,
                    "retrofit": data.get("retrofit", "all"),
                },
            )
            if waiting_delivery:
                _preschedule_retrofit(
                    reg, arr_id,
                    cost_index,
                    time.time() + max(60, int(result.get("remain_sec", 600))) + 180,
                    fid=result.get("fid", ""), hub_id=hub_id,
                    retrofit=retrofit_val, economy=eco_val, business=b_val, first=f_val,
                    cargo_l=cargo_l if is_cargo else "", cargo_h=cargo_h if is_cargo else "",
                )
            _publish_log(
                f"⏳ 已创建{'交付续建' if waiting_delivery else ('航线恢复' if waiting_route_lookup else '购机结果恢复')}待办\n"
                f" {max(1, result.get('remain_sec', 0) // 60)} 分钟后检查并自动继续"
            )
        if result.get("route_id"):
            _publish_log(
                f"✅ [{reg}] 航线建设完成：航线ID {result['route_id']}，已移交待办运营"
            )
        elif not (waiting_delivery or waiting_route_lookup or waiting_fleet_lookup):
            _publish_log(
                f"{'✅' if result.get('ok') else '❌'} [{reg}] 建设流程结束："
                f"{'已完成当前可执行步骤' if result.get('ok') else '未能完成，请查看上方失败步骤'}"
            )
        final_build_status = (
            "routed" if result.get("route_id")
            else ("delivering" if waiting_delivery or waiting_fleet_lookup
                  else ("waiting" if waiting_route_lookup
                        else ("done" if result.get("ok") else "failed"))))
        _record_build(
            reg, ac_name, hub_id,
            str(data.get("dep_airport_id", "") or ""),
            arr_id,
            final_build_status,
            fid=result.get("fid", ""),
            route_id=result.get("route_id", ""),
            economy=eco_val, business=b_val, first=f_val,
            cargo_l=cargo_l if is_cargo else "", cargo_h=cargo_h if is_cargo else "",
            engine=engine_val,
            retrofit=retrofit_val,
        )
        if result.get("route_id"):
            _create_retrofit_task(
                reg, result["route_id"],
                cost_index,
                result.get("fid", ""), hub_id, retrofit_val,
                eco_val, b_val, f_val,
                cargo_l if is_cargo else "", cargo_h if is_cargo else "",
            )
        return jsonify(result)
    except Exception as e:
        _publish_log(f"❌ 建设 {ac_name} {reg} 异常终止：{e}")
        return jsonify({"ok": False, "error": f"建设失败: {e}", "steps": []}), 500


@app.route("/api/pending")
def api_pending():
    """待定任务清单（交付等待等）：按触发时间排序，含剩余秒数。"""
    now = time.time()
    with _pending_lock:
        tasks = sorted(
            (t for t in _pending_tasks if t.get("status") in ("pending", "running", "failed")),
            key=lambda t: t.get("trigger_at", 0),
        )
        out = [{
            "id": t["id"],
            "kind": t.get("kind"),
            "title": _pending_display_title(t),
            "status": t.get("status"),
            "error": t.get("error"),
            "route_id": str((t.get("params") or {}).get("route_id", "")),
            "route_required": t.get("kind") in _AIRCRAFT_OWNED_TASK_KINDS,
            "route_ready": bool(t.get("params", {}).get("route_id")),
            "trigger_at": t.get("trigger_at"),
            "remaining": max(0, int(t.get("trigger_at", now) - now)),
            "created_at": t.get("created_at"),
        } for t in tasks]
    return jsonify(out)


def _pending_display_title(task: dict) -> str:
    """待办列表使用适合窄屏的标题，细节由独立字段展示。"""
    if task.get("kind") != "takeoff":
        return str(task.get("title") or "")
    params = task.get("params") or {}
    reg = str(params.get("reg", "飞机")).strip() or "飞机"
    reason = str(params.get("reason", ""))
    if reason in {"维护/改装完成", "返场结束"} or "返场" in str(task.get("title", "")):
        return f"{reg} 返场后起飞"
    return f"{reg} 下次起飞"


@app.route("/api/pending/<tid>/cancel", methods=["POST"])
def api_pending_cancel(tid: str):
    """取消一条尚未执行的待定任务。"""
    _require_csrf()
    with _pending_lock:
        for t in _pending_tasks:
            if t["id"] == tid and t.get("status") == "pending":
                t["status"] = "cancelled"
                _save_pending_tasks()
                if t.get("kind") == "delivery_continue":
                    _mark_build(t.get("params", {}).get("reg", ""), status="cancelled")
                return jsonify({"ok": True, "msg": "已取消"})
    return jsonify({"ok": False, "error": "任务不存在或已执行"}), 404


@app.route("/api/maintenance")
def api_maintenance():
    """飞机检修预警：只返回需要检修的飞机（A-Check 优先、高损坏率次之）。"""
    # 优先返回采集脚本实时推送的检修预警缓存
    with _maint_cache_lock:
        if _maint_cache is not None:
            return jsonify(_maint_cache)

    fleet = _read_csv(_paths()["fleet"])

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def fmt_num(x: float) -> str:
        return f"{x:g}" if x == int(x) else f"{x:.1f}"

    a_check, high_wear = [], []
    for r in fleet:
        reg = r.get("注册号", "").strip()
        hours = to_float(r.get("距A-Check小时"))
        wear = to_float(r.get("损坏率%"))
        if not reg or (hours is None and wear is None):
            continue
        if hours is not None and hours < 50:
            # A-Check 优先
            a_check.append({
                "注册号": reg,
                "距A-Check小时": fmt_num(hours),
                "损坏率%": fmt_num(wear) if wear is not None else "",
                "status": "需A-Check",
                "_sort": hours,
            })
        elif wear is not None and wear > 60:
            high_wear.append({
                "注册号": reg,
                "距A-Check小时": fmt_num(hours) if hours is not None else "",
                "损坏率%": fmt_num(wear),
                "status": "高损坏",
                "_sort": wear,
            })

    a_check.sort(key=lambda x: x["_sort"])
    high_wear.sort(key=lambda x: x["_sort"], reverse=True)

    warnings = []
    for it in a_check + high_wear:
        it.pop("_sort", None)
        warnings.append(it)

    return jsonify({
        "warnings": warnings,
        "count": len(warnings),
        "updated_at": _now_bjt().strftime("%H:%M:%S"),
    })


@app.route("/api/market")
def api_market():
    """返回余额、燃油价格、CO2 价格等市场数据。

    立即返回缓存旧值（不阻塞），若距上次成功抓取 >2 分钟则后台异步抓主页
    更新余额/燃油库存，完成后经 SSE 推送 market 事件供前端无感更新。
    """
    # 始终以磁盘采集快照（含燃油价/CO2价/配额）为基底
    data = {"balance": "", "fuel_qty": "", "fuel_price": "", "co2_price": "", "co2_qty": "", "updated_at": ""}
    _mkt = _paths()["market"]
    if _mkt.exists():
        try:
            data.update(json.loads(_mkt.read_text(encoding="utf-8")))
        except Exception:
            pass
    now = time.time()
    with _market_rt_lock:
        if _market_rt_cache is not None:
            # 实时缓存覆盖余额/燃油库存（抓取会更新）
            data["balance"] = _market_rt_cache.get("balance", data.get("balance", ""))
            data["fuel_qty"] = _market_rt_cache.get("fuel_qty", data.get("fuel_qty", ""))
            data["co2_qty"] = _market_rt_cache.get("co2_qty", data.get("co2_qty", ""))
            data["co2_price"] = _market_rt_cache.get("co2_price", data.get("co2_price", ""))
            data["fuel_price"] = _market_rt_cache.get("fuel_price", data.get("fuel_price", ""))
            data["updated_at"] = _market_rt_cache.get("updated_at", data.get("updated_at", ""))
            need_fetch = (now - _market_rt_ts) > _market_rt_min_interval
        else:
            need_fetch = True
        retry_in = max(0, int(_market_rt_retry_after - now))
        retry_error = _market_rt_last_error

    if retry_in > 0:
        need_fetch = False

    if need_fetch:
        # 去重锁：同一时刻只有一个 market worker，避免 Cookie 失效时并发 curl
        if _market_rt_worker_lock.acquire(blocking=False):
            threading.Thread(target=_refresh_market_rt_worker_locked, daemon=True).start()
        data["refreshing"] = True
    else:
        data["refreshing"] = False
    data["refresh_retry_in"] = retry_in
    data["refresh_error"] = retry_error if retry_in > 0 else ""

    reserve = max(0.0, float(os.environ.get("AM4_CASH_RESERVE", "5000000")))
    try:
        balance_num = float(re.sub(r"[^0-9.-]", "", str(data.get("balance", ""))) or 0)
    except (TypeError, ValueError):
        balance_num = 0.0
    data["cash_reserve"] = round(reserve)
    data["spendable_balance"] = round(max(0.0, balance_num - reserve)) if balance_num > 0 else None
    return jsonify(data)


def _refresh_market_rt_worker_locked():
    """市场去重并串行占用在线会话，避免与航线/待办竞争 Cookie。"""
    try:
        with _online_session_lock:
            _refresh_market_rt_worker()
    finally:
        _market_rt_worker_lock.release()


@app.route("/api/run", methods=["POST"])
def api_run():
    _require_csrf()
    global _run_status, _run_proc, _stop_requested
    data = request.get_json(silent=True)
    if request.is_json and data is None:
        return jsonify({"ok": False, "msg": "请求 JSON 无效"}), 400
    data = data or {}
    mode = str(data.get("mode", "once"))
    if mode not in {"once", "light", "loop"}:
        return jsonify({"ok": False, "msg": "运行模式必须是 once、light 或 loop"}), 400
    with _run_lock:
        if _run_status["running"]:
            return jsonify({"ok": False, "msg": "脚本已在运行中，请先停止"})
        _stop_requested = False
        _run_status["running"] = True
        _run_status["mode"] = mode
        _run_status["error"] = None
        _run_status["progress_total"] = 0
        _run_status["progress_current"] = 0
    # 清空历史日志：备份旧文件（带时间戳）后重新开始
    try:
        lf = _paths()["log"]
        if lf.exists():
            content = lf.read_text(encoding="utf-8")
            if content.strip():
                ts = _now_bjt().strftime("%Y%m%d_%H%M%S")
                bak = lf.with_name(f"run_log_{ts}.txt.bak")
                bak.write_text(content, encoding="utf-8")
    except Exception:
        pass
    try:
        _paths()["log"].write_text("", encoding="utf-8")
    except Exception:
        pass

    _broadcast_sse({
        "type": "start",
        "mode": _run_status["mode"],
    })

    run_email, run_password = _active_credentials()
    run_paths = _paths_for_account(run_email)

    def _runner():
        global _run_status, _run_proc, _market_rt_cache, _market_rt_ts, _maint_cache
        global _home_status_cache, _home_status_ts
        seen_aircraft_ids: set[str] = set()
        try:
            script = str(Path(__file__).resolve().parent / "collector.py")
            args = [sys.executable, script]
            if _run_status["mode"] == "loop":
                args.append("--loop")
            elif _run_status["mode"] == "light":
                args.append("--light")

            env = os.environ.copy()
            # 一次 runner 固定使用启动时账号；运行中修改 .env 只会在本轮结束后切换。
            env["AM4_EMAIL"] = run_email
            env["AM4_PASSWORD"] = run_password
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                args,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            # 先安全登记 _run_proc（受锁保护），消除"刚启动即点停止"的竞态窗口
            with _run_lock:
                _run_proc = proc

            # ---- 行读取：独立 reader 线程 + 带超时消费者，避免管道阻塞挂死 ----
            line_q: queue.Queue = queue.Queue()

            def _stdout_reader():
                try:
                    for raw in _run_proc.stdout:
                        line_q.put(raw)
                except Exception:
                    pass
                finally:
                    line_q.put(None)  # EOF 哨兵

            threading.Thread(target=_stdout_reader, daemon=True).start()

            _mode = _run_status.get("mode", "")
            idle_since = time.time()
            sleep_deadline: float | None = None  # 脚本宣布睡眠后的到期时刻（看门狗）
            _WORK_SILENCE = 15 * 60  # 运行中最大无输出时间
            _SLEEP_GRACE = 15 * 60   # 睡眠到期后的宽限

            def _watchdog_trip(reason: str):
                """看门狗：强制终止卡死的采集进程。"""
                _append_log(f"⏹ 看门狗：{reason}，已强制终止")
                _broadcast_sse({"type": "log", "line": f"⏹ 看门狗：{reason}，已强制终止\n"})
                _run_proc.terminate()
                _run_status["error"] = f"看门狗超时（{reason}）"

            while True:
                try:
                    raw = line_q.get(timeout=30)
                except queue.Empty:
                    if _run_proc.poll() is not None:
                        break  # 进程已退出
                    now = time.time()
                    if _mode == "loop":
                        if sleep_deadline is not None:
                            # 睡眠中：超过宣布的醒来时刻 + 宽限仍未恢复 → 卡死
                            if now > sleep_deadline:
                                _watchdog_trip("睡眠到期后未恢复")
                                break
                        elif now - idle_since > _WORK_SILENCE:
                            # 运行中：超过 15 分钟无输出 → 卡死
                            _watchdog_trip("运行中超过 15 分钟无输出")
                            break
                    elif now - idle_since > 600:
                        # 单次模式：10 分钟无输出 → 卡死
                        _watchdog_trip("单次模式超过 10 分钟无输出")
                        break
                    continue

                if raw is None:
                    break  # EOF
                line = raw.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                idle_since = time.time()
                if sleep_deadline is not None:
                    sleep_deadline = None  # 有输出说明已醒来

                # 数据行（__MARKET__/__MAINT__/__STATUS__/__AIRCRAFT__/__HUBS__/__SLEEP__）不写入日志，
                # 避免 JSON 长行污染 /api/log 翻页与下载（噪声）
                is_data_line = (
                    line.startswith("__MARKET__")
                    or line.startswith("__MAINT__")
                    or line.startswith("__STATUS__")
                    or line.startswith("__AIRCRAFT__")
                    or line.startswith("__FLEET_REMOVE__")
                    or line.startswith("__HUBS__")
                    or line.startswith("__SLEEP__")
                    or line.startswith("__NEXT_TAKEOFF__")
                    or line.startswith("__TAKEOVER_TAKEOFF__")
                )

                # ⭐ 脚本宣布睡眠：__SLEEP__{秒} → 设置看门狗到期时刻
                if line.startswith("__SLEEP__"):
                    try:
                        secs = int(line[len("__SLEEP__"):].strip())
                        sleep_deadline = time.time() + secs + _SLEEP_GRACE
                    except ValueError:
                        pass
                    configured_email = _current_env_credentials()[0]
                    if normalize_account(configured_email) != normalize_account(run_email):
                        _append_log("🔄 检测到账号配置变化：当前轮次已完成，停止旧账号循环并安全切换")
                        _run_proc.terminate()
                        break
                    continue

                # ⭐ 脚本直接输出市场 JSON 行: __MARKET__{json} → 写盘+更新缓存+推送 market-bar
                if line.startswith("__MARKET__"):
                    try:
                        mkt = json.loads(line[len("__MARKET__"):].strip())
                        with _market_rt_lock:
                            if _market_rt_cache is None:
                                _market_rt_cache = {}
                            _market_rt_cache.update(mkt)
                            _market_rt_ts = time.time()
                        _market_retry_success()
                        try:
                            run_paths["market"].write_text(json.dumps(mkt, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                        _broadcast_sse({"type": "market", "data": mkt})
                        continue
                    except Exception:
                        pass

                # ⭐ 脚本直接输出检修需求 JSON 行: __MAINT__{json} → 实时更新检修页
                if line.startswith("__MAINT__"):
                    try:
                        maint = json.loads(line[len("__MAINT__"):].strip())
                        with _maint_cache_lock:
                            _maint_cache = maint
                        _broadcast_sse({"type": "maint", "data": maint})
                        continue  # 不下游当普通日志
                    except Exception:
                        pass

                # 复用采集脚本已经抓过的主页全量状态，起飞待办无需立刻重复访问主页。
                if line.startswith("__STATUS__"):
                    try:
                        status_map = json.loads(line[len("__STATUS__"):].strip())
                        if isinstance(status_map, dict) and status_map:
                            with _maint_cache_lock:
                                _home_status_cache = status_map
                                _home_status_ts = time.time()
                        continue
                    except Exception:
                        pass

                # ⭐ 脚本直接输出枢纽 JSON 行: __HUBS__[names] → 前端刷新枢纽 chips/下拉
                if line.startswith("__HUBS__"):
                    try:
                        hubs = json.loads(line[len("__HUBS__"):].strip())
                        _broadcast_sse({"type": "hubs", "data": hubs})
                        continue
                    except Exception:
                        pass

                # ⭐ 全量扫描/首页状态发现飞机后：登记或合并对应起飞待办
                if line.startswith("__TAKEOVER_TAKEOFF__"):
                    try:
                        tk = json.loads(line[len("__TAKEOVER_TAKEOFF__"):].strip())
                        reg = tk.get("reg", "")
                        route_id = str(tk.get("route_id", ""))
                        trigger_at = float(tk.get("trigger_at", 0) or 0)
                        if route_id and trigger_at > time.time():
                            retrofit_block = _retrofit_blocks_takeoff(reg)
                            if retrofit_block:
                                _publish_log(
                                    f"🔧 {reg} 起飞待办已跳过：{retrofit_block}"
                                )
                                continue
                            reason = str(tk.get("reason", "") or "")
                            reason_label = (
                                "返场结束" if reason == "维护/改装完成" else reason
                            )
                            title = (f"对 {reg} 进行起飞前需求检查（航线 {route_id}）"
                                     if reason == "全量扫描发现" else
                                     f"{reg} {reason_label or '落地'}后接管起飞（航线 {route_id}）")
                            scheduled = _add_takeoff_task(
                                reg, route_id, int(tk.get("cost_index", 200)), trigger_at,
                                title,
                                fid=str(tk.get("fid", "")), jitter=0,
                                ready_at=float(tk.get("ready_at", 0) or 0),
                                reason=reason,
                            )
                            if (reason in {"维护/改装完成", "返场结束"}
                                    and (not scheduled.get("deduplicated")
                                         or scheduled.get("trigger_changed"))):
                                remaining = max(0, int(
                                    float(scheduled.get("trigger_at", trigger_at)) - time.time()
                                ))
                                _publish_log(
                                    f"{reg} 维护中，预计 {remaining // 60} 分钟后完成"
                                )
                        continue
                    except Exception:
                        pass

                # ⭐ 需求检查起飞成功后：按本次飞行落地时间排「下次起飞」待办
                if line.startswith("__NEXT_TAKEOFF__"):
                    try:
                        tk = json.loads(line[len("__NEXT_TAKEOFF__"):].strip())
                        reg = tk.get("reg", "")
                        route_id = str(tk.get("route_id", ""))
                        secs = int(tk.get("flight_secs", 0) or 0)
                        ci = int(tk.get("cost_index", 200))
                        if route_id and secs > 0:
                            delay = secs + _TAKEOFF_READY_BUFFER_SECONDS
                            ready_at = time.time() + secs
                            _add_pending_task(
                                "takeoff",
                                f"{reg} 下次起飞（航线 {route_id}）",
                                time.time() + delay,
                                {"route_id": route_id, "reg": reg, "cost_index": ci,
                                 "ready_at": ready_at, "reason": "本次飞行落地"},
                                jitter=0,
                            )
                        continue
                    except Exception:
                        pass

                # ⭐ 脚本直接输出 JSON 飞机数据行: __AIRCRAFT__{json}
                if line.startswith("__FLEET_REMOVE__"):
                    try:
                        removed = json.loads(line[len("__FLEET_REMOVE__"):])
                        _cancel_removed_aircraft_tasks(removed)
                        _broadcast_sse({"type": "fleet_remove", "data": removed})
                        continue
                    except Exception:
                        pass

                # ⭐ 脚本直接输出 JSON 飞机数据行: __AIRCRAFT__{json}
                if line.startswith("__AIRCRAFT__"):
                    try:
                        ac = json.loads(line[len("__AIRCRAFT__"):])
                        with _maint_cache_lock:
                            live_statuses = dict(_home_status_cache or {})
                        ac_status = live_statuses.get(str(ac.get("飞机ID", "")), {})
                        if not ac_status:
                            target_reg = str(ac.get("注册号", "")).strip().upper()
                            ac_status = next((item for item in live_statuses.values()
                                              if isinstance(item, dict) and str(
                                                  item.get("注册号", "")).strip().upper() == target_reg), {})
                        _decorate_operation_state(ac, ac_status)
                        ac_key = str(ac.get("飞机ID") or ac.get("注册号") or "")
                        if ac_key:
                            seen_aircraft_ids.add(ac_key)
                        current = len(seen_aircraft_ids)
                        _run_status["progress_current"] = current
                        if not _run_status["progress_total"]:
                            _run_status["progress_total"] = current
                        elif current > _run_status["progress_total"]:
                            # 实际采集量可能超过页面预估值（如机型分组统计偏差），以实际为准
                            _run_status["progress_total"] = current
                        _broadcast_sse({
                            "type": "aircraft",
                            "data": ac,
                            "count": current,
                            "total": _run_status["progress_total"],
                        })
                        continue  # 不下游当普通日志显示
                    except Exception:
                        pass

                # 普通文本日志直接显示；解析失败的数据行只给简短提示，避免长 JSON 刷屏
                if not is_data_line:
                    _append_log(line)
                    _broadcast_sse({"type": "log", "line": line})
                else:
                    tag = line.split("{", 1)[0].strip("_").strip()
                    _append_log(f"⚠ 数据行解析失败: {tag}")
                    _broadcast_sse({"type": "log", "line": f"⚠ 数据行解析失败: {tag}"})

                # 解析进度行（备用）
                m = _AIR_DETAIL_RE.search(line)
                if m:
                    total = int(m.group(2))
                    _run_status["progress_total"] = total

                    _broadcast_sse({
                        "type": "progress",
                        "current": _run_status.get("progress_current", 0),
                        "total": total,
                    })

            # 进程结束后清空残余行（防日志丢失；同样跳过数据行噪声）
            try:
                while True:
                    raw = line_q.get_nowait()
                    if raw is None:
                        break
                    line = raw.rstrip("\n").rstrip("\r")
                    if line.strip() and not (
                        line.startswith("__MARKET__")
                        or line.startswith("__MAINT__")
                        or line.startswith("__STATUS__")
                        or line.startswith("__AIRCRAFT__")
                        or line.startswith("__FLEET_REMOVE__")
                        or line.startswith("__HUBS__")
                        or line.startswith("__SLEEP__")
                        or line.startswith("__NEXT_TAKEOFF__")
                        or line.startswith("__TAKEOVER_TAKEOFF__")
                    ):
                        _append_log(line)
                        _broadcast_sse({"type": "log", "line": line})
            except queue.Empty:
                pass

            _run_proc.wait()
            if (not _stop_requested and not _run_status["error"]
                    and _run_proc.returncode not in (0, -15)):
                _run_status["error"] = f"脚本退出码: {_run_proc.returncode}"
        except Exception as e:
            _run_status["error"] = str(e)
        finally:
            # 完成事件发出前保持 running=True；账号切换会等到旧账号结果全部收尾。
            with _run_lock:
                _run_status["mode"] = ""
                _run_status["last_run"] = _now_bjt().strftime("%Y-%m-%d %H:%M:%S")
                _run_proc = None
                fleet = _read_csv(run_paths["fleet"])
                maint = _read_csv(run_paths["maint"])
                _broadcast_sse({
                    "type": "done",
                    "error": _run_status["error"],
                    "last_run": _run_status["last_run"],
                    "fleet_count": len(fleet),
                    "maint_count": len(maint),
                    "refresh": True,
                })
                _run_status["running"] = False
            _sync_account_context()
            _ensure_marketing_tasks()

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "msg": "脚本已启动"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _require_csrf()
    global _run_status, _run_proc, _stop_requested
    with _run_lock:
        if not _run_status["running"]:
            return jsonify({"ok": False, "msg": "没有正在运行的任务"})
        if _run_proc and _run_proc.poll() is None:
            _stop_requested = True
            _run_proc.terminate()
            msg = "⏹ 用户手动停止\n"
            _append_log(msg)
            _broadcast_sse({"type": "log", "line": msg})
    return jsonify({"ok": True, "msg": "已发送停止信号"})


# 所有调度器会调用的辅助函数和路由均已定义后，再恢复任务并启动线程。
_load_pending_tasks()
if os.environ.get("AM4_DISABLE_SCHEDULER", "").strip() != "1":
    threading.Thread(target=_pending_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("AM4_PORT", "5000")), debug=False)
