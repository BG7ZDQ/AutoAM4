import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 服务端测试使用独立输出根目录且不启动后台线程，绝不接触真实账号运行数据。
_TEST_OUTPUTS = tempfile.TemporaryDirectory()
os.environ["AM4_OUTPUTS_DIR"] = _TEST_OUTPUTS.name
os.environ["AM4_DISABLE_SCHEDULER"] = "1"
os.environ["AM4_EMAIL"] = "tests@example.invalid"
os.environ["AM4_PASSWORD"] = "test-password"
# 面板数据库放在独立临时目录，避免被 _migrate_legacy_outputs 移走
_TEST_DB_DIR = tempfile.mkdtemp()
os.environ["AM4_PANEL_DB"] = os.path.join(_TEST_DB_DIR, "panel.db")

import auto_buy
import account_storage
import collector
import fresh_demand
import panel_store
import route_planner
import server
import storage_utils

# 面板已启用登录鉴权：测试统一以管理员身份登录，并绑定测试账号数据。
_TEST_ADMIN_ID = panel_store.create_user(
    "testadmin", "test-pass-1", is_admin=True, status="active",
    am4_email="tests@example.invalid", am4_password="test-password",
)
server._effective_user = lambda: panel_store.get_user_by_id(_TEST_ADMIN_ID)


class DailyScheduleTests(unittest.TestCase):
    def test_full_refresh_matches_game_reset_hour(self):
        self.assertEqual(collector.DAILY_FULL_HOUR_BJT, 6)

    def test_collection_elapsed_time_is_human_readable(self):
        self.assertEqual(collector._format_elapsed(0.4), "0秒")
        self.assertEqual(collector._format_elapsed(389), "6分29秒")
        self.assertEqual(collector._format_elapsed(3661), "1小时1分1秒")

    def test_normal_and_preclose_slots_are_both_scheduled(self):
        at = lambda h, m, s=0: datetime(2026, 8, 9, h, m, s,
                                       tzinfo=collector.BJT)
        self.assertEqual(collector._next_cycle_ts(at(7, 20)), at(7, 29))
        self.assertEqual(collector._next_cycle_ts(at(7, 29)), at(7, 30))
        self.assertEqual(collector._next_cycle_ts(at(7, 30)), at(7, 59))
        self.assertEqual(collector._next_cycle_ts(at(7, 59, 1)), at(8, 0))
        self.assertEqual(
            collector._next_cycle_ts(at(23, 59, 1)),
            datetime(2026, 8, 10, 0, 0, tzinfo=collector.BJT),
        )

    def test_29_and_59_are_preclose_slots(self):
        at = lambda m: datetime(2026, 8, 9, 7, m,
                                tzinfo=collector.BJT)
        self.assertTrue(collector._is_preclose_slot(at(29)))
        self.assertTrue(collector._is_preclose_slot(at(59)))
        self.assertFalse(collector._is_preclose_slot(at(0)))

    def test_daily_full_completion_state_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "schedule_state.json"
            with patch.object(collector, "SCHEDULE_STATE_JSON", state_path):
                self.assertEqual(collector._load_last_full_date(), "")
                collector._save_last_full_date("2026-08-09")
                self.assertEqual(
                    collector._load_last_full_date(), "2026-08-09")

    def test_current_csv_drops_records_absent_from_latest_snapshot(self):
        fields = ["检修ID", "飞机注册号", "最后更新时间"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "maintenance_checks.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"检修ID": "old", "飞机注册号": "OLD-1"})
            count = collector.save_current_csv(
                path, fields,
                [{"检修ID": "current", "飞机注册号": "NOW-1"}],
                "检修ID", "2026-08-12 20:00:00",
            )
            rows = collector.load_existing_csv(path)
        self.assertEqual(count, 1)
        self.assertNotIn("old", rows)
        self.assertEqual(rows["current"]["飞机注册号"], "NOW-1")

    def test_empty_current_csv_clears_finished_maintenance(self):
        fields = ["检修ID", "飞机注册号", "最后更新时间"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "maintenance_checks.csv"
            path.write_text(
                "检修ID,飞机注册号,最后更新时间\nold,OLD-1,2026-08-11 00:00:00\n",
                encoding="utf-8-sig",
            )
            count = collector.save_current_csv(
                path, fields, [], "检修ID", "2026-08-12 20:00:00")
            rows = collector.load_existing_csv(path)
        self.assertEqual(count, 0)
        self.assertEqual(rows, {})


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


class PlayerSafetyTests(unittest.TestCase):
    def test_maintenance_protection(self):
        self.assertIsNotNone(fresh_demand._maintenance_block({"距A-Check小时": "4", "损坏率%": "10"}))
        self.assertIsNotNone(fresh_demand._maintenance_block({"距A-Check小时": "20", "损坏率%": "85"}))
        self.assertIsNone(fresh_demand._maintenance_block({"距A-Check小时": "20", "损坏率%": "30"}))

    def test_estimate_includes_investment_metrics(self):
        aircraft = {
            "type": "0", "speed": 800, "capacity": 100, "range": 10000,
            "fuel": 5, "co2": 0.1, "check_cost": 10000, "maint": 500,
            "cost": 10_000_000,
        }
        origin = {"id": "1", "lat": 0, "lng": 0}
        dest = {"id": "2", "lat": 0, "lng": 10}
        result = route_planner.am4_estimate(
            aircraft, origin, dest, 1, demand=(10000, 10000, 10000)
        )
        self.assertEqual(result["initial_investment"],
                         result["aircraft_cost"] + result["creation_cost"])
        self.assertGreater(result["net_per_day"], 0)
        self.assertGreater(result["payback_days"], 0)
        self.assertGreater(result["roi_30d_pct"], 0)

    def test_maximise_finds_best_feasible_frequency_for_a380_from_shenzhen(self):
        aircraft = route_planner.aircraft_by_name("A380-800")
        origin = route_planner.airport_by_id("3911")  # Shenzhen (SZX)
        fixed = route_planner.candidate_routes(aircraft, origin, 6, limit=10)
        maximised = route_planner.candidate_routes(
            aircraft, origin, 6, limit=10, maximize=True)
        self.assertEqual(fixed, [])
        self.assertGreater(len(maximised), 0)
        for candidate in maximised:
            self.assertGreaterEqual(candidate["tpd"], 1)
            self.assertLessEqual(candidate["tpd"], candidate["max_tpd"])
            self.assertLessEqual(candidate["tpd"], 20)
            self.assertIsNotNone(candidate["net_per_day"])

    def test_cargo_modify_page_reads_checked_inputs_beyond_nested_divs(self):
        page = """
        <div id='typeModify'><div><div><div>cargo layout</div></div></div>
        <div class='later-controls'>
          <input type="checkbox" class='mod-check' id='mod1' disabled checked="checked">
          <input checked='checked' id="mod2" class='mod-check' type="checkbox" disabled>
          <input type="checkbox" disabled id='mod3' checked>
        </div><script>var x='modType=cargo';</script>
        """
        self.assertEqual(collector.parse_modify_page(page), {
            "mod1_completed": True, "mod2_completed": True, "mod3_completed": True,
        })


class MarketingScheduleTests(unittest.TestCase):
    def test_relogin_must_reach_authenticated_homepage(self):
        with patch.object(
            collector, "_do_curl",
            side_effect=["login-page", "login-response", "login-page"],
        ), patch.object(collector, "_mark_account") as mark:
            with self.assertRaisesRegex(RuntimeError, "重新登录未成功"):
                collector._relogin()
        mark.assert_not_called()

    def test_marketing_purchase_can_reuse_known_inactive_page(self):
        active = "<tr><td><span class='glyphicons glyphicons-leaf'></span></td><td id='eTimer'></td></tr><script>timer('eTimer',43200);</script>"
        with patch.object(collector, "_ensure_login"), \
             patch.object(collector, "fetch", return_value=active) as fetch, \
             patch.object(collector, "_do_curl", return_value="ok"), \
             patch.object(collector.time, "sleep"):
            ok, _message, remaining = collector._purchase_marketing(
                "eco_12h", "环保营销（12 小时）", known_inactive=True)
        self.assertTrue(ok)
        self.assertEqual(remaining, 43200)
        self.assertEqual(fetch.call_count, 1)

    def test_marketing_purchase_never_writes_after_invalid_precheck(self):
        with patch.object(collector, "_ensure_login"), \
             patch.object(collector, "fetch", return_value=""), \
             patch.object(collector, "_do_curl") as write:
            ok, message, _remaining = collector._purchase_marketing(
                "eco_12h", "环保营销（12 小时）")
        self.assertFalse(ok)
        self.assertIn("未执行购买", message)
        write.assert_not_called()

    def test_purchase_failure_is_redetectable(self):
        self.assertIn("不足", collector._marketing_response_error(
            "<div class='alert'>餘額不足，無法購買</div>"
        ))
        self.assertEqual(collector._marketing_response_error(
            "<div class='alert alert-success'>Campaign started</div>"
        ), "")

    def test_active_marketing_remaining_time(self):
        page = """
        <tr><td><span class='glyphicons glyphicons-star'></span> 航空聲譽</td><td id='aTimer'></td></tr>
        <tr><td><span class='glyphicons glyphicons-leaf'></span> 環保</td><td id='eTimer'></td></tr>
        <script>timer('aTimer',18922);timer('eTimer',1200);</script>
        """
        self.assertEqual(
            collector._parse_active_marketing(page),
            {"airline": 18922, "eco": 1200},
        )


class FleetReconciliationTests(unittest.TestCase):
    def test_sold_aircraft_removed_only_for_complete_inventory(self):
        existing = {
            "1": {"飞机ID": "1", "注册号": "KEEP"},
            "2": {"飞机ID": "2", "注册号": "SOLD"},
        }
        removed = collector._reconcile_removed_aircraft(existing, {"1"}, 1)
        self.assertEqual([row["飞机ID"] for row in removed], ["2"])
        self.assertEqual(set(existing), {"1"})

    def test_partial_inventory_never_deletes(self):
        existing = {
            "1": {"飞机ID": "1", "注册号": "KEEP"},
            "2": {"飞机ID": "2", "注册号": "UNKNOWN"},
        }
        removed = collector._reconcile_removed_aircraft(existing, {"1"}, 2)
        self.assertEqual(removed, [])
        self.assertEqual(set(existing), {"1", "2"})

    def test_build_placeholder_is_never_mistaken_for_sold_aircraft(self):
        existing = {
            "1": {"飞机ID": "1", "注册号": "KEEP"},
            "B-NEW-1": {
                "飞机ID": "B-NEW-1", "注册号": "NEW-1", "建设状态": "建设中",
            },
        }
        removed = collector._reconcile_removed_aircraft(existing, {"1"}, 1)
        self.assertEqual(removed, [])
        self.assertIn("B-NEW-1", existing)

    def test_light_refresh_upgrades_build_placeholder_to_real_aircraft_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            placeholder = {
                "飞机ID": "B-MC-21-4-90", "注册号": "MC-21-4-90",
                "建设状态": "建设中", "枢纽分类": "Singapore",
                "起飞机场名称": "Singapore", "到达机场名称": "Ningbo",
            }
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=collector.CSV_FIELDNAMES,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerow(placeholder)
            fresh = {"飞机ID": "90", "注册号": "MC-21-4-90",
                     "枢纽分类": "其他", "飞行时长": "00:00:00"}
            with patch.object(collector, "FLEET_CSV", fleet):
                collector._write_light_fleet_snapshot(
                    {"B-MC-21-4-90": placeholder}, [fresh], [], {}, [])
                rows = collector.load_existing_csv(fleet)
        self.assertEqual(set(rows), {"90"})
        self.assertEqual(rows["90"]["建设状态"], "建设中")
        self.assertEqual(rows["90"]["枢纽分类"], "Singapore")
        self.assertEqual(rows["90"]["到达机场名称"], "Ningbo")

    def test_full_snapshot_collapses_placeholder_after_real_id_appears(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            fields = ["飞机ID", "注册号", "建设状态", "枢纽分类", "最后更新时间"]
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"飞机ID": "B-NEW-1", "注册号": "NEW-1",
                                 "建设状态": "建设中", "枢纽分类": "Singapore"})
            real = {"飞机ID": "90", "注册号": "NEW-1",
                    "建设状态": "建设中", "枢纽分类": "Singapore"}
            with patch.object(collector, "FLEET_CSV", fleet), \
                 patch.object(collector, "CSV_FIELDNAMES", fields):
                collector._write_full_fleet_snapshot([real])
                rows = collector.load_existing_csv(fleet)
        self.assertEqual(set(rows), {"90"})

    def test_home_status_supports_airborne_takeover(self):
        page = """
        statusData[15599652] = {
            reg: 'MC-21-4-1', icon: 9, routeId: 27196446,
            hoursToCheck: 188, cargo: 0, wear: 28.90,
            maintEnd: 1786214892, arrived: 1786211987, grounded: 0, end: 6693
        };
        """
        status = collector.parse_status_data(page)["15599652"]
        self.assertEqual(status["航线ID"], "27196446")
        self.assertEqual(status["维护改装结束时间戳"], "1786214892")
        self.assertEqual(status["预计落地时间戳"], "1786211987")
        self.assertEqual(status["剩余飞行秒数"], "6693")
        self.assertEqual(fresh_demand._takeover_trigger(status, now=1786210000), 1786212107)
        self.assertEqual(fresh_demand._takeover_trigger(status, now=1786211980), 0)

        plans = collector._maintenance_takeovers(
            {"15599652": status}, now=1786210000)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["trigger_at"], 1786215012)
        self.assertEqual(plans[0]["reason"], "返场结束")
        self.assertEqual(
            collector._maintenance_takeovers(
                {"15599652": status}, now=1786214900),
            [],
        )

    def test_maintenance_takeover_survives_missing_check_fields(self):
        page = """
        statusData[42] = {
            reg: 'B757-MOD', icon: 9, routeId: 7654,
            maintEnd: 1786214892, arrived: 0, grounded: 1, end: 0
        };
        """
        status = collector.parse_status_data(page)["42"]
        self.assertEqual(status["距A-Check小时"], "")
        self.assertEqual(status["损坏率%"], "")
        plans = collector._maintenance_takeovers(
            {"42": status}, now=1786210000)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["route_id"], "7654")

    def test_light_snapshot_preserves_server_refreshed_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            latest = {key: "" for key in collector.CSV_FIELDNAMES}
            latest.update({"飞机ID": "fid-1", "注册号": "SAFE-1",
                           "经济舱需求": "999", "距A-Check小时": "20", "损坏率%": "5"})
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=collector.CSV_FIELDNAMES,
                    extrasaction="ignore")
                writer.writeheader()
                writer.writerow(latest)
            stale = dict(latest)
            stale["经济舱需求"] = "100"
            status = {"fid-1": {"距A-Check小时": "19", "损坏率%": "6"}}
            with patch.object(collector, "FLEET_CSV", fleet):
                collector._write_light_fleet_snapshot(
                    {"fid-1": stale}, [], [], status, [])
            row = collector.load_existing_csv(fleet)["fid-1"]
        self.assertEqual(row["经济舱需求"], "999")
        self.assertEqual(row["距A-Check小时"], "19")
        self.assertEqual(row["损坏率%"], "6")

    def test_full_snapshot_preserves_newer_server_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            fields = collector.CSV_FIELDNAMES
            newer = {key: "" for key in fields}
            newer.update({"飞机ID": "fid-1", "注册号": "SAFE-1",
                          "经济舱需求": "999", "CO2减排放": "未查询",
                          "最后更新时间": "2026-08-09 05:01:00"})
            older = dict(newer)
            older.update({"经济舱需求": "100", "CO2减排放": "已改装",
                          "最后更新时间": "2026-08-09 05:00:00"})
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(newer)
            with patch.object(collector, "FLEET_CSV", fleet):
                self.assertTrue(collector._write_full_fleet_snapshot([older]))
            row = collector.load_existing_csv(fleet)["fid-1"]
        self.assertEqual(row["经济舱需求"], "999")
        self.assertEqual(row["CO2减排放"], "已改装")
        self.assertEqual(row["最后更新时间"], "2026-08-09 05:01:00")


class SecurityAndPersistenceTests(unittest.TestCase):
    def test_csrf_token_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "missing" / ".csrf_token"
            with patch.object(server, "_CSRF_TOKEN_FILE", token_path):
                first = server._load_csrf_token()
                second = server._load_csrf_token()
            self.assertTrue(token_path.is_file())
            self.assertEqual(token_path.read_text(encoding="utf-8"), first)
            self.assertEqual(second, first)

    def test_login_payload_url_encodes_credentials(self):
        email = "a+b@example.com"
        password = "a&b=c+d% e"
        payload = collector._login_payload(email, password)
        self.assertEqual(
            parse_qs(payload),
            {"lEmail": [email], "lPass": [password], "fbSig": ["null"]},
        )

    def test_account_keys_do_not_collide_after_sanitizing(self):
        first = account_storage.account_key("a+b@example.com")
        second = account_storage.account_key("a_b@example.com")
        self.assertNotEqual(first, second)
        self.assertEqual(first, account_storage.account_key(" A+B@EXAMPLE.COM "))

    def test_legacy_account_directory_is_migrated(self):
        email = "pilot@example.com"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / account_storage.legacy_account_key(email)
            legacy.mkdir()
            (legacy / "pending_tasks.json").write_text("[]", encoding="utf-8")
            target = account_storage.account_output_dir(root, email)
            self.assertEqual(target.name, account_storage.account_key(email))
            self.assertTrue((target / "pending_tasks.json").exists())
            self.assertFalse(legacy.exists())

    def test_partial_account_migration_resumes(self):
        email = "pilot@example.com"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / account_storage.legacy_account_key(email)
            target = root / account_storage.account_key(email)
            legacy.mkdir()
            target.mkdir()
            (target / "fleet.csv").write_text("fleet", encoding="utf-8")
            (legacy / "pending_tasks.json").write_text("[]", encoding="utf-8")
            result = account_storage.account_output_dir(root, email)
            self.assertEqual(result, target)
            self.assertTrue((target / "fleet.csv").exists())
            self.assertTrue((target / "pending_tasks.json").exists())
            self.assertFalse(legacy.exists())

    def test_delivery_query_failure_stays_unknown(self):
        with patch.object(route_planner, "_do_curl", side_effect=RuntimeError("timeout")):
            self.assertEqual(route_planner._delivery_status("123"), (None, 0))
        with patch.object(
                route_planner, "_do_curl",
                return_value='<form action="weblogin/login.php"><input name="lEmail"></form>'):
            self.assertEqual(route_planner._delivery_status("123"), (None, 0))

    def test_unknown_delivery_never_continues_to_route_write(self):
        aircraft = {"id": "344", "name": "MC-21-400", "capacity": 230, "eid": "312"}
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_fleet_aircraft_id", return_value="fid-1"), \
             patch.object(route_planner, "_delivery_status", return_value=(None, 0)), \
             patch.object(route_planner, "_do_curl") as do_curl:
            result = route_planner.build_route(
                aircraft, "hub-1", "airport-2", "SAFE-1",
                economy=230, business=0, first=0,
            )
        self.assertTrue(result["waiting_delivery"])
        self.assertTrue(result["delivery_unknown"])
        do_curl.assert_not_called()

    def test_atomic_json_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pending_tasks.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            with patch.object(storage_utils.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    storage_utils.atomic_write_json(path, [{"id": "t1"}])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_file_lock_serializes_writers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fleet.csv"
            state = {"active": 0, "maximum": 0}
            guard = threading.Lock()

            def writer():
                with storage_utils.exclusive_file_lock(target):
                    with guard:
                        state["active"] += 1
                        state["maximum"] = max(state["maximum"], state["active"])
                    time.sleep(0.03)
                    with guard:
                        state["active"] -= 1

            workers = [threading.Thread(target=writer) for _ in range(3)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(state["maximum"], 1)

    def test_file_lock_serializes_separate_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fleet.csv"
            events = Path(temp_dir) / "events.txt"
            code = "\n".join([
                "import sys, time",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "from storage_utils import exclusive_file_lock",
                "target, events = Path(sys.argv[2]), Path(sys.argv[3])",
                "with exclusive_file_lock(target):",
                "  with events.open('a', encoding='utf-8') as f: f.write('start\\n')",
                "  time.sleep(0.15)",
                "  with events.open('a', encoding='utf-8') as f: f.write('end\\n')",
            ])
            args = [sys.executable, "-c", code, str(ROOT / "src"), str(target), str(events)]
            workers = [subprocess.Popen(args) for _ in range(2)]
            for worker in workers:
                self.assertEqual(worker.wait(timeout=10), 0)
            sequence = events.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sequence, ["start", "end", "start", "end"])

    def test_hub_chips_do_not_embed_inline_javascript(self):
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('onclick="shc2(', template)
        self.assertIn("chip.addEventListener('click',()=>shc2(h))", template)
        self.assertIn("sel.replaceChildren()", template)
        self.assertIn("replace(/\"/g,'&quot;')", template)

    def test_takeoff_response_requires_ajax_fragment(self):
        self.assertEqual(collector.classify_takeoff_response("doDepart();"), "accepted")
        self.assertEqual(
            collector.classify_takeoff_response(
                '<form action="weblogin/login.php"><input name="lEmail"></form>'),
            "unknown",
        )
        self.assertEqual(
            collector.classify_takeoff_response("不能起飛：燃油不足"), "no_fuel")
        self.assertEqual(
            collector.classify_takeoff_response("沒有足夠燃油"), "no_fuel")
        self.assertEqual(
            collector.classify_takeoff_response("Not enough fuel"), "no_fuel")
        self.assertEqual(
            collector.classify_takeoff_response("沒有航線剩下出發"),
            "not_ready",
        )
        self.assertEqual(
            collector.classify_takeoff_response("toast('x','denied','error')"),
            "rejected",
        )

    def test_fuel_exhaustion_circuit_breaker(self):
        server._market_rt_cache[server._active_account_key] = {"fuel_qty": "500000"}
        with patch.object(server, "_broadcast_sse"), \
             patch.object(server, "_append_log"):
            server._mark_fuel_exhausted()
        self.assertEqual(
            server._market_rt_cache[server._active_account_key]["fuel_qty"], "0")
        self.assertEqual(server._current_fuel_lbs(), 0.0)

    def test_incremental_aircraft_updates_in_place_and_keeps_operation_state(self):
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function applyFleetView()", template)
        self.assertIn("setTimeout(applyFleetView,80)", template)
        self.assertIn("if(!ac['_operation_state'])ac['_operation_state']=old['_operation_state']", template)
        self.assertIn("function fleetRowsAll(){return fullFd.concat(fullFdc)}", template)
        self.assertNotIn("...(hub&&{hub})", template)

    def test_mobile_fleet_controls_are_compact_and_horizontally_scrollable(self):
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".hub-chips{flex-wrap:nowrap;overflow-x:auto", template)
        self.assertIn(".toolbar>#hf,.toolbar>button{display:none}", template)
        self.assertIn(".reg-cell>strong{white-space:nowrap}", template)
        self.assertIn('class="build-tag-row"', template)
        self.assertIn('class="hub-scroll"', template)

    def test_route_planner_uses_in_page_selectors_instead_of_native_dropdowns(self):
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="rp-select" id="rpAcCtl"', template)
        self.assertIn('class="rp-select" id="rpEngineCtl"', template)
        self.assertIn('class="rp-select" id="rpDepCtl"', template)
        self.assertNotIn('id="rpAc" list=', template)
        self.assertNotIn('<select class="filter-select" id="rpEngine"', template)
        self.assertNotIn('<select class="filter-select" id="rpDep"', template)
        self.assertIn("RP_CACHE_KEY='am4-route-base-v1'", template)
        self.assertIn("account:rpCacheAccount()", template)
        self.assertIn("rpRestoreBase();", template)

    def test_dashboard_restores_theme_before_paint_and_caches_by_account(self):
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertLess(template.index("document.documentElement.dataset.theme"),
                        template.index("<title>AM4 机队中心</title>"))
        self.assertIn(":root{color-scheme:light", template)
        self.assertIn("DASH_CACHE_KEY='am4-dashboard-cache-v1'", template)
        self.assertIn("String(SERVER_ACCOUNT||'').toLowerCase()", template)
        self.assertIn("let SERVER_BOOTSTRAP={{ dashboard_bootstrap|tojson }}", template)
        self.assertIn("restoreServerSnapshot(restoreDashboardCache())", template)
        self.assertIn("a.length+c.length===0&&fullFd.length+fullFdc.length>0", template)
        self.assertLess(template.index("window.__AM4_CACHE_STATUS='hit'"),
                        template.index("let SERVER_BOOTSTRAP={{ dashboard_bootstrap|tojson }}"))
        self.assertIn("window.__AM4_CACHE_WRITE_STATUS='ok'", template)
        self.assertIn("window.addEventListener('pagehide',saveDashboardCache)", template)
        self.assertIn("function logClass(line)", template)
        self.assertIn("line.includes('not_ready'))return'warn'", template)
        self.assertIn(".console .warn{color:var(--wr)}", template)
        self.assertIn("mobile=matchMedia('(max-width:768px)').matches", template)
        self.assertIn("document.getElementById('sb').textContent=mobile?compact(m.balance):m.balance", template)
        self.assertIn("function renderRuns(runs)", template)
        self.assertIn("function ownEvent(d)", template)
        self.assertIn("{{ '运行中' if initial_running else '空闲' }}", template)
        self.assertIn("function chips(c)", template)
        self.assertIn("if(row['_pending_build'])tr.style.background='rgba(255,193,7,.12)'", template)
        self.assertIn("function order(rows)", template)
        self.assertIn("renderHubOptions();applyFleetView();rm()", template)
        self.assertLess(template.index("window.st=function(t)"),
                        template.index("let SERVER_BOOTSTRAP={{ dashboard_bootstrap|tojson }}"))


class ServerSchedulingTests(unittest.TestCase):
    def setUp(self):
        server._pending_tasks[:] = []
        server._pending_seq = 0
        server._active_account_email = "tests@example.invalid"
        server._active_account_password = "test-password"
        server._active_account_key = account_storage.account_key("tests@example.invalid")
        server._loop_owner_email = "tests@example.invalid"
        server._loop_owner_pinned = False
        server._refresh_pending_mirror()
        server._runs.clear()
        server._home_status_cache.clear()
        server._home_status_ts.clear()
        # 隔离每个用例的建设记录，防止前序用例真实写入全局 builds.csv 后
        # 污染后续 takeoff 用例（_retrofit_blocks_takeoff 会读取该文件）。
        builds = server._paths_for_account("tests@example.invalid")["builds"]
        if builds.exists():
            builds.unlink()

    def test_server_import_finishes_before_scheduler_start(self):
        self.assertTrue(hasattr(server, "_publish_log"))
        self.assertTrue(hasattr(server, "_route_step_log"))
        self.assertEqual(os.environ["AM4_DISABLE_SCHEDULER"], "1")

    def test_dashboard_bootstrap_uses_compact_fleet_rows(self):
        compact = server._dashboard_fleet_snapshot([{
            "飞机ID": "1", "注册号": "TEST-1", "飞行时长": "01:00:00",
            "经济舱需求": "999", "_operation_state": "flying",
        }])[0]
        self.assertEqual(compact["飞机ID"], "1")
        self.assertEqual(compact["_operation_state"], "flying")
        self.assertNotIn("经济舱需求", compact)

    def test_invalid_run_json_does_not_leave_false_running_state(self):
        with server.app.test_client() as client:
            response = client.post(
                "/api/run", data="{bad json", content_type="application/json",
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(server._any_run_running())

    def test_light_debug_mode_is_accepted_without_ui_entry(self):
        with patch.object(server.threading, "Thread") as thread, \
             server.app.test_client() as client:
            response = client.post(
                "/api/run", json={"mode": "light"},
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(
            server._runs[server._active_account_key]["mode"], "light")
        thread.assert_called_once()
        server._runs.clear()
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("run('light')", template)

    def test_resume_loop_keeps_existing_log_and_runs_collector_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "run_log.txt"
            log.write_text("existing log\n", encoding="utf-8")
            paths = dict(server._paths())
            paths["log"] = log
            with patch.object(server, "_paths", return_value=paths), \
                 patch.object(server, "_rotate_run_log") as rotate, \
                 patch.object(server.threading, "Thread") as thread, \
                 server.app.test_client() as client:
                response = client.post(
                    "/api/run", json={"mode": "loop_resume"},
                    headers={"X-CSRF-Token": server._csrf_token},
                )
            persisted_log = log.read_text(encoding="utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            server._runs[server._active_account_key]["mode"], "loop_resume")
        self.assertEqual(persisted_log, "existing log\n")
        rotate.assert_not_called()
        thread.assert_called_once()
        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("run('loop_resume')", template)
        server._runs.clear()

    def test_regular_loop_rotates_existing_log(self):
        with patch.object(server, "_rotate_run_log") as rotate, \
             patch.object(server.threading, "Thread"), \
             server.app.test_client() as client:
            response = client.post(
                "/api/run", json={"mode": "loop"},
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 200)
        rotate.assert_called_once()
        server._runs.clear()

    def test_systemd_helper_uses_resume_loop_mode(self):
        helper = (ROOT / "deploy" / "start_loop.py").read_text(encoding="utf-8")
        self.assertIn('json.dumps({"mode": "loop_resume"})', helper)

    def test_resume_loop_uses_loop_watchdog_policy(self):
        source = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
        self.assertIn('if _mode in {"loop", "loop_resume"}:', source)

    def test_marketing_pending_is_not_marked_as_missing_route(self):
        server._pending_tasks[:] = [{
            "id": "marketing-1",
            "kind": "marketing",
            "title": "环保营销自动续期（12 小时）",
            "status": "pending",
            "trigger_at": time.time() + 60,
            "created_at": time.time(),
            "params": {"campaign": "eco"},
        }]
        with patch.object(server, "_sync_account_context"), server.app.test_client() as client:
            tasks = client.get("/api/pending").get_json()
        task = next(item for item in tasks if item["id"] == "marketing-1")
        self.assertFalse(task["route_required"])
        self.assertFalse(task["route_ready"])

        template = (ROOT / "src" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("t.route_required && !t.route_ready", template)

    def test_takeoff_pending_uses_compact_mobile_fields(self):
        server._pending_tasks[:] = [{
            "id": "takeoff-1", "kind": "takeoff",
            "title": "飞机 A330-2F-1 返场结束后接管起飞（航线 27989308）",
            "status": "pending", "trigger_at": time.time() + 900,
            "created_at": time.time(),
            "error": "游戏暂不允许起飞（not_ready）\n第 2 次，15 分钟后重试",
            "params": {"reg": "A330-2F-1", "route_id": "27989308",
                       "reason": "返场结束"},
        }]
        with patch.object(server, "_sync_account_context"), server.app.test_client() as client:
            task = client.get("/api/pending").get_json()[0]
        self.assertEqual(task["title"], "A330-2F-1 返场后起飞")
        self.assertEqual(task["route_id"], "27989308")
        self.assertIn("\n", task["error"])

    def test_delivery_continue_reuses_confirmation_and_keeps_waiting_result(self):
        planner = Mock()
        planner._delivery_status.return_value = (True, 0)
        planner.build_route.return_value = {
            "ok": True, "steps": [], "waiting_delivery": True, "remain_sec": 300,
        }
        task = {
            "kind": "delivery_continue", "status": "running", "trigger_at": 0,
            "params": {
                "fid": "fid-1", "reg": "SAFE-1", "aircraft": {"id": "344"},
                "hub_id": "hub-1", "arr_id": "airport-2",
            },
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertIn("顺延", task["error"])
        self.assertEqual(planner.build_route.call_args.kwargs["confirmed_fid"], "fid-1")

    def test_unknown_takeoff_response_is_retried(self):
        planner = Mock()
        planner.takeoff_route.return_value = "<html>login</html>"
        planner.classify_takeoff_response.return_value = "unknown"
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"route_id": "route-1", "reg": "SAFE-1", "cost_index": 200},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_refresh_fleet_row", return_value={
                 "需求状态": "旺盛", "飞行时长": "01:00:00"}), \
             patch.object(server, "_takeoff_maintenance_block", return_value=None), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertIn("5 分钟", task["error"])

    def test_takeoff_request_exception_uses_bounded_retry(self):
        planner = Mock()
        planner.takeoff_route.side_effect = TimeoutError("timeout")
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_refresh_fleet_row", return_value={
                 "需求状态": "旺盛", "飞行时长": "01:00:00"}), \
             patch.object(server, "_takeoff_maintenance_block", return_value=None), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["retry"]["category"], "takeoff")
        self.assertEqual(task["retry"]["attempts"], 1)
        self.assertIn("5 分钟", task["error"])

    def test_takeoff_refresh_failure_is_retried_without_departure(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_refresh_fleet_row", return_value=None), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertIn("无法刷新", task["error"])
        planner.takeoff_route.assert_not_called()

    def test_takeoff_uses_home_maintenance_end_without_aircraft_request(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "retry": {"category": "takeoff", "attempts": 1},
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "ready_at": 900, "reason": "本次飞行落地"},
        }
        latest = {
            "维护改装结束时间戳": "5000",
            "预计落地时间戳": "4000",
            "距A-Check小时": "4",
            "损坏率%": "70",
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value=latest), \
             patch.object(server, "_refresh_fleet_row") as refresh, \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log") as publish, \
             patch.object(server.time, "time", return_value=1000):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["trigger_at"], 5120)
        self.assertEqual(task["params"]["ready_at"], 5000)
        self.assertEqual(task["params"]["reason"], "返场结束")
        self.assertNotIn("retry", task)
        self.assertIsNone(task["error"])
        self.assertIn("返场结束后接管起飞", task["title"])
        self.assertIn("顺延", publish.call_args.args[0])
        refresh.assert_not_called()
        planner.takeoff_route.assert_not_called()

    def test_manually_grounded_aircraft_ends_takeoff_task_without_retry(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "retry": {"category": "takeoff", "attempts": 2},
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={"停飞": "1"}), \
             patch.object(server, "_refresh_fleet_row") as refresh, \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log") as publish:
            server._run_pending_task(task)
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["error"])
        self.assertNotIn("retry", task)
        self.assertIn("人工停飞", publish.call_args.args[0])
        refresh.assert_not_called()
        planner.takeoff_route.assert_not_called()

    def test_operation_state_uses_home_status(self):
        with patch.object(server.time, "time", return_value=1_000):
            flying = server._decorate_operation_state(
                {"注册号": "AIR-1"}, {"预计落地时间戳": "1200"})
            maintenance = server._decorate_operation_state(
                {"注册号": "MAINT-1"}, {"维护改装结束时间戳": "1300"})
            building = server._decorate_operation_state(
                {"注册号": "BUILD-1", "建设状态": "建设中"}, {})
        self.assertEqual(flying["_operation_state"], "flying")
        self.assertEqual(flying["_operation_until"], 1200)
        self.assertEqual(maintenance["_operation_state"], "maintenance")
        self.assertEqual(maintenance["_operation_until"], 1300)
        self.assertEqual(building["_operation_state"], "building")

    def test_operation_status_payload_is_minimal_and_realtime_ready(self):
        with patch.object(server.time, "time", return_value=1_000):
            payload = server._operation_status_payload({
                "fid-1": {"注册号": "AIR-1", "预计落地时间戳": "1200"},
                "fid-2": {"注册号": "MAINT-1", "维护改装结束时间戳": "1300"},
            })
        self.assertEqual(payload, [
            {"fid": "fid-1", "reg": "AIR-1", "state": "flying", "until": 1200.0},
            {"fid": "fid-2", "reg": "MAINT-1", "state": "maintenance", "until": 1300.0},
        ])

    def test_takeoff_stops_when_latest_demand_is_insufficient(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_refresh_fleet_row", return_value={"需求状态": "不足"}), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log") as publish:
            server._run_pending_task(task)
        self.assertEqual(task["status"], "done")
        planner.takeoff_route.assert_not_called()
        self.assertIn("需求重置", publish.call_args.args[0])

    def test_takeoff_reuses_preflight_detail_for_next_task(self):
        planner = Mock()
        planner.takeoff_route.return_value = "doDepart();"
        planner.classify_takeoff_response.return_value = "accepted"
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        refreshed = {"需求状态": "旺盛", "飞行时长": "01:30:00"}
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance",
                          side_effect=[{}, {"预计落地时间戳": "6400"}]), \
             patch.object(server, "_refresh_fleet_row", return_value=refreshed) as refresh, \
             patch.object(server, "_takeoff_maintenance_block", return_value=None), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_add_takeoff_task") as add_next, \
             patch.object(server, "_mark_build"), \
             patch.object(server, "_append_log"), \
             patch.object(server, "_broadcast_sse"), \
             patch.object(server.time, "time", return_value=1_000):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "done")
        self.assertEqual(refresh.call_count, 2)
        planner.takeoff_route.assert_called_once_with("route-1", 200)
        add_next.assert_called_once()
        self.assertEqual(add_next.call_args.kwargs["fid"], "fid-1")
        self.assertEqual(add_next.call_args.args[3], 1_000 + 5_400 + 120)
        self.assertEqual(add_next.call_args.kwargs["ready_at"], 1_000 + 5_400)
        self.assertEqual(add_next.call_args.kwargs["reason"], "本次飞行落地")

    def test_first_takeoff_with_zero_duration_schedules_read_only_reconciliation(self):
        planner = Mock()
        planner.takeoff_route.return_value = "doDepart();"
        planner.classify_takeoff_response.return_value = "accepted"
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 900,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "NEW-1",
                       "hub_id": "hub-1", "reason": "改装完成"},
        }
        refreshed = {"需求状态": "旺盛", "飞行时长": "00:00:00"}
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_refresh_fleet_row", return_value=refreshed), \
             patch.object(server, "_takeoff_maintenance_block", return_value=None), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_add_pending_task") as reconcile, \
             patch.object(server, "_mark_build"), \
             patch.object(server, "_append_log"), \
             patch.object(server, "_broadcast_sse"), \
             patch.object(server.time, "time", return_value=1_000):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["completed_at"], 1_000)
        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args.args[0], "takeoff_reconcile")
        self.assertEqual(reconcile.call_args.args[2], 1_300)
        self.assertEqual(reconcile.call_args.args[3]["started_at"], 1_000)

    def test_first_flight_reconciliation_uses_home_arrival_without_departing_again(self):
        planner = Mock()
        task = {
            "kind": "takeoff_reconcile", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "NEW-1",
                       "hub_id": "hub-1", "cost_index": 200, "started_at": 1_000},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance",
                          return_value={"预计落地时间戳": "6400"}), \
             patch.object(server, "_set_fleet_duration_if_missing") as set_duration, \
             patch.object(server, "_add_takeoff_task") as add_next, \
             patch.object(server, "_publish_log"), \
             patch.object(server.time, "time", return_value=1_300):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "done")
        set_duration.assert_called_once_with("NEW-1", 5_400)
        add_next.assert_called_once()
        self.assertEqual(add_next.call_args.args[3], 6_520)
        planner.takeoff_route.assert_not_called()

    def test_legacy_doubled_takeoff_is_repaired_once(self):
        task = {
            "kind": "takeoff", "status": "pending", "created_at": 1_000,
            "trigger_at": 1_000 + 5_400 * 2 + 120,
            "params": {"reg": "SAFE-1", "reason": "往返完成",
                       "ready_at": 1_000 + 5_400 * 2},
        }
        fleet = [{"注册号": "SAFE-1", "飞行时长": "01:30:00"}]
        self.assertEqual(server._repair_legacy_doubled_takeoffs([task], fleet), 1)
        self.assertEqual(task["trigger_at"], 1_000 + 5_400 + 120)
        self.assertEqual(task["params"]["ready_at"], 1_000 + 5_400)
        self.assertEqual(task["params"]["reason"], "本次飞行落地")
        self.assertEqual(server._repair_legacy_doubled_takeoffs([task], fleet), 0)

    def test_latest_aircraft_detail_can_block_takeoff_for_maintenance(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        fresh = {"需求状态": "旺盛", "飞行时长": "01:00:00",
                 "距A-Check小时": "4", "损坏率%": "10"}
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value=fresh), \
             patch.object(server, "_refresh_fleet_row", return_value=fresh), \
             patch.object(server, "_takeoff_maintenance_block",
                          return_value="距A-Check仅 4 小时"), \
             patch.object(server, "_current_fuel_lbs", return_value=1_000_000), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "failed")
        planner.takeoff_route.assert_not_called()

    def test_retrofit_network_failure_retries_without_takeoff(self):
        planner = Mock()
        planner._apply_retrofit.return_value = {
            "ok": False, "retryable": True, "msg": "改装页抓取失败",
        }
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "all", "economy": 100, "business": 0, "first": 0},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        schedule.assert_not_called()

    def test_submitted_retrofit_waits_for_install_before_confirmation(self):
        planner = Mock()
        planner._apply_retrofit.return_value = {
            "ok": False,
            "retryable": True,
            "submitted": True,
            "install_secs": 3600,
            "msg": "改装已提交但确认页暂不可用",
        }
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "retry": {"category": "retrofit", "attempts": 2},
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "all", "economy": 100, "business": 0, "first": 0},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_publish_log"), \
             patch.object(server.time, "time", return_value=1000):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["trigger_at"], 4720)
        self.assertEqual(task["params"]["retrofit_ready_at"], 4720)
        self.assertTrue(task["params"]["retrofit_submitted"])
        self.assertNotIn("retry", task)
        self.assertIn("62 分钟后确认", task["error"])
        schedule.assert_not_called()

    def test_submitted_retrofit_uses_read_only_confirmation_path(self):
        planner = Mock()
        planner._confirm_retrofit.return_value = {
            "ok": False, "retryable": True,
            "msg": "确认页暂不可用或飞机仍在安装中",
        }
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "CARGO-1",
                       "retrofit": "all", "cargo_l": 99, "cargo_h": 1,
                       "retrofit_submitted": True, "retrofit_ready_at": 900},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance", return_value={}), \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_publish_log") as publish, \
             patch.object(server.time, "time", return_value=1_000):
            server._run_pending_task(task)
        planner._confirm_retrofit.assert_called_once_with(
            "fid-1", {"co2", "speed", "fuel"})
        planner._apply_retrofit.assert_not_called()
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["trigger_at"], 1_300)
        self.assertEqual(task["retry"]["category"], "retrofit_confirm")
        self.assertNotIn("失败", task["error"])
        self.assertIn("再次确认", publish.call_args.args[0])
        schedule.assert_not_called()

    def test_submitted_retrofit_uses_home_pending_time_and_schedules_takeoff(self):
        planner = Mock()
        planner._apply_retrofit.return_value = {
            "ok": False,
            "retryable": True,
            "submitted": True,
            "install_secs": 120,
            "msg": "改装已提交但改装页暂不可用",
        }
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "all", "economy": 100, "business": 0, "first": 0},
        }
        home_status = {
            "注册号": "SAFE-1",
            "维护改装结束时间戳": 1_600,
            "预计落地时间戳": 0,
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_latest_home_maintenance",
                          return_value=home_status) as home, \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_mark_build") as mark_build, \
             patch.object(server, "_publish_log"), \
             patch.object(server.time, "time", return_value=1_000):
            server._run_pending_task(task)
        home.assert_called_once_with(planner, "SAFE-1", force_refresh=True)
        planner._confirm_retrofit.assert_not_called()
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["error"])
        mark_build.assert_called_once_with("SAFE-1", status="routed")
        schedule.assert_called_once_with(
            "route-1", "SAFE-1", 200,
            fid="fid-1", hub_id="", delay=720.0)

    def test_retrofit_rejection_fails_without_takeoff(self):
        planner = Mock()
        planner._apply_retrofit.return_value = {
            "ok": False, "retryable": False, "msg": "余额不足",
        }
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "all", "economy": 100, "business": 0, "first": 0},
        }
        with patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_mark_build") as mark_build, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "failed")
        schedule.assert_not_called()
        mark_build.assert_called_once_with("SAFE-1", status="retrofit_failed")

    def test_invalid_retrofit_config_fails_before_login(self):
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "speed,unknown", "economy": 100,
                       "business": 0, "first": 0},
        }
        with patch.object(server, "_get_route_planner") as get_planner, \
             patch.object(server, "_mark_build") as mark_build, \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "failed")
        self.assertIn("配置无效", task["error"])
        get_planner.assert_not_called()
        mark_build.assert_called_once_with("SAFE-1", status="retrofit_failed")
        schedule.assert_not_called()

    def test_full_scan_only_enqueues_takeoff(self):
        row = {"飞机ID": "fid-1", "注册号": "SAFE-1", "需求状态": "旺盛"}
        status = {"fid-1": {"航线ID": "route-1", "预计落地时间戳": 0}}
        with patch("builtins.print") as output:
            fresh_demand.enqueue_strong_demand([row], status_map=status)
        lines = [call.args[0] for call in output.call_args_list]
        marker = next(line for line in lines if line.startswith("__TAKEOVER_TAKEOFF__"))
        payload = json.loads(marker.removeprefix("__TAKEOVER_TAKEOFF__"))
        self.assertEqual(payload["fid"], "fid-1")
        self.assertEqual(payload["route_id"], "route-1")
        self.assertEqual(payload["reason"], "全量扫描发现")

    def test_full_scan_skips_manually_grounded_aircraft(self):
        row = {"飞机ID": "fid-1", "注册号": "SAFE-1", "需求状态": "旺盛"}
        status = {"fid-1": {"航线ID": "route-1", "停飞": "1"}}
        with patch("builtins.print") as output:
            fresh_demand.enqueue_strong_demand([row], status_map=status)
        lines = [str(call.args[0]) for call in output.call_args_list]
        self.assertTrue(any("人工停飞" in line for line in lines))
        self.assertFalse(any(line.startswith("__TAKEOVER_TAKEOFF__") for line in lines))

    def test_unknown_and_zero_fuel_are_both_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            market = Path(temp_dir) / "market.json"
            with patch.object(server, "_paths", return_value={"market": market}):
                self.assertIsNone(server._current_fuel_lbs())
                market.write_text('{"fuel_qty": "0"}', encoding="utf-8")
                self.assertEqual(server._current_fuel_lbs(), 0)

    def test_unknown_fuel_does_not_login_or_refresh_aircraft(self):
        planner = Mock()
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner", return_value=planner) as get_planner, \
             patch.object(server, "_current_fuel_lbs", return_value=None), \
             patch.object(server, "_refresh_fleet_row") as refresh, \
             patch.object(server, "_takeoff_maintenance_block", return_value=None), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        get_planner.assert_not_called()
        refresh.assert_not_called()

    def test_online_failures_stop_after_bounded_backoff(self):
        task = {"status": "running", "title": "SAFE-1 起飞"}
        with patch.object(server, "_publish_log"):
            for _ in range(4):
                server._defer_online_failure(task, "detail", "详情失败")
        self.assertEqual(task["status"], "pending")
        self.assertIn("连续失败 4 次", task["error"])
        self.assertIn("下个整点", task["error"])
        self.assertGreater(task["trigger_at"], time.time())

    def test_construction_failures_keep_low_frequency_recovery(self):
        task = {"kind": "delivery_continue", "status": "running", "title": "SAFE-1 续建"}
        with patch.object(server, "_publish_log"):
            for _ in range(4):
                server._defer_online_failure(task, "route_lookup", "航线查询失败")
        self.assertEqual(task["status"], "pending")
        self.assertIn("每 6 小时", task["error"])

    def test_required_retrofit_without_fid_never_schedules_takeoff(self):
        task = {
            "kind": "retrofit", "status": "running", "trigger_at": 0,
            "params": {"fid": "", "route_id": "route-1", "reg": "SAFE-1",
                       "retrofit": "all"},
        }
        with patch.object(server, "_load_builds", return_value=[]), \
             patch.object(server, "_mark_build") as mark_build, \
             patch.object(server, "_schedule_takeoff") as schedule, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "failed")
        self.assertIn("缺少机队 ID", task["error"])
        mark_build.assert_called_once_with("SAFE-1", status="retrofit_failed")
        schedule.assert_not_called()

    def test_failed_retrofit_blocks_daily_takeoff_discovery(self):
        with patch.object(server, "_load_builds", return_value=[{
                "reg": "SAFE-1", "status": "retrofit_failed"}]):
            self.assertIn("改装失败", server._retrofit_blocks_takeoff("SAFE-1"))

    def test_existing_takeoff_is_cancelled_when_retrofit_blocks(self):
        task = {
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_retrofit_blocks_takeoff",
                          return_value="要求的改装尚未成功"), \
             patch.object(server, "_current_fuel_lbs") as fuel, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "cancelled")
        fuel.assert_not_called()

    def test_removed_aircraft_cancels_owned_tasks_and_closes_build(self):
        server._pending_tasks[:] = [
            {"kind": "takeoff", "status": "pending", "params": {
                "reg": "SOLD-1", "fid": "101", "route_id": "r1"}},
            {"kind": "retrofit", "status": "failed", "params": {
                "reg": "SOLD-1", "fid": "101", "route_id": "r1"}},
            {"kind": "takeoff_reconcile", "status": "running", "params": {
                "reg": "OLD-NAME", "fid": "101", "route_id": "r1"}},
            {"kind": "takeoff", "status": "pending", "params": {
                "reg": "KEEP-1", "fid": "202", "route_id": "r2"}},
            {"kind": "takeoff", "status": "done", "params": {
                "reg": "SOLD-1", "fid": "101", "route_id": "r1"}},
        ]
        with patch.object(server, "_save_pending_tasks") as save, \
             patch.object(server, "_mark_build") as mark_build, \
             patch.object(server, "_publish_log") as publish:
            count = server._cancel_removed_aircraft_tasks([
                {"飞机ID": "101", "注册号": "SOLD-1"},
            ])
        self.assertEqual(count, 3)
        self.assertEqual(
            [task["status"] for task in server._pending_tasks],
            ["cancelled", "cancelled", "cancelled", "pending", "done"],
        )
        self.assertTrue(all(
            task["params"].get("aircraft_removed")
            for task in server._pending_tasks[:3]
        ))
        self.assertTrue(all(
            "已售出或移除" in task["error"]
            for task in server._pending_tasks[:3]
        ))
        save.assert_called_once()
        mark_build.assert_called_once_with("SOLD-1", status="sold")
        self.assertIn("已取消 3 项", publish.call_args.args[0])

    def test_removed_aircraft_guard_prevents_retry_from_rearming_task(self):
        task = {
            "kind": "takeoff", "status": "running",
            "params": {"reg": "SOLD-1", "aircraft_removed": True},
            "retry": {"category": "takeoff", "attempts": 2},
        }
        with patch.object(server, "_current_fuel_lbs") as fuel:
            server._run_pending_task(task)
        self.assertEqual(task["status"], "cancelled")
        self.assertNotIn("retry", task)
        fuel.assert_not_called()

    def test_mark_build_matches_registration_case_insensitively(self):
        builds = [{"reg": "sadsfdg", "status": "routed", "updated_at": 1}]
        with patch.object(server, "_load_builds", return_value=builds), \
             patch.object(server, "_save_builds") as save, \
             patch.object(server.time, "time", return_value=2):
            server._mark_build("SADSFDG", status="sold")
        self.assertEqual(builds[0]["status"], "sold")
        self.assertEqual(builds[0]["updated_at"], 2)
        save.assert_called_once_with(builds)

    def test_manual_retrofit_completion_clears_failed_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            fields = ["注册号", "CO2减排放", "飞行速度增加", "耗油量减少"]
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"注册号": "SAFE-1", "CO2减排放": "已改装",
                                 "飞行速度增加": "已改装", "耗油量减少": "已改装"})
            server._pending_tasks[:] = [{
                "kind": "retrofit", "status": "failed", "error": "旧错误",
                "params": {"reg": "SAFE-1", "retrofit": "all"},
            }]
            with patch.object(server, "_paths", return_value={"fleet": fleet}), \
                 patch.object(server, "_load_builds", return_value=[{
                     "reg": "SAFE-1", "status": "retrofit_failed", "retrofit": "all"}]), \
                 patch.object(server, "_mark_build") as mark_build, \
                 patch.object(server, "_save_pending_tasks"), \
                 patch.object(server, "_publish_log"):
                block = server._retrofit_blocks_takeoff("SAFE-1")
        self.assertIsNone(block)
        self.assertEqual(server._pending_tasks[0]["status"], "done")
        mark_build.assert_called_once_with("SAFE-1", status="routed")

    def test_failed_retrofit_survives_restart_for_reconciliation(self):
        owner = account_storage.account_key("tests@example.invalid")
        task = {
            "id": "t7", "kind": "retrofit", "status": "failed",
            "account": owner, "created_at": 1, "trigger_at": 2,
            "params": {"reg": "SAFE-1", "route_id": "route-1",
                       "retrofit": "all"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            pending = Path(temp_dir) / "pending_tasks.json"
            pending.write_text(json.dumps([task]), encoding="utf-8")
            with patch.object(server, "_save_pending_tasks"):
                server._load_pending_tasks(pending, owner)
        self.assertEqual(len(server._pending_tasks), 1)
        self.assertEqual(server._pending_tasks[0]["status"], "failed")
        self.assertEqual(server._pending_seq, 7)

    def test_network_failed_takeoffs_are_staggered_on_restart(self):
        owner = account_storage.account_key("tests@example.invalid")
        tasks = [{
            "id": f"t{n}", "kind": "takeoff", "status": "failed",
            "account": owner, "created_at": 1, "trigger_at": 2,
            "error": "curl returned non-zero exit status 6.",
            "params": {"reg": f"NET-{n}", "route_id": f"route-{n}"},
        } for n in (20, 21)]
        with tempfile.TemporaryDirectory() as temp_dir:
            pending = Path(temp_dir) / "pending_tasks.json"
            pending.write_text(json.dumps(tasks), encoding="utf-8")
            with patch.object(server, "_save_pending_tasks"), \
                 patch.object(server, "_append_log") as log, \
                 patch.object(server.time, "time", return_value=1_000):
                server._load_pending_tasks(pending, owner)
        self.assertEqual([t["status"] for t in server._pending_tasks],
                         ["pending", "pending"])
        self.assertEqual([t["trigger_at"] for t in server._pending_tasks],
                         [1_120, 1_150])
        self.assertTrue(all(t["retry"]["category"] == "network_recovery"
                            for t in server._pending_tasks))
        self.assertIn("恢复 2 条", log.call_args.args[0])

    def test_network_error_detection_is_limited_to_connectivity_failures(self):
        self.assertTrue(server._is_recoverable_network_error("exit status 6"))
        self.assertTrue(server._is_recoverable_network_error("exit status 7"))
        self.assertTrue(server._is_recoverable_network_error("exit status 28"))
        self.assertFalse(server._is_recoverable_network_error("HTTP 500 rejected"))

    def test_restart_recovers_missing_first_flight_followup(self):
        owner = account_storage.account_key("tests@example.invalid")
        completed = {
            "id": "t12", "kind": "takeoff", "status": "done",
            "account": owner, "created_at": 900, "trigger_at": 1_000,
            "params": {"reg": "NEW-1", "route_id": "route-1", "fid": "fid-1",
                       "hub_id": "hub-1", "cost_index": 200,
                       "reason": "改装完成"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending = root / "pending_tasks.json"
            fleet = root / "fleet.csv"
            pending.write_text(json.dumps([completed]), encoding="utf-8")
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["注册号", "飞行时长"])
                writer.writeheader()
                writer.writerow({"注册号": "NEW-1", "飞行时长": "00:00:00"})
            with patch.object(server, "_paths", return_value={"fleet": fleet}), \
                 patch.object(server, "_save_pending_tasks"):
                server._load_pending_tasks(pending, owner)
        self.assertEqual(len(server._pending_tasks), 1)
        recovered = server._pending_tasks[0]
        self.assertEqual(recovered["kind"], "takeoff_reconcile")
        self.assertEqual(recovered["params"]["reg"], "NEW-1")
        self.assertEqual(recovered["params"]["started_at"], 1_000)
        self.assertGreater(server._pending_seq, 12)

    def test_single_aircraft_refresh_preserves_home_and_mod_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            fields = ["飞机ID", "注册号", "枢纽分类", "距A-Check小时", "损坏率%",
                      "CO2减排放", "飞行速度增加", "耗油量减少", "需求状态"]
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "飞机ID": "fid-1", "注册号": "SAFE-1", "枢纽分类": "Hub",
                    "距A-Check小时": "4", "损坏率%": "10", "CO2减排放": "已改装",
                    "飞行速度增加": "未改装", "耗油量减少": "已改装", "需求状态": "旺盛",
                })
            fresh = {key: "" for key in fields}
            fresh.update({"飞机ID": "fid-1", "注册号": "SAFE-1",
                          "CO2减排放": "未查询", "飞行速度增加": "未查询",
                          "耗油量减少": "未查询", "需求状态": "旺盛"})
            paths = {"fleet": fleet, "hubs": Path(temp_dir) / "hubs.json",
                     "builds": Path(temp_dir) / "builds.csv"}
            with patch.object(server, "_paths", return_value=paths), \
                 patch.object(route_planner, "fetch_aircraft_fleet_row", return_value=fresh):
                result = server._refresh_fleet_row("SAFE-1", "fid-1", "")
        self.assertEqual(result["距A-Check小时"], "4")
        self.assertEqual(result["CO2减排放"], "已改装")
        self.assertEqual(result["飞行速度增加"], "未改装")

    def test_build_merge_never_replaces_real_fleet_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = Path(temp_dir) / "fleet.csv"
            fields = ["飞机ID", "注册号", "航班号", "飞行时长", "经济舱需求", "机型"]
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"飞机ID": "fid-1", "注册号": "SAFE-1",
                                 "航班号": "MA100", "飞行时长": "01:30:00",
                                 "经济舱需求": "900", "机型": ""})
            paths = {"fleet": fleet, "hubs": Path(temp_dir) / "hubs.json"}
            with patch.object(server, "_paths", return_value=paths):
                server._merge_build_into_fleet({
                    "fid": "fid-1", "reg": "SAFE-1", "aircraft": "Test Jet",
                    "status": "done",
                })
                rows = server._read_csv(fleet)
        self.assertEqual(rows[0]["航班号"], "MA100")
        self.assertEqual(rows[0]["飞行时长"], "01:30:00")
        self.assertEqual(rows[0]["经济舱需求"], "900")
        self.assertEqual(rows[0]["机型"], "Test Jet")

    def test_build_csv_persists_layout_and_retrofit_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            builds = Path(temp_dir) / "builds.csv"
            paths = {"builds": builds}
            with patch.object(server, "_paths", return_value=paths):
                server._record_build(
                    "SAFE-1", "Test Jet", "hub-1", "ap-1", "ap-2", "delivering",
                    economy="80", business="10", first="0", retrofit="co2,fuel",
                )
                loaded = server._load_builds()[0]
        self.assertEqual(loaded["economy"], "80")
        self.assertEqual(loaded["business"], "10")
        self.assertEqual(loaded["first"], "0")
        self.assertEqual(loaded["retrofit"], "co2,fuel")

    def test_building_row_survives_preflight_and_clears_after_final_refresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fleet = root / "fleet.csv"
            hubs = root / "hubs.json"
            hubs.write_text(json.dumps([{"hub_id": "hub-1", "name": "Dubai"}]), encoding="utf-8")
            paths = {"fleet": fleet, "hubs": hubs, "builds": root / "builds.csv"}
            build = {"reg": "NEW-1", "fid": "fid-1", "aircraft": "Test Jet",
                     "hub_id": "hub-1", "origin_airport_id": "", "dest_airport_id": ""}
            with patch.object(server, "_paths", return_value=paths):
                server._merge_build_into_fleet(build, building=True)
                self.assertEqual(server._read_csv(fleet)[0]["建设状态"], "建设中")
                fresh = dict(server._read_csv(fleet)[0])
                fresh.update({"飞机ID": "fid-1", "注册号": "NEW-1", "飞行时长": "01:00:00"})
                with patch.object(route_planner, "fetch_aircraft_fleet_row", return_value=fresh):
                    server._refresh_fleet_row("NEW-1", "fid-1", "hub-1")
                self.assertEqual(server._read_csv(fleet)[0]["建设状态"], "建设中")
                with patch.object(route_planner, "fetch_aircraft_fleet_row", return_value=fresh):
                    server._refresh_fleet_row(
                        "NEW-1", "fid-1", "hub-1", finalize_build=True)
                self.assertEqual(server._read_csv(fleet)[0]["建设状态"], "")

    def test_build_merge_collapses_legacy_placeholder_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fleet = root / "fleet.csv"
            fields = ["飞机ID", "注册号", "机型", "建设状态"]
            with fleet.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"飞机ID": "B-NEW-1", "注册号": "NEW-1", "建设状态": "建设中"})
                writer.writerow({"飞机ID": "fid-1", "注册号": "NEW-1", "机型": "Test Jet"})
            paths = {"fleet": fleet, "hubs": root / "hubs.json"}
            with patch.object(server, "_paths", return_value=paths):
                server._merge_build_into_fleet({
                    "fid": "fid-1", "reg": "NEW-1", "aircraft": "Test Jet",
                })
                rows = server._read_csv(fleet)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["飞机ID"], "fid-1")

    def test_marketing_tasks_are_persistent_and_deduplicated(self):
        server._pending_tasks[:] = []
        with patch.object(server, "_save_pending_tasks"):
            server._ensure_marketing_tasks()
            server._ensure_marketing_tasks()
        campaigns = [(task.get("params") or {}).get("campaign")
                     for task in server._pending_tasks if task.get("kind") == "marketing"]
        self.assertCountEqual(campaigns, ["airline", "eco"])

    def test_chinese_all_retrofit_value_means_all_three_mods(self):
        self.assertEqual(server._retrofit_mods("全部"), {"co2", "speed", "fuel"})

    def test_marketing_never_purchases_when_status_page_is_unavailable(self):
        task = {
            "kind": "marketing", "status": "running", "trigger_at": 0,
            "account": server._active_account_key,
            "params": {"campaign": "eco"},
        }
        with patch.object(server, "_get_route_planner"), \
             patch.object(collector, "fetch", return_value=""), \
             patch.object(collector, "_purchase_marketing") as purchase, \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        purchase.assert_not_called()

    def test_expired_marketing_is_repurchased_instead_of_stuck_in_readonly_confirm(self):
        # 游戏不允许同种营销重复购买：活动结束（remaining==0）后重试购买是安全的。
        # 旧的 confirmation_only 标记不得把任务锁死在“只读确认”，否则网络偶发失败
        # 会导致营销永久中断。
        task = {
            "kind": "marketing", "status": "running", "trigger_at": 0,
            "account": server._active_account_key,
            "params": {"campaign": "airline", "confirmation_only": True},
        }
        page = "<a href='marketing_new.php'>marketing</a>"
        with patch.object(server, "_get_route_planner"), \
             patch.object(collector, "fetch", return_value=page), \
             patch.object(collector, "_purchase_marketing",
                          return_value=(True, "购买成功", 86400)) as purchase, \
             patch.object(server, "_refresh_market_after_spend"), \
             patch.object(server, "_publish_log"):
            server._run_pending_task(task)
        purchase.assert_called_once()
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["error"], None)

    def test_active_marketing_reschedules_at_expiry_plus_sixty_seconds(self):
        task = {
            "kind": "marketing", "status": "running", "trigger_at": 0,
            "account": server._active_account_key,
            "params": {"campaign": "eco"},
        }
        page = (
            "<a href='marketing_new.php'>marketing</a>"
            "<tr><td><span class='glyphicons glyphicons-leaf'></span></td>"
            "<td id='eTimer'></td></tr><script>timer('eTimer',1000);</script>"
        )
        with patch.object(server, "_get_route_planner"), \
             patch.object(collector, "fetch", return_value=page), \
             patch.object(collector, "_purchase_marketing") as purchase, \
             patch.object(server.time, "time", return_value=100):
            server._run_pending_task(task)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["trigger_at"], 1160)
        purchase.assert_not_called()

    def test_unknown_marketing_task_is_failed_without_network_access(self):
        task = {
            "kind": "marketing", "status": "running", "trigger_at": 0,
            "account": server._active_account_key,
            "params": {"campaign": "unexpected"},
        }
        with patch.object(server, "_get_route_planner") as planner:
            server._run_pending_task(task)
        self.assertEqual(task["status"], "failed")
        planner.assert_not_called()

    def test_spend_refresh_reads_homepage_only(self):
        page = "<span id='headerAccount'>57,000,000</span>"
        with patch.object(server, "_get_route_planner"), \
             patch.object(collector, "_do_curl", return_value=page) as curl, \
             patch.object(collector, "parse_status_data", return_value={}), \
             patch.object(server, "_broadcast_sse"):
            server._refresh_market_after_spend()
        self.assertEqual(curl.call_count, 1)
        self.assertEqual(server._market_rt_cache[server._active_account_key]["balance"], "57,000,000")

    def test_task_owned_by_another_account_is_never_executed(self):
        task = {
            "account": account_storage.account_key("other@example.invalid"),
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        with patch.object(server, "_get_route_planner") as get_planner:
            server._run_pending_task(task)
        self.assertEqual(task["status"], "cancelled")
        get_planner.assert_not_called()

    def test_account_switch_swaps_pending_queue_and_clears_caches(self):
        old_email = "old@example.invalid"
        new_email = "new@example.invalid"
        server._active_account_email = old_email
        server._active_account_password = "old-password"
        server._active_account_key = account_storage.account_key(old_email)
        server._refresh_pending_mirror()
        server._pending_tasks[:] = [{
            "id": "t1", "account": server._active_account_key,
            "kind": "takeoff", "status": "pending", "trigger_at": 999,
            "params": {"route_id": "old-route", "reg": "OLD-1"},
        }]
        new_paths = server._paths_for_account(new_email)
        storage_utils.atomic_write_json(new_paths["pending"], [{
            "id": "t7", "account": account_storage.account_key(new_email),
            "kind": "takeoff", "status": "pending", "trigger_at": 777,
            "params": {"route_id": "new-route", "reg": "NEW-1"},
        }])
        server._market_rt_cache = {server._active_account_key: {"balance": "123"}}
        server._maint_cache = {server._active_account_key: {"warnings": [{"注册号": "OLD-1"}]}}
        self.assertTrue(server._sync_account_context(new_email, "new-password"))
        self.assertEqual(server._active_account_email, new_email)
        self.assertEqual(server._pending_tasks[0]["params"]["reg"], "NEW-1")
        self.assertNotIn(server._active_account_key, server._market_rt_cache)
        self.assertNotIn(server._active_account_key, server._maint_cache)

    def test_account_switch_waits_while_collector_is_running(self):
        old_email = server._active_account_email
        server._runs["fake-running"] = {"running": True}
        try:
            self.assertFalse(
                server._sync_account_context("next@example.invalid", "next-password"))
            self.assertEqual(server._active_account_email, old_email)
        finally:
            server._runs.clear()

    def test_env_admin_bootstrap_creates_admin_when_missing(self):
        with patch.object(panel_store, "admin_exists", return_value=False), \
             patch.object(panel_store, "create_user") as create, \
             patch.dict(os.environ,
                        {"AM4_ADMIN_USERNAME": "root", "AM4_ADMIN_PASSWORD": "root123"},
                        clear=False):
            server._bootstrap_admin_from_env()
        create.assert_called_once_with(
            "root", "root123", is_admin=True, status="active")

    def test_env_admin_bootstrap_skips_without_credentials(self):
        with patch.object(panel_store, "admin_exists", return_value=False), \
             patch.object(panel_store, "create_user") as create, \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AM4_ADMIN_USERNAME", None)
            os.environ.pop("AM4_ADMIN_PASSWORD", None)
            server._bootstrap_admin_from_env()
        create.assert_not_called()

    def test_unknown_balance_rejects_new_purchase_after_readonly_lookup(self):
        planner = Mock()
        planner.DEFAULT_COST_INDEX = 200
        planner.aircraft_by_name.return_value = {
            "id": "344", "name": "Test Jet", "capacity": 100, "type": "0",
            "eid": "312",
        }
        planner.airport_by_id.side_effect = lambda airport_id: {
            "id": airport_id, "name": f"Airport {airport_id}", "iata": "TST",
        }
        planner.estimate_route_local.return_value = {
            "feasible": True, "initial_investment": 10_000_000,
            "distance_km": 1000, "flight_hours": 2, "revenue_per_day": 1,
            "net_per_day": 1, "payback_days": 10,
        }
        planner._fleet_aircraft_id.return_value = None
        payload = {
            "ac": "Test Jet", "dep_hub_id": "hub-1", "dep_airport_id": "ap-1",
            "arr_airport_id": "ap-2", "reg": "SAFE-1", "tpd": 1,
            "economy": 100, "business": 0, "first": 0,
        }
        with server.app.test_client() as client, \
             patch.object(server, "_get_route_planner", return_value=planner) as get_planner, \
             patch.object(server, "_current_balance", return_value=None), \
             patch.object(server, "_publish_log"):
            response = client.post(
                "/api/route/build", json=payload,
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("余额尚未确认", response.get_json()["error"])
        self.assertTrue(any(call.kwargs.get("require_login")
                            for call in get_planner.call_args_list))
        planner.build_route.assert_not_called()

    def test_existing_aircraft_recovery_does_not_require_purchase_balance(self):
        planner = Mock()
        planner.DEFAULT_COST_INDEX = 200
        planner.FleetLookupError = route_planner.FleetLookupError
        planner.aircraft_by_name.return_value = {
            "id": "344", "name": "Test Jet", "capacity": 100, "type": "0", "eid": "312",
        }
        planner.airport_by_id.side_effect = lambda airport_id: {
            "id": airport_id, "name": f"Airport {airport_id}", "iata": "TST",
        }
        planner.estimate_route_local.return_value = {
            "feasible": True, "initial_investment": 10_000_000,
            "distance_km": 1000, "flight_hours": 2, "revenue_per_day": 1,
            "net_per_day": 1, "payback_days": 10,
        }
        planner._fleet_aircraft_id.return_value = "fid-existing"
        planner.build_route.return_value = {
            "ok": True, "steps": [], "waiting_delivery": True,
            "fid": "fid-existing", "remain_sec": 600,
        }
        payload = {
            "ac": "Test Jet", "dep_hub_id": "hub-1", "dep_airport_id": "ap-1",
            "arr_airport_id": "ap-2", "reg": "SAFE-1", "tpd": 1,
            "economy": 100, "business": 0, "first": 0,
        }
        with server.app.test_client() as client, \
             patch.object(server, "_get_route_planner", return_value=planner), \
             patch.object(server, "_current_balance") as balance, \
             patch.object(server, "_publish_log"):
            response = client.post(
                "/api/route/build", json=payload,
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 200)
        balance.assert_not_called()
        self.assertEqual(planner.build_route.call_args.kwargs["confirmed_fid"], "fid-existing")
        self.assertFalse(planner.build_route.call_args.kwargs["delivery_confirmed"])

    def test_build_requires_origin_airport_id(self):
        planner = Mock()
        planner.aircraft_by_name.return_value = {
            "id": "344", "name": "Test Jet", "capacity": 100, "type": "0",
        }
        with server.app.test_client() as client, \
             patch.object(server, "_get_route_planner", return_value=planner):
            response = client.post(
                "/api/route/build",
                json={"ac": "Test Jet", "dep_hub_id": "hub-1",
                      "arr_airport_id": "ap-2", "reg": "SAFE-1"},
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 400)
        planner.build_route.assert_not_called()

    def test_invalid_retrofit_is_rejected_before_online_write(self):
        planner = Mock()
        planner.DEFAULT_COST_INDEX = 200
        planner.aircraft_by_name.return_value = {
            "id": "344", "name": "Test Jet", "capacity": 100, "type": "0",
        }
        planner.airport_by_id.side_effect = lambda airport_id: {
            "id": airport_id, "name": f"Airport {airport_id}", "iata": "TST",
        }
        payload = {
            "ac": "Test Jet", "dep_hub_id": "hub-1", "dep_airport_id": "ap-1",
            "arr_airport_id": "ap-2", "reg": "SAFE-1", "tpd": 1,
            "economy": 100, "business": 0, "first": 0,
            "retrofit": "speed,unknown",
        }
        with server.app.test_client() as client, \
             patch.object(server, "_get_route_planner", return_value=planner) as get_planner:
            response = client.post(
                "/api/route/build", json=payload,
                headers={"X-CSRF-Token": server._csrf_token},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("改装配置无效", response.get_json()["error"])
        self.assertFalse(any(call.kwargs.get("require_login")
                             for call in get_planner.call_args_list))
        planner.build_route.assert_not_called()


class RouteRecoveryTests(unittest.TestCase):
    AIRCRAFT = {"id": "344", "name": "Test Jet", "capacity": 100, "eid": "312"}

    def setUp(self):
        # 隔离账号上下文：避免前序用例改动的循环归属影响本类测试
        server._active_account_email = "tests@example.invalid"
        server._active_account_password = "test-password"
        server._active_account_key = account_storage.account_key("tests@example.invalid")
        server._loop_owner_email = "tests@example.invalid"
        server._loop_owner_pinned = False
        server._refresh_pending_mirror()
        server._pending_tasks[:] = []

    @staticmethod
    def _retrofit_page(*completed: str) -> str:
        keys = {"co2": "mod1", "speed": "mod2", "fuel": "mod3"}
        done = {keys[key] for key in completed}
        return "".join(
            f'<input type="checkbox" class=\'mod-check\' id=\'{mod}\''
            f'{" disabled checked" if mod in done else ""}>'
            for mod in ("mod1", "mod2", "mod3")
        )

    def test_unknown_fleet_lookup_never_orders_aircraft(self):
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_fleet_aircraft_id",
                          side_effect=route_planner.FleetLookupError("timeout")), \
             patch.object(route_planner, "_do_curl") as curl:
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1", retrofit="all",
            )
        self.assertTrue(result["waiting_fleet_lookup"])
        curl.assert_not_called()

    def test_login_page_is_not_treated_as_confirmed_absence(self):
        login = '<form action="weblogin/login.php"><input name="lEmail"></form>'
        with patch.object(route_planner, "_do_curl", return_value=login):
            with self.assertRaises(route_planner.FleetLookupError):
                route_planner._fleet_aircraft_id("344", "SAFE-1")

    def test_valid_empty_fleet_group_confirms_absence(self):
        page = "<a href='fleet_ground.php?mode=all&type=344&state=ground'>Ground all</a>"
        with patch.object(route_planner, "_do_curl", return_value=page):
            self.assertIsNone(route_planner._fleet_aircraft_id("344", "SAFE-1"))

    def test_fleet_lookup_decodes_registration_html_entities(self):
        page = (
            "<a href='fleet_ground.php?mode=all&type=344&state=ground'>Ground all</a>"
            "<a href='fleet_details.php?id=123&returnType=0'>SAFE&amp;1</a>"
        )
        with patch.object(route_planner, "_do_curl", return_value=page):
            self.assertEqual(route_planner._fleet_aircraft_id("344", "SAFE&1"), "123")

    def test_retrofit_login_page_never_sends_write(self):
        login = '<form action="weblogin/login.php"><input name="lEmail"></form>'
        with patch.object(route_planner, "_do_curl", return_value=login) as curl:
            result = route_planner._apply_retrofit(
                "fid-1", 100, 0, 0, {"co2", "speed", "fuel"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertEqual(curl.call_count, 1)

    def test_submitted_retrofit_confirmation_is_strictly_read_only(self):
        with patch.object(route_planner, "_do_curl", return_value="") as curl:
            result = route_planner._confirm_retrofit(
                "fid-1", {"co2", "speed", "fuel"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertIn("确认页暂不可用", result["msg"])
        curl.assert_called_once()

    def test_submitted_retrofit_confirmation_detects_completion(self):
        completed = self._retrofit_page("co2", "speed", "fuel")
        with patch.object(route_planner, "_do_curl", return_value=completed) as curl:
            result = route_planner._confirm_retrofit(
                "fid-1", {"co2", "speed", "fuel"})
        self.assertTrue(result["ok"])
        self.assertIn("改装已完成", result["msg"])
        curl.assert_called_once()

    def test_retrofit_write_requires_readonly_confirmation(self):
        initial = self._retrofit_page()
        confirmed = self._retrofit_page("co2", "speed", "fuel")
        with patch.object(route_planner, "_do_curl",
                          side_effect=[initial, "submitted", confirmed]) as curl:
            result = route_planner._apply_retrofit(
                "fid-1", 100, 0, 0, {"co2", "speed", "fuel"})
        self.assertTrue(result["ok"])
        self.assertEqual(curl.call_count, 3)

    def test_unconfirmed_retrofit_stays_retryable(self):
        initial = (
            self._retrofit_page()
            + "<script>if (ready && 1==1) {}\n"
            + "mod1time = 800 * 1.5; mod2time = 800 * 1.5; "
            + "mod3time = 800 * 1.5;</script>"
        )
        with patch.object(route_planner, "_do_curl",
                          side_effect=[initial, "submitted", initial]):
            result = route_planner._apply_retrofit(
                "fid-1", 100, 0, 0, {"co2", "speed", "fuel"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["install_secs"], 3600)
        self.assertIn("尚未确认", result["msg"])

    def test_retrofit_confirmation_network_error_keeps_submitted_state(self):
        initial = self._retrofit_page()
        with patch.object(
                route_planner, "_do_curl",
                side_effect=[initial, "submitted", OSError("confirmation timeout")]):
            result = route_planner._apply_retrofit(
                "fid-1", 100, 0, 0, {"co2", "speed", "fuel"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["install_secs"], 0)
        self.assertIn("确认页抓取失败", result["msg"])

    def test_successful_order_not_yet_listed_uses_readonly_recovery(self):
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl", return_value="order accepted"), \
             patch.object(route_planner, "_fleet_aircraft_id", return_value=None):
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                fleet_absence_confirmed=True,
            )
        self.assertTrue(result["waiting_fleet_lookup"])
        self.assertTrue(result["ok"])

    def test_balance_refresh_failure_does_not_reclassify_successful_order(self):
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl", return_value="order accepted"), \
             patch.object(route_planner, "_fleet_aircraft_id", return_value=None):
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                fleet_absence_confirmed=True,
                after_spend=Mock(side_effect=RuntimeError("balance refresh failed")),
            )
        self.assertTrue(result["waiting_fleet_lookup"])
        self.assertNotIn("下单响应未确认", result["steps"][-1]["msg"])

    def test_multi_engine_lookup_never_silently_uses_another_engine(self):
        rows = [
            {"name": "Twin", "eid": "1", "ename": "Fast"},
            {"name": "Twin", "eid": "2", "ename": "Efficient"},
        ]
        with patch.object(route_planner, "_load_aircraft_models", return_value=rows):
            self.assertEqual(route_planner.aircraft_by_name("Twin", "2")["ename"], "Efficient")
            self.assertIsNone(route_planner.aircraft_by_name("Twin", "999"))

    def test_cargo_order_uses_cargo_endpoint_and_split_holds(self):
        cargo = dict(self.AIRCRAFT, type="1", capacity=127480, eid="330")
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl", return_value="order accepted") as curl, \
             patch.object(route_planner, "_fleet_aircraft_id", return_value=None):
            result = route_planner.build_route(
                cargo, "hub-1", "ap-2", "CARGO-1", cargo_l=35, cargo_h=65,
                fleet_absence_confirmed=True,
            )
        self.assertTrue(result["waiting_fleet_lookup"])
        order_url = curl.call_args.args[0]
        self.assertTrue(urlparse(order_url).path.endswith("/ac_order_do_cargo.php"))
        query = parse_qs(urlparse(order_url).query)
        self.assertEqual(query["engine"], ["330"])
        self.assertEqual(int(query["aft"][0]) + int(query["fwd"][0]), 65)
        self.assertEqual(query["reg"], ["CARGO-1"])

    def test_cargo_retrofit_uses_absolute_large_and_heavy_loads(self):
        initial = (self._retrofit_page() +
                   "<script>var maxLoad = 127480; var x='modType=cargo';</script>")
        confirmed = (self._retrofit_page("co2", "speed", "fuel") +
                     "<script>var maxLoad = 127480; var x='modType=cargo';</script>")
        with patch.object(route_planner, "_do_curl",
                          side_effect=[initial, "submitted", confirmed]) as curl:
            result = route_planner._apply_retrofit(
                "fid-cargo", 0, 0, 0, {"co2", "speed", "fuel"}, cargo_l=35, cargo_h=65)
        self.assertTrue(result["ok"])
        query = parse_qs(urlparse(curl.call_args_list[1].args[0]).query)
        self.assertEqual(query["modType"], ["cargo"])
        self.assertEqual(int(query["large"][0]) + int(query["heavy"][0]), 127480)
        self.assertEqual(int(query["heavy"][0]), round(127480 * 0.65))

    def test_cargo_route_creation_uses_large_and_heavy_ticket_prices(self):
        cargo = dict(self.AIRCRAFT, type="1", capacity=127480, eid="330")
        flight_info = "<div>建立新航線</div>"
        panel = "<input id='routeReg' value='CARGO-R'><div>建立航線費用</div>" + "x" * 150
        created = "addRouteToMap(1,88776,2);"
        origin = {"id": "ap-1", "lat": 0, "lng": 0}
        dest = {"id": "ap-2", "lat": 0, "lng": 10}
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl",
                          side_effect=[flight_info, panel, created]) as curl, \
             patch.object(route_planner, "airport_by_id",
                          side_effect=lambda x: origin if x == "ap-1" else dest):
            result = route_planner.build_route(
                cargo, "hub-1", "ap-2", "CARGO-1", cargo_l=35, cargo_h=65,
                confirmed_fid="fid-cargo", delivery_confirmed=True,
                origin_airport_id="ap-1",
            )
        self.assertEqual(result["route_id"], "88776")
        query = parse_qs(urlparse(curl.call_args_list[2].args[0]).query)
        expected = route_planner._optimal_cargo_ticket(
            route_planner.haversine_km(origin, dest), route_planner.DEFAULT_GAME_MODE)
        self.assertEqual(float(query["e"][0]), expected["l"])
        self.assertEqual(float(query["b"][0]), expected["h"])
        self.assertEqual(query["f"], ["0"])

    def test_existing_route_id_is_recovered_from_flight_info(self):
        page = '<a href="route_depart.php?id=98765&ref=list">depart</a>'
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl", return_value=page):
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                confirmed_fid="fid-1", delivery_confirmed=True, retrofit="all",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["route_id"], "98765")
        self.assertTrue(result["existing_route"])

    def test_new_route_result_keeps_aircraft_id_for_retrofit(self):
        flight_info = "<div>建立新航線</div>"
        panel = ("<input id='routeReg' value='SAFE-ROUTE'>"
                 "<script>autoPrice(100,200,300,0)</script>" + "x" * 120)
        created = "addRouteToMap(1,98765,2);"
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl",
                          side_effect=[flight_info, panel, created]):
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                confirmed_fid="fid-1", delivery_confirmed=True, retrofit="all",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["route_id"], "98765")
        self.assertEqual(result["fid"], "fid-1")

    def test_existing_route_uses_at_most_one_homepage_fallback(self):
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl",
                          side_effect=["<div>已有航线</div>", "home"]) as curl, \
             patch.object(route_planner, "parse_status_data",
                          return_value={"fid-1": {"航线ID": "54321"}}):
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                confirmed_fid="fid-1", delivery_confirmed=True, retrofit="all",
            )
        self.assertEqual(result["route_id"], "54321")
        self.assertEqual(curl.call_count, 2)

    def test_route_status_failure_never_attempts_route_creation(self):
        with patch.object(route_planner, "_ensure_login"), \
             patch.object(route_planner, "_do_curl", side_effect=TimeoutError("timeout")) as curl:
            result = route_planner.build_route(
                self.AIRCRAFT, "hub-1", "ap-2", "SAFE-1",
                confirmed_fid="fid-1", delivery_confirmed=True, retrofit="all",
            )
        self.assertTrue(result["waiting_route_lookup"])
        self.assertEqual(curl.call_count, 1)

    def test_takeoff_constraints_keep_latest_ready_time(self):
        with patch.object(server, "_save_pending_tasks"):
            server._add_takeoff_task(
                "SAFE-1", "route-1", 200, 620, "维护完成后起飞",
                ready_at=500, reason="维护/改装完成",
            )
            server._add_takeoff_task(
                "SAFE-1", "route-1", 200, 520, "落地后起飞",
                ready_at=400, reason="落地",
            )
        self.assertEqual(len(server._pending_tasks), 1)
        task = server._pending_tasks[0]
        self.assertEqual(task["trigger_at"], 620)
        self.assertEqual(task["params"]["ready_at"], 500)
        self.assertEqual(task["params"]["reason"], "维护/改装完成")

    def test_repeated_maintenance_takeover_reports_no_trigger_change(self):
        with patch.object(server, "_save_pending_tasks"):
            first = server._add_takeoff_task(
                "SAFE-1", "route-1", 200, 620, "维护完成后起飞",
                ready_at=500, reason="维护/改装完成",
            )
            repeated = server._add_takeoff_task(
                "SAFE-1", "route-1", 200, 620, "维护完成后起飞",
                ready_at=500, reason="维护/改装完成",
            )
        self.assertFalse(first["deduplicated"])
        self.assertTrue(repeated["deduplicated"])
        self.assertFalse(repeated["trigger_changed"])


if __name__ == "__main__":
    unittest.main()
