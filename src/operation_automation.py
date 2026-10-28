"""自动检修和货运营销的只读报价、阈值与资金校验。"""
from __future__ import annotations

import html
import math
import re


class MaintenanceLocationUnavailable(ValueError):
    """官网明确拒绝当前位置的操作；不同于网络、费用或解析失败。"""


def number(value) -> float | None:
    try:
        result = float(str(value).replace(",", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def maintenance_action(status: dict, settings: dict) -> str | None:
    hours = number(status.get("距A-Check小时"))
    wear = number(status.get("损坏率%"))
    if hours is not None and hours <= settings.get("min_a_check_hours", 5):
        return "check"  # A-Check 总价已经包含维修，不再另买维修。
    if wear is not None and wear >= settings.get("max_wear_for_takeoff", 80):
        return "repair"
    return None


def check_cash(home: str, cost: float, reserve: float) -> None:
    match = re.search(r"id=['\"]headerAccount['\"][^>]*>\s*\$?\s*([\d,]+(?:\.\d+)?)\s*<", home)
    balance = number(match.group(1)) if match else None
    if balance is None or not math.isfinite(cost) or cost <= 0:
        raise ValueError("余额或费用无法确认，未执行购买")
    if not math.isfinite(reserve) or reserve < 0:
        raise ValueError("现金安全垫无效，未执行购买")
    if cost > max(0, balance - reserve):
        raise ValueError(f"现金保护：费用 ${cost:,.0f}，可用 ${max(0, balance-reserve):,.0f}")


def cargo_marketing_cost(page: str) -> float:
    """只接受官网已核实的广告 4 / d=6 表单及其价格公式。"""
    body = html.unescape(page or "")
    if not re.search(r"marketing_new\.php\?type=2&c=4&mode=do&d=", body):
        raise ValueError("货运营销报价表单无法确认")
    if not re.search(r"<option\b[^>]*value=['\"]6['\"][^>]*>\s*24\s*(?:小時|小时|Hours)", body, re.I):
        raise ValueError("货运营销 24 小时选项无法确认")
    # 六档时长，最后 else 分支为 24 小时；未知公式拒绝购买。
    factor = re.search(r"else\s*\{\s*var costFactor\s*=\s*([\d.]+)\s*;\s*\}", body)
    base = re.search(r"var c4\s*=\s*(\d+)\s*\*\s*costFactor\s*;", body)
    shown = re.search(r"id=['\"]c4['\"][^>]*>\s*([\d,]+)\s*<", body)
    if (not factor or not base or not shown
            or number(factor.group(1)) != 4.5
            or number(base.group(1)) != number(shown.group(1))):
        raise ValueError("货运营销费用公式无法确认")
    cost = float(base.group(1)) * float(factor.group(1))
    if cost <= 0:
        raise ValueError("货运营销费用无效")
    return cost


def maintenance_quote(page: str, fid: str, action: str) -> dict:
    """从对应操作区读取总价，严格校验目标 ID 和可用按钮。"""
    if not str(fid).isdigit() or action not in {"check", "repair"}:
        raise ValueError("检修目标无效")
    location = re.search(r"<div\b[^>]*id=['\"]typeCheck['\"][^>]*>(.*?)<div\b[^>]*id=['\"]typeRepair['\"]",
                         page or "", re.S | re.I)
    if location and re.search(r"飛機不在樞紐，或現時不在前往樞紐|飞机不在枢纽，或现时不在前往枢纽", location.group(1)):
        raise MaintenanceLocationUnavailable("飞机不在枢纽或未飞往枢纽，等待返场")
    section_id = "typeCheck" if action == "check" else "typeRepair"
    next_id = "typeRepair" if action == "check" else "typeModify"
    match = re.search(
        rf"<div\b[^>]*id=['\"]{section_id}['\"][^>]*>(.*?)<div\b[^>]*id=['\"]{next_id}['\"]",
        page or "", re.S | re.I)
    if not match:
        raise ValueError("检修报价页面无法确认")
    section = html.unescape(match.group(1))
    path = f"maint_plan_do.php?mode=do&type={action}&id={fid}"
    buttons = re.findall(r"<button\b[^>]*>.*?</button>", section, re.S | re.I)
    if not any((path + "'") in button and not re.search(
            r"\bdisabled\b|not-active", button, re.I) for button in buttons):
        if re.search(r"飛機不在樞紐，或現時不在前往樞紐|飞机不在枢纽，或现时不在前往枢纽", section):
            raise MaintenanceLocationUnavailable("飞机不在枢纽或未飞往枢纽，等待返场")
        raise ValueError("游戏暂不允许此项检修（可能不在基地或已有维护）")
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", section))
    price = re.search(r"(?:總價格|总价格|Total price)\s*\$\s*([\d,]+)", plain, re.I)
    duration = re.search(r"(?:檢修時間|检修时间|維修時間|维修时间)\s*(?:(\d+)d\s+)?(\d+):(\d{2}):(\d{2})(?![\d:])", plain, re.I)
    if not price or not duration:
        raise ValueError("检修总价或时长无法确认")
    cost = number(price.group(1))
    days, hours, minutes, secs = (int(v or 0) for v in duration.groups())
    if minutes >= 60 or secs >= 60 or (days and hours >= 24):
        raise ValueError("检修时长格式无效")
    seconds = days*86400 + hours*3600 + minutes*60 + secs
    if cost is None or cost <= 0 or seconds <= 0:
        raise ValueError("检修总价或时长无效")
    return {"path": path, "cost": cost, "seconds": seconds}
