"""????/????/???????"""
from bootstrap import *


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

    def test_aircraft_rename_keeps_single_takeoff_task_by_fid(self):
        with patch.object(server, "_save_pending_tasks"):
            server._add_takeoff_task(
                "A380-8-02", "route-1", 200, 620, "A380-8-02 下次起飞",
                fid="18923431", reason="本次飞行落地",
            )
            repeated = server._add_takeoff_task(
                "A380-8-2", "route-1", 200, 620, "A380-8-2 下次起飞",
                fid="18923431", reason="本次飞行落地",
            )
        self.assertEqual(len(server._pending_tasks), 1)
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(server._pending_tasks[0]["params"]["reg"], "A380-8-2")
        self.assertEqual(server._pending_tasks[0]["params"]["fid"], "18923431")


if __name__ == "__main__":
    unittest.main()


