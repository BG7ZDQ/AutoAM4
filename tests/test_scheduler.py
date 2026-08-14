"""???????????????/??/??????????"""
from bootstrap import *


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

    def test_task_never_executes_with_mismatched_thread_context(self):
        task = {
            "account": account_storage.account_key("other@example.invalid"),
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        # 线程上下文归属另一个账号：必须取消，绝不能串号执行
        server._task_account_ctx.account = {
            "email": "wrong@example.invalid", "password": "p",
            "settings": panel_store.DEFAULT_SETTINGS,
        }
        try:
            with patch.object(server, "_get_route_planner") as get_planner:
                server._run_pending_task(task)
            self.assertEqual(task["status"], "cancelled")
            get_planner.assert_not_called()
        finally:
            server._task_account_ctx.account = None

    def test_task_executes_with_matching_thread_context(self):
        task = {
            "account": account_storage.account_key("tests@example.invalid"),
            "kind": "takeoff", "status": "running", "trigger_at": 0,
            "params": {"fid": "fid-1", "route_id": "route-1", "reg": "SAFE-1"},
        }
        server._task_account_ctx.account = {
            "email": "tests@example.invalid", "password": "p",
            "settings": panel_store.DEFAULT_SETTINGS,
        }
        try:
            with patch.object(server, "_get_route_planner") as get_planner, \
                 patch.object(server, "_current_fuel_lbs", return_value=500000), \
                 patch.object(server, "_latest_home_maintenance",
                              return_value={"停飞": "0"}), \
                 patch.object(server, "_refresh_fleet_row", return_value=None), \
                 patch.object(server, "_add_takeoff_task"), \
                 patch.object(server, "_publish_log"):
                server._run_pending_task(task)
            self.assertNotEqual(task["status"], "cancelled")
            get_planner.assert_called()
        finally:
            server._task_account_ctx.account = None

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

    def test_protected_account_helpers(self):
        with patch.dict(os.environ,
                        {"AM4_PROTECTED_ACCOUNTS": "bg7zdq@satellites.ac.cn"}):
            server.PROTECTED_ACCOUNTS = {
                server.normalize_account(x)
                for x in os.environ.get("AM4_PROTECTED_ACCOUNTS", "").split(",")
                if x.strip()
            }
            try:
                self.assertTrue(
                    server._account_protected("bg7zdq@satellites.ac.cn"))
                self.assertFalse(
                    server._account_protected("other@example.com"))
            finally:
                server.PROTECTED_ACCOUNTS = set()

    def test_protected_account_rejected_for_loop(self):
        server.PROTECTED_ACCOUNTS = {
            server.normalize_account("bg7zdq@satellites.ac.cn")}
        try:
            ok, msg = server._start_loop(
                "bg7zdq@satellites.ac.cn", "pw", {}, "loop")
            self.assertFalse(ok)
            self.assertIn("受保护", msg)
            self.assertNotIn("bg7zdq_satellites_ac_cn_48d93926b2fc",
                             server._runs)
        finally:
            server.PROTECTED_ACCOUNTS = set()

    def test_env_admin_bootstrap_creates_admin_when_missing(self):
        with patch.object(panel_store, "admin_exists", return_value=False), \
             patch.object(panel_store, "get_user_by_username", return_value=None), \
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

    def test_register_rate_limited_per_ip(self):
        server._register_attempts.clear()
        try:
            for _ in range(10):
                self.assertFalse(server._register_blocked("1.2.3.4"))
            self.assertTrue(server._register_blocked("1.2.3.4"))
            self.assertFalse(server._register_blocked("5.6.7.8"))
        finally:
            server._register_attempts.clear()

    def test_register_rate_limited_per_username(self):
        server._register_attempts.clear()
        try:
            for _ in range(3):
                self.assertFalse(server._register_blocked("9.9.9.9", "SameName"))
            self.assertTrue(server._register_blocked("9.9.9.9", "SameName"))
            # 同一 IP 换用户名不受该用户名桶影响（IP 桶也未到上限）
            self.assertFalse(server._register_blocked("9.9.9.9", "DifferentName"))
        finally:
            server._register_attempts.clear()

    def test_security_headers_present(self):
        with server.app.test_client() as client:
            resp = client.get("/login")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")

    def test_login_requires_csrf(self):
        with server.app.test_client() as client:
            resp = client.post(
                "/api/login",
                json={"username": "tadmin", "password": "test-pass-1"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_write_requires_csrf(self):
        with server.app.test_client() as client:
            resp = client.post("/api/admin/users/1/status",
                               json={"status": "active"})
        self.assertEqual(resp.status_code, 403)

    def test_verify_am4_endpoint(self):
        with server.app.test_client() as client:
            # 未带 CSRF：拒绝写请求
            resp = client.post(
                "/api/verify-am4",
                json={"am4_email": "x@example.com", "am4_password": "p"})
            self.assertEqual(resp.status_code, 403)
            # 独立 Cookie 登录成功：主页含登录特征 → 通过
            with patch.object(collector, "_do_curl",
                              side_effect=["", "", "ok headerAccount home"]) as curl:
                resp = client.post(
                    "/api/verify-am4",
                    json={"am4_email": "ok@example.com",
                          "am4_password": "secret"},
                    headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            self.assertEqual(curl.call_count, 3)
            # 密码错误：主页没有登录特征 → 明确报错
            with patch.object(collector, "_do_curl",
                              side_effect=["", "", "login form"]) as curl:
                resp = client.post(
                    "/api/verify-am4",
                    json={"am4_email": "bad@example.com",
                          "am4_password": "wrong"},
                    headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.get_json()["ok"])
            self.assertIn("不正确", resp.get_json()["msg"])

    def test_setup_creates_pure_admin_without_game_account(self):
        with patch.object(server, "_SETUP_TOKEN", "tok-123"), \
             patch.object(server.panel_store, "admin_exists",
                          return_value=False), \
             server.app.test_client() as client:
            resp = client.post(
                "/api/setup",
                json={"setup_token": "tok-123",
                      "username": "newadmin", "password": "new-pass-1"},
                headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        user = server.panel_store.get_user_by_username("newadmin")
        self.assertIsNotNone(user)
        self.assertEqual(user["is_admin"], 1)
        # 管理员是纯管理身份：不绑定任何游戏账号
        account = server.panel_store.get_account(user["id"])
        self.assertEqual(account.get("am4_email"), "")
        self.assertEqual(account.get("am4_password"), "")
        server.panel_store.delete_user(user["id"])

    def test_start_loop_rejects_missing_credentials(self):
        ok, msg = server._start_loop("nobody@example.com", "", {}, "loop")
        self.assertFalse(ok)
        self.assertIn("凭据", msg)

    def test_run_requires_account_password(self):
        with patch.object(server, "_session_account",
                          return_value={"email": "nobody@example.com",
                                        "password": "", "settings": {}}), \
             server.app.test_client() as client:
            resp = client.post(
                "/api/run", json={"mode": "loop"},
                headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("密码", resp.get_json()["msg"])

    def test_resume_targets_skip_missing_credentials(self):
        tmp = Path(tempfile.mkdtemp()) / "active_loops.json"
        with patch.object(server, "_ACTIVE_LOOPS_FILE", tmp), \
             patch.object(server, "_active_credentials", return_value=("", "")), \
             patch.object(server, "_current_env_credentials",
                          return_value=("", "")):
            self.assertEqual(server._resume_loop_targets(), [])

    def test_setup_endpoint_rate_limited(self):
        with patch.object(server, "_setup_blocked", return_value=True), \
             server.app.test_client() as client:
            resp = client.post(
                "/api/setup", json={},
                headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(resp.status_code, 429)

    def test_manual_stop_marks_run_stopped_immediately(self):
        key = server.account_key("stop@example.com")
        proc = Mock()
        proc.poll.return_value = None
        server._runs[key] = {
            "account_email": "stop@example.com", "running": True,
            "proc": proc, "mode": "loop",
            "paths": server._paths_for_account("stop@example.com"),
        }
        try:
            with patch.object(server, "_session_account",
                              return_value={"email": "stop@example.com",
                                            "password": "p", "settings": {}}), \
                 patch.object(server, "_append_log"), \
                 patch.object(server, "_broadcast_sse"), \
                 patch.object(server, "_persist_active_loops") as persist, \
                 server.app.test_client() as client:
                resp = client.post(
                    "/api/stop",
                    headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(server._runs[key]["running"])
            self.assertEqual(server._runs[key]["mode"], "")
            proc.terminate.assert_called_once()
            persist.assert_called_once()
            # 停止即暂停该账号待办；重新启动循环后自动恢复
            self.assertIn(
                server.normalize_account("stop@example.com"),
                server._stopped_accounts)
            with patch.object(server, "_rotate_run_log"), \
                 patch.object(server, "_broadcast_sse"), \
                 patch.object(server, "_append_log"), \
                 patch.object(server, "_persist_active_loops"), \
                 patch.object(server.threading, "Thread"):
                ok, _ = server._start_loop(
                    "stop@example.com", "p", {}, "loop")
            self.assertTrue(ok)
            self.assertNotIn(
                server.normalize_account("stop@example.com"),
                server._stopped_accounts)
        finally:
            server._runs.pop(key, None)
            server._stopped_accounts.discard(
                server.normalize_account("stop@example.com"))

    def test_pending_task_aborts_when_account_stopped(self):
        server._stopped_accounts.add(server.normalize_account("stop@example.com"))
        old = getattr(server._task_account_ctx, "account", None)
        try:
            task = {"status": "pending", "title": "x", "trigger_at": 0,
                    "account": server.account_key("stop@example.com")}
            server._task_account_ctx.account = {
                "email": "stop@example.com", "password": "p", "settings": {}}
            server._run_pending_task(task)
            self.assertEqual(task["status"], "pending")
            self.assertIn("循环已停止", task["error"])
        finally:
            server._task_account_ctx.account = old
            server._stopped_accounts.discard(
                server.normalize_account("stop@example.com"))

    def test_admin_targets_reject_admin_accounts(self):
        other_admin = panel_store.create_user(
            "othadmin", "other-pass-1", is_admin=True, status="active")
        try:
            with patch.object(server, "_real_user",
                              return_value=panel_store.get_user_by_username("tadmin")), \
                 server.app.test_client() as client:
                resp = client.post(
                    f"/api/admin/users/{other_admin}/password",
                    json={"password": "hacked-pass-1"},
                    headers={"X-CSRF-Token": server._csrf_token})
                self.assertEqual(resp.status_code, 403)
                resp = client.post(
                    f"/api/admin/users/{other_admin}/status",
                    json={"status": "disabled"},
                    headers={"X-CSRF-Token": server._csrf_token})
                self.assertEqual(resp.status_code, 403)
        finally:
            panel_store.delete_user(other_admin)

    def test_protected_account_blocks_online_build(self):
        admin = panel_store.get_user_by_username("tadmin")
        email = panel_store.get_account(admin["id"])["am4_email"]
        server.PROTECTED_ACCOUNTS.add(server.normalize_account(email))
        try:
            with server.app.test_client() as client:
                resp = client.post(
                    "/api/route/build", json={"ac": "anything"},
                    headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(resp.status_code, 403)
            self.assertIn("受保护", resp.get_json()["error"])
        finally:
            server.PROTECTED_ACCOUNTS.discard(server.normalize_account(email))

    def test_protected_account_blocks_online_estimate(self):
        admin = panel_store.get_user_by_username("tadmin")
        email = panel_store.get_account(admin["id"])["am4_email"]
        server.PROTECTED_ACCOUNTS.add(server.normalize_account(email))
        try:
            with server.app.test_client() as client:
                resp = client.get("/api/route/estimate?ac=x&dep=1&arr=2")
            self.assertEqual(resp.status_code, 403)
            self.assertIn("受保护", resp.get_json()["error"])
        finally:
            server.PROTECTED_ACCOUNTS.discard(server.normalize_account(email))

    def test_disabled_account_is_treated_as_protected(self):
        uid = panel_store.create_user(
            "linkuser", "link-pass-1", am4_email="link@example.com",
            status="active")
        try:
            self.assertFalse(server._account_protected("link@example.com"))
            panel_store.set_user_status(uid, "disabled")
            self.assertTrue(server._account_protected("LINK@example.com "))
            panel_store.set_user_status(uid, "active")
            self.assertFalse(server._account_protected("link@example.com"))
        finally:
            panel_store.delete_user(uid)

    def test_disable_user_stops_running_loop(self):
        uid = panel_store.create_user(
            "stopuser", "stop-pass-1", am4_email="stop@example.com",
            status="active")
        key = server.account_key("stop@example.com")
        proc = Mock()
        proc.poll.return_value = None
        server._runs[key] = {
            "account_email": "stop@example.com",
            "account_key": key,
            "password": "",
            "settings": {},
            "mode": "loop",
            "running": True,
            "last_run": None,
            "error": None,
            "progress_total": 0,
            "progress_current": 0,
            "proc": proc,
            "stop_requested": False,
            "paths": server._paths_for_account("stop@example.com"),
        }
        try:
            with patch.object(server, "_real_user",
                              return_value=panel_store.get_user_by_username("tadmin")), \
                 server.app.test_client() as client:
                resp = client.post(
                    f"/api/admin/users/{uid}/status",
                    json={"status": "disabled"},
                    headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(server._runs[key]["stop_requested"])
            proc.terminate.assert_called_once()
        finally:
            server._runs.clear()
            panel_store.delete_user(uid)

    def test_csv_safe_cell_prefixes_formula_start(self):
        self.assertEqual(server._csv_safe_cell("=cmd"), "'=cmd")
        self.assertEqual(server._csv_safe_cell("@mail"), "'@mail")
        self.assertEqual(server._csv_safe_cell("MC-21"), "MC-21")
        self.assertEqual(server._csv_safe_cell(""), "")

    def test_admin_password_synced_from_env(self):
        # 管理员密码只能通过服务器配置修改：.env 配置的密码会在启动时同步
        with patch.dict(os.environ,
                        {"AM4_ADMIN_USERNAME": "tadmin",
                         "AM4_ADMIN_PASSWORD": "env-pass-1"}, clear=False):
            server._bootstrap_admin_from_env()
        try:
            old_login = server.app.test_client().post(
                "/api/login", json={"username": "tadmin",
                                    "password": "test-pass-1"},
                headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(old_login.status_code, 401)
            new_login = server.app.test_client().post(
                "/api/login", json={"username": "tadmin",
                                    "password": "env-pass-1"},
                headers={"X-CSRF-Token": server._csrf_token})
            self.assertEqual(new_login.status_code, 200)
        finally:
            # 恢复原密码，避免影响其他用例
            panel_store.set_user_password(
                panel_store.get_user_by_username("tadmin")["id"], "test-pass-1")

    def test_admin_resets_user_password(self):
        uid = panel_store.create_user(
            "resetbob", "bob-old-pass",
            am4_email="resetbob@example.com", am4_password="p")
        panel_store.set_user_status(uid, "active")
        client = server.app.test_client()
        client.post("/api/login",
                    json={"username": "tadmin", "password": "test-pass-1"},
                    headers={"X-CSRF-Token": server._csrf_token})
        resp = client.post(
            "/api/admin/users/%d/password" % uid,
            json={"password": "bob-new-pass"},
            headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(resp.status_code, 200)
        old = server.app.test_client().post(
            "/api/login", json={"username": "resetbob", "password": "bob-old-pass"},
            headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(old.status_code, 401)
        new = server.app.test_client().post(
            "/api/login", json={"username": "resetbob", "password": "bob-new-pass"},
            headers={"X-CSRF-Token": server._csrf_token})
        self.assertEqual(new.status_code, 200)

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


