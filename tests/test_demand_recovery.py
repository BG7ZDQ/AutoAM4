from bootstrap import *


class DemandRecoveryTests(unittest.TestCase):
    def test_single_aircraft_refresh_pushes_latest_demand_to_its_account(self):
        old = {"飞机ID": "20301437", "注册号": "MC-21-4-97", "需求状态": "不足",
               "商务舱需求": "59", "头等舱需求": "28", "客机组数量": "1"}
        new = {**old, "需求状态": "旺盛", "商务舱需求": "429", "头等舱需求": "246",
               "最后更新时间": "2026-10-26 13:00:00"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "_paths", return_value={"fleet": Path(tmp)/"fleet.csv"}), \
             patch.object(server, "_hub_name_by_id", return_value=""), \
             patch.object(server, "_infer_hub_name", return_value=""), \
             patch.object(server, "_session_account", return_value={"email": "owner@example.invalid"}), \
             patch.object(route_planner, "fetch_aircraft_fleet_row", return_value=new), \
             patch.object(server, "_broadcast_sse") as broadcast:
            server._write_fleet_csv([old])
            server._refresh_fleet_row(old["注册号"], old["飞机ID"], "")
            saved = server._read_csv(Path(tmp)/"fleet.csv")[0]
        self.assertEqual(saved["需求状态"], "旺盛")
        event = broadcast.call_args.args[0]
        self.assertEqual(event["type"], "aircraft")
        self.assertEqual(event["account"], "owner@example.invalid")
        self.assertEqual(event["data"]["商务舱需求"], "429")


    def test_daily_initial_read_does_not_repeat_before_reset(self):
        for state, expected in [("旺盛", "旺盛"), ("不足", "待复核"), ("", "")]:
            with self.subTest(state=state), patch.object(collector, "fetch", return_value="page") as fetch, \
                 patch.object(collector, "parse_aircraft_detail", return_value={"demand_status": state}):
                result = collector.fetch_detail_with_demand_retries("url", referer="fleet", label="TEST")
                self.assertEqual(result["demand_status"], expected)
                fetch.assert_called_once()

    def test_empty_initial_response_does_not_overwrite_fleet(self):
        with patch.object(collector, "fetch", return_value="") as fetch:
            self.assertIsNone(collector.fetch_detail_with_demand_retries("url", referer="fleet", label="TEST"))
            fetch.assert_called_once()


class DailyDemandRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tasks = []
        for p in [patch.object(server, "_task_owner_key", return_value="owner"),
                  patch.object(server, "_tasks_by_account", {"owner": self.tasks}),
                  patch.object(server, "_save_pending_tasks"), patch.object(server, "_publish_log"),
                  patch.object(server, "_read_csv", return_value=[]),
                  patch.object(server, "_set_demand_check_label"),
                  patch.object(server, "_removed_aircraft_guard", return_value=False),
                  patch.object(server, "_current_operation_settings", return_value={"auto_takeoff": True})]:
            p.start(); self.addCleanup(p.stop)

    def task(self):
        return {"kind": "demand_recheck", "account": "owner", "status": "running", "params": {
            "fid": "123", "reg": "TEST", "cycle": server._now_bjt().date().isoformat(), "attempts": 0}}

    def test_two_spaced_checks_and_no_third(self):
        task = self.task()
        with patch.object(server.time, "time", return_value=1000), \
             patch.object(server, "_refresh_fleet_row", side_effect=[{"需求状态": "待复核"}, {"需求状态": "不足"}]) as refresh:
            server._run_daily_demand_recheck(task, Mock())
            self.assertEqual(task["status"], "pending")
            self.assertEqual(task["trigger_at"], 4600)
            self.assertTrue(refresh.call_args.kwargs["defer_insufficient"])
            server._run_daily_demand_recheck(task, Mock())
            self.assertEqual(task["status"], "done")
            self.assertFalse(refresh.call_args.kwargs["defer_insufficient"])
            server._run_daily_demand_recheck(task, Mock())
            self.assertEqual(refresh.call_count, 2)

    def test_recovery_uses_live_route_and_maintenance_end(self):
        task = self.task()
        with patch.object(server.time, "time", return_value=1000), \
             patch.object(server, "_refresh_fleet_row", return_value={"需求状态": "旺盛"}), \
             patch.object(server, "_latest_home_maintenance", return_value={"航线ID": "new", "预计落地时间戳": "3000", "维护改装结束时间戳": "4000"}), \
             patch.object(server, "_accept_takeoff_takeover") as enroll:
            server._run_daily_demand_recheck(task, Mock())
        self.assertEqual(task["status"], "done")
        self.assertEqual(enroll.call_args.args[0]["route_id"], "new")
        self.assertEqual(enroll.call_args.args[0]["trigger_at"], 4120)

    def test_manual_grounding_or_disabled_takeoff_never_enrolls(self):
        for enabled in [True, False]:
            with patch.object(server, "_current_operation_settings", return_value={"auto_takeoff": enabled}), \
                 patch.object(server, "_refresh_fleet_row", return_value={"需求状态": "旺盛"}), \
                 patch.object(server, "_latest_home_maintenance", return_value={"航线ID": "7", "停飞": "1"}), \
                 patch.object(server, "_accept_takeoff_takeover") as enroll:
                server._run_daily_demand_recheck(self.task(), Mock())
                enroll.assert_not_called()

    def test_recovery_retry_does_not_read_demand_again(self):
        task = self.task()
        with patch.object(server, "_refresh_fleet_row", return_value={"需求状态": "旺盛"}) as refresh, \
             patch.object(server, "_latest_home_maintenance", side_effect=[None, {"停飞": "1"}]):
            server._run_daily_demand_recheck(task, Mock())
            server._run_daily_demand_recheck(task, Mock())
            refresh.assert_called_once()
            self.assertEqual(task["status"], "done")

    def test_unknown_never_becomes_confirmed_insufficient(self):
        task = self.task()
        with patch.object(server, "_refresh_fleet_row", return_value=None):
            server._run_daily_demand_recheck(task, Mock())
            server._run_daily_demand_recheck(task, Mock())
        self.assertEqual(task["status"], "failed")

    def test_duplicate_registration_preserves_progress_and_delay(self):
        row = {"飞机ID": "123", "注册号": "TEST", "需求状态": "待复核", "最后更新时间": str(server._now_bjt())}
        server._register_daily_demand_recheck(row)
        self.tasks[0]["params"]["attempts"] = 1
        self.tasks[0]["trigger_at"] = 7000
        server._register_daily_demand_recheck(row)
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.tasks[0]["params"]["attempts"], 1)
        self.assertEqual(self.tasks[0]["trigger_at"], 7000)
        self.assertEqual(self.tasks[0]["account"], "owner")

    def test_new_day_supersedes_old_check(self):
        old = self.task(); old["params"]["cycle"] = "2000-01-01"; self.tasks.append(old)
        server._register_daily_demand_recheck({"飞机ID": "123", "注册号": "TEST", "需求状态": "待复核"})
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(self.tasks[-1]["params"]["attempts"], 0)

    def test_attempt_must_be_persisted_before_fetch(self):
        task = self.task()
        with patch.object(server, "_save_pending_tasks", side_effect=OSError("disk")), \
             patch.object(server, "_refresh_fleet_row") as fetch, self.assertRaises(OSError):
            server._run_daily_demand_recheck(task, Mock())
        fetch.assert_not_called()
        self.assertEqual(task["params"]["attempts"], 0)

    def test_pending_check_survives_restart(self):
        future = time.time()+7000
        task = {**self.task(), "id": "t1", "trigger_at": future, "status": "pending"}
        task["params"]["attempts"] = 1
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "_pending_tasks", []), \
             patch.object(server, "_load_builds", return_value=[]):
            path = Path(tmp)/"pending.json"
            path.write_text(json.dumps([task]), encoding="utf-8")
            server._load_pending_tasks(path, "owner")
            recovered = server._tasks_by_account["owner"][0]
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["params"]["attempts"], 1)
        self.assertEqual(recovered["trigger_at"], future)

    def test_unknown_first_retry_cannot_confirm_three_insufficient_reads(self):
        task = self.task()
        with patch.object(server, "_refresh_fleet_row", side_effect=[None, {"需求状态": "待复核"}]) as refresh:
            server._run_daily_demand_recheck(task, Mock())
            server._run_daily_demand_recheck(task, Mock())
        self.assertTrue(refresh.call_args.kwargs["defer_insufficient"])
        self.assertEqual(task["status"], "failed")

    def test_crash_after_second_attempt_does_not_make_a_third_request(self):
        task = self.task(); task["params"]["attempts"] = 2
        with patch.object(server, "_refresh_fleet_row") as refresh:
            server._run_daily_demand_recheck(task, Mock())
        refresh.assert_not_called()
        self.assertEqual(task["status"], "failed")
