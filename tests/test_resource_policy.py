"""?????????/CO? ???????????????"""
from bootstrap import *


class ResourcePolicyTests(unittest.TestCase):
    def test_default_resource_price_thresholds(self):
        self.assertEqual(
            auto_buy._FUEL_THRESHOLD,
            float(os.environ.get("AM4_FUEL_BUY_BELOW", "500")),
        )
        self.assertEqual(
            auto_buy._CO2_THRESHOLD,
            float(os.environ.get("AM4_CO2_BUY_BELOW", "125")),
        )

    def test_route_load_defaults_are_unified(self):
        self.assertEqual(route_planner.DEFAULT_PAX_LOAD, 0.95)
        self.assertEqual(route_planner.DEFAULT_CARGO_LOAD, 0.95)

    def test_preclose_high_prices_make_no_online_request(self):
        at = datetime(2026, 8, 9, 16, 29, tzinfo=collector.BJT)
        with tempfile.TemporaryDirectory() as temp_dir:
            market_path = Path(temp_dir) / "market.json"
            market_path.write_text(json.dumps({
                "balance": "100000000", "fuel_price": "500", "co2_price": "125",
                "updated_at": "2026-08-09 16:00:00",
            }), encoding="utf-8")
            with patch.object(collector, "MARKET_JSON", market_path), \
             patch.object(collector, "_now_bjt", return_value=at), \
                 patch.object(auto_buy, "_FUEL_THRESHOLD", 500), \
                 patch.object(auto_buy, "_CO2_THRESHOLD", 125), \
                 patch.object(collector, "fetch") as fetch:
                collector.run_preclose_topup()
        fetch.assert_not_called()

    def test_preclose_only_rechecks_cached_low_resource(self):
        at = datetime(2026, 8, 9, 16, 29, tzinfo=collector.BJT)
        with tempfile.TemporaryDirectory() as temp_dir:
            market_path = Path(temp_dir) / "market.json"
            market_path.write_text(json.dumps({
                "balance": "100000000", "fuel_price": "499", "co2_price": "125",
                "updated_at": "2026-08-09 16:00:00",
            }), encoding="utf-8")
            with patch.object(collector, "MARKET_JSON", market_path), \
             patch.object(collector, "_now_bjt", return_value=at), \
                 patch.object(auto_buy, "_FUEL_THRESHOLD", 500), \
                 patch.object(auto_buy, "_CO2_THRESHOLD", 125), \
                 patch.object(collector, "fetch", return_value="fuel-page") as fetch, \
                 patch.object(auto_buy, "auto_buy", return_value={"fuel": 0, "co2": 0}) as buy, \
                 patch.object(collector, "apply_purchase_to_market", return_value=False):
                collector.run_preclose_topup()
        fetch.assert_called_once_with(
            collector.FUEL,
            referer=collector.HOME,
            label="正在更新燃油信息",
        )
        buy.assert_called_once_with(
            "fuel-page", "", "100000000", buy_fuel=True, buy_co2=True)

    def test_preclose_stale_cache_refreshes_current_cycle_prices(self):
        at = datetime(2026, 8, 9, 16, 59, tzinfo=collector.BJT)
        fresh = {
            "balance": "90000000", "fuel_price": "400", "co2_price": "100",
            "updated_at": "2026-08-09 16:59:00",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            market_path = Path(temp_dir) / "market.json"
            market_path.write_text(json.dumps({
                "balance": "100000000", "fuel_price": "2000", "co2_price": "200",
                "updated_at": "2026-08-09 16:00:00",
            }), encoding="utf-8")
            with patch.object(collector, "MARKET_JSON", market_path), \
                 patch.object(collector, "_now_bjt", return_value=at), \
                 patch.object(collector, "fetch",
                              side_effect=["home-page", "fuel-page", "co2-page"]) as fetch, \
                 patch.object(collector, "parse_market_data",
                              return_value=fresh), \
                 patch.object(collector, "save_market_data"), \
                 patch.object(auto_buy, "auto_buy",
                              return_value={"fuel": 0, "co2": 0}) as buy:
                collector.run_preclose_topup()
        self.assertEqual(fetch.call_count, 3)
        buy.assert_called_once_with(
            "fuel-page", "co2-page", "90000000", buy_fuel=True, buy_co2=True)

    def test_purchase_respects_reserve_and_capacity(self):
        with patch.object(auto_buy, "_CASH_RESERVE", 5_000_000), \
             patch.object(auto_buy, "_MAX_RESOURCE_SPEND", 25_000_000):
            self.assertEqual(auto_buy._buy_amount(400, 100_000_000, 10_000_000), 12_500_000)
            self.assertEqual(auto_buy._buy_amount(400, 1_000_000, 10_000_000), 1_000_000)
            self.assertEqual(auto_buy._buy_amount(400, 1_000_000, 4_000_000), 0)

    def test_unknown_balance_keeps_capacity_behavior(self):
        self.assertEqual(auto_buy._buy_amount(400, 12345, None), 12345)

    def test_fuel_and_co2_share_one_round_budget(self):
        bought = []
        with patch.object(auto_buy, "_CASH_RESERVE", 0), \
             patch.object(auto_buy, "_MAX_RESOURCE_SPEND", 1_000), \
             patch.object(auto_buy, "_price_cap", side_effect=[(100, 8_000), (100, 8_000)]), \
             patch.object(auto_buy, "_buy", side_effect=lambda _u, n, _unit, label: bought.append((label, n)) or True):
            auto_buy.auto_buy("fuel", "co2", 10_000)
        self.assertEqual(bought, [("燃油", 8_000), ("CO2 配额", 2_000)])

    def test_purchase_immediately_updates_market_snapshot(self):
        market = {
            "balance": "145,655,579", "fuel_qty": "12,403,485",
            "fuel_price": "1370", "co2_qty": "8,082,932", "co2_price": "104",
        }
        with patch.object(collector, "save_market_data"), \
             patch("builtins.print"):
            changed = collector.apply_purchase_to_market(
                market, {"fuel": 0, "co2": 10_120_568})
        self.assertTrue(changed)
        self.assertEqual(market["co2_qty"], "18,203,500")
        self.assertEqual(market["balance"], "144,603,040")


