"""自动补货：低价时补充资源，同时保留玩家设定的现金安全垫。

启动全量采集和每次轻量更新（北京时间 00/30 整点）都会检查；
因为页面已抓取，检查零额外请求；买入后容量满 remCapacity=0 自然不再买。
"""
import os
import re
from collector import _do_curl, HOME

_FUEL_THRESHOLD = float(os.environ.get("AM4_FUEL_BUY_BELOW", "500"))
_CO2_THRESHOLD = float(os.environ.get("AM4_CO2_BUY_BELOW", "125"))
_CASH_RESERVE = max(0.0, float(os.environ.get("AM4_CASH_RESERVE", "5000000")))
_MAX_RESOURCE_SPEND = max(0.0, float(os.environ.get("AM4_MAX_RESOURCE_SPEND", "25000000")))


def _num(s):
    return float(s.replace(",", "").strip())


def _price_cap(html: str, price_pat: str):
    """从已抓取的页面解析 (价格每1000, 剩余可购容量or0)。"""
    pm = re.search(price_pat, html)
    rm = re.search(r"id='remCapacity'>([^<]+)<", html)
    price = _num(pm.group(1)) if pm else None
    cap = _num(rm.group(1)) if rm and rm.group(1).strip() else 0
    return price, cap


def _buy_amount(price: float, capacity: float, balance: float | None) -> int:
    """按价格（每1000单位）计算安全购买量；未知余额时保持旧行为。"""
    if price <= 0 or capacity <= 0:
        return 0
    if balance is None:
        return int(capacity)
    spendable = max(0.0, balance - _CASH_RESERVE)
    if _MAX_RESOURCE_SPEND > 0:
        spendable = min(spendable, _MAX_RESOURCE_SPEND)
    return max(0, min(int(capacity), int(spendable * 1000.0 / price)))


def _buy(url: str, amount: int, unit: str, label: str):
    """执行购买：fuel.php?mode=do&amount=N 或 co2.php?mode=do&amount=N。"""
    full = f"{url}&mode=do&amount={amount}" if "?" in url else f"{url}?mode=do&amount={amount}"
    try:
        _do_curl(full, data=None, output=None, referer=HOME)
        print(f"✅ 已买入 {amount:,} {unit} {label}", flush=True)
        return True
    except Exception as e:
        print(f"⚠ {label} 买入失败: {e}", flush=True)
        return False


def _print_wait_for_price(label: str, price: float, threshold: float,
                          capacity: float, unit: str) -> None:
    """本轮不补货的说明"""
    display = f"{label} " if label == "CO₂" else label
    if capacity <= 0:
        print(f"{display}已达配额极限，暂不补货。", flush=True)
        return
    print(f"{display}价格过高 (${price:g}/1000{unit})，暂不补货。",flush=True,)


def auto_buy(fuel_html: str, co2_html: str, balance: str | float | None = None,
             *, buy_fuel: bool = True, buy_co2: bool = True):
    """启动及轻量轮次调用，直传本次已抓取的 fuel/co2 页面 HTML。

    buy_fuel / buy_co2：面板「设置」中的操作开关，关闭时跳过对应采购。
    """
    purchased = {"fuel": 0, "co2": 0}
    try:
        try:
            cash = _num(str(balance)) if balance not in (None, "") else None
        except (TypeError, ValueError):
            cash = None
        round_budget = _MAX_RESOURCE_SPEND if _MAX_RESOURCE_SPEND > 0 else None
        if not buy_fuel:
            print("自动买油已关闭，跳过燃油采购", flush=True)
        else:
            fp, fcap = _price_cap(
                fuel_html, r"現在價格：</span><br><span class='text-danger'><b>\$\s*([\d,]+)</b>")
            if fp is not None and fp < _FUEL_THRESHOLD and fcap > 0:
                amount = _buy_amount(fp, fcap, cash)
                if round_budget is not None:
                    amount = min(amount, int(round_budget * 1000.0 / fp))
                if amount:
                    if _buy("https://www.airlinemanager.com/fuel.php", amount, "Lbs", "燃油"):
                        purchased["fuel"] = amount
                        if cash is not None:
                            cash = max(0.0, cash - amount * fp / 1000.0)
                        if round_budget is not None:
                            round_budget = max(0.0, round_budget - amount * fp / 1000.0)
                else:
                    print(f"当前余额低于安全阈值 (${_CASH_RESERVE:,.0f})，跳过燃油购买", flush=True)
            else:
                _print_wait_for_price(
                    "燃油", fp, _FUEL_THRESHOLD, fcap, "Lbs"
                ) if fp is not None else None
        if not buy_co2:
            print("自动买 CO₂ 已关闭，跳过 CO₂ 采购", flush=True)
        else:
            cp, ccap = _price_cap(
                co2_html, r"每CO2配額價格</span><br><span class='text-danger'><b>\$\s*([\d,]+)</b>")
            if cp is not None and cp < _CO2_THRESHOLD and ccap > 0:
                amount = _buy_amount(cp, ccap, cash)
                if round_budget is not None:
                    amount = min(amount, int(round_budget * 1000.0 / cp))
                if amount:
                    if _buy("https://www.airlinemanager.com/co2.php", amount, "", "CO2 配额"):
                        purchased["co2"] = amount
                else:
                    print(f"当前余额低于安全阈值 (${_CASH_RESERVE:,.0f})，跳过 CO₂ 购买", flush=True)
            else:
                _print_wait_for_price(
                    "CO₂", cp, _CO2_THRESHOLD, ccap, "配额"
                ) if cp is not None else None
    except Exception as e:
        print(f"⚠ 自动补货检查异常: {e}", flush=True)
    return purchased
