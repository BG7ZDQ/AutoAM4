"""把全量扫描发现的可运营飞机登记为起飞待办。

每天 06:00（以及启动时发现当日尚未完成）的全量扫描负责发现飞机；真正起飞前，由待办只刷新
本机详情并检查最新需求。这样无需每四小时再次逐架访问整支机队。
"""
import json
import os
import time

from collector import TAKEOFF_READY_BUFFER_SECONDS, _clean_num


_COST_INDEX = int(os.environ.get("AM4_COST_INDEX", "200"))
_MIN_A_CHECK_HOURS = max(0.0, float(os.environ.get("AM4_MIN_A_CHECK_HOURS", "5")))
_MAX_WEAR_FOR_TAKEOFF = min(100.0, max(
    0.0, float(os.environ.get("AM4_MAX_WEAR_FOR_TAKEOFF", "80"))))


def _maintenance_block(row: dict) -> str | None:
    """返回阻止自动起飞的检修原因；缺少数据时不误拦截。"""
    try:
        a_raw = str(row.get("距A-Check小时", "")).strip()
        wear_raw = str(row.get("损坏率%", "")).strip()
        if a_raw and _clean_num(a_raw) <= _MIN_A_CHECK_HOURS:
            return f"距A-Check {a_raw}h"
        if wear_raw and _clean_num(wear_raw) >= _MAX_WEAR_FOR_TAKEOFF:
            return f"损坏率 {wear_raw}%"
    except (TypeError, ValueError):
        pass
    return None


def _takeover_trigger(status: dict, now: float | None = None) -> float:
    """在飞飞机的接管时间：预计落地后 2 分钟；已落地/字段缺失返回 0。"""
    try:
        arrival = float(status.get("预计落地时间戳", 0) or 0)
        now = time.time() if now is None else now
        return (arrival + TAKEOFF_READY_BUFFER_SECONDS
                if arrival > now + 60 else 0.0)
    except (TypeError, ValueError):
        return 0.0


def enqueue_strong_demand(rows: list[dict],
                          status_map: dict[str, dict] | None = None) -> None:
    """为全量扫描发现的需求旺盛飞机登记待办，不在采集进程直接起飞。"""
    status_map = status_map or {}
    now = time.time()
    for row in rows:
        block = _maintenance_block(row)
        if block:
            print(f"{row.get('注册号', '')} 处于检修保护（{block}）状态，暂不登记", flush=True)
            continue

        aircraft_id = str(row.get("飞机ID", ""))
        reg = row.get("注册号", "")
        aircraft_status = status_map.get(aircraft_id, {})
        if str(aircraft_status.get("停飞", "0") or "0").strip() not in {"", "0", "false", "False"}:
            print(f"{reg} 已人工停飞，暂不登记自动起飞", flush=True)
            continue
        route_id = str(aircraft_status.get("航线ID", ""))
        if not route_id:
            print(f"{reg} 尚未取得航线 ID，暂不登记", flush=True)
            continue

        trigger_at = _takeover_trigger(aircraft_status, now)
        if trigger_at:
            reason = "落地"
            remaining = max(0, int(trigger_at - now))
            print(f"{reg} 飞行中，预计 {remaining // 60} 分钟后落地", flush=True)
        else:
            trigger_at = now + TAKEOFF_READY_BUFFER_SECONDS
            reason = "全量扫描发现"
            print(f"{reg} 已登记起飞，{TAKEOFF_READY_BUFFER_SECONDS // 60} 分钟后检查", flush=True)

        payload = json.dumps({
            "fid": aircraft_id,
            "reg": reg,
            "route_id": route_id,
            "ready_at": trigger_at - TAKEOFF_READY_BUFFER_SECONDS,
            "trigger_at": trigger_at,
            "cost_index": _COST_INDEX,
            "reason": reason,
        }, ensure_ascii=False)
        print(f"__TAKEOVER_TAKEOFF__{payload}", flush=True)
