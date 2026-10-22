"""自动检修与货运营销：费用、重复扣款保护及账号隔离回归。"""
from bootstrap import *
from contextlib import ExitStack
import operation_automation as ops


# 按官网只读确认页的结构保留最小表单，不包含真实账号或凭据。
MAINT = """
<div id='typeCheck' class='checkTypesList'>
  <span>檢修時間</span><div>03:21:11</div>
  <span>維修費用</span><div>$ 219,144</div>
  <span>檢修費用</span><div>$ 494,428</div>
  <span>總價格</span><div>$ 713,572</div>
  <button onClick="Ajax('maint_plan_do.php?mode=do&type=check&id=123','maintPlanActionDo',this);">計畫檢修</button>
</div><div id='typeRepair'>
  <span>維修時間</span><div>00:58:47</div>
  <span>總價格</span><div>$ 219,144</div>
  <button onClick="Ajax('maint_plan_do.php?mode=do&type=repair&id=123','maintPlanActionDo',this);">計畫維修</button>
</div><div id='typeModify'></div>
"""
CARGO = """
<option value='6'>24 小時</option>
<button onClick="Ajax('marketing_new.php?type=2&c=4&mode=do&d='+$('#dSelector').val());"><span id='c4'>6,934,500</span></button>
<script>if(val==5) {var costFactor = 3.8;}else{var costFactor = 4.5;}
var c4 = 6934500*costFactor;</script>
"""
HOME = """<span id='headerAccount'>100,000,000</span>
<script>statusData[123] = {reg: 'TEST-1', hoursToCheck: 5, wear: 81, grounded: 0, routeId: 77};</script>"""
STATUS = collector.parse_status_data(HOME)["123"]


class AutomaticOperationsTests(unittest.TestCase):
    def test_thresholds_and_check_includes_repair(self):
        self.assertEqual(ops.maintenance_action(STATUS, {}), "check")
        self.assertEqual(ops.maintenance_action({**STATUS, "距A-Check小时": "6"}, {}), "repair")
        self.assertIsNone(ops.maintenance_action({"距A-Check小时": "6", "损坏率%": "79.99"}, {}))
        self.assertIsNone(ops.maintenance_action({"距A-Check小时": "NaN", "损坏率%": "bad"}, {}))
        self.assertEqual(ops.maintenance_action({**STATUS, "距A-Check小时": "-1"}, {}), "check")
        self.assertEqual(collector.parse_status_data(HOME.replace("hoursToCheck: 5", "hoursToCheck: -2"))["123"]["距A-Check小时"], "-2")

    def test_maintenance_quote_uses_total_for_correct_action(self):
        self.assertEqual(ops.maintenance_quote(MAINT, "123", "check")["cost"], 713572)
        self.assertEqual(ops.maintenance_quote(MAINT, "123", "repair")["cost"], 219144)
        for page, fid in [(MAINT, "12"), (MAINT.replace("<button ", "<button disabled "), "123"),
                          (MAINT.replace("總價格", "unknown"), "123"), ("login", "123")]:
            with self.subTest(fid=fid, page=page[:20]), self.assertRaises(ValueError):
                ops.maintenance_quote(page, fid, "check")

    def test_cargo_quote_and_reserve_fail_closed(self):
        self.assertEqual(ops.cargo_marketing_cost(CARGO), 31205250)
        for page in ["", CARGO.replace("4.5", "5.0"), CARGO.replace("type=2", "type=1"),
                     CARGO.replace("24 小時", "20 小時"), CARGO.replace("6,934,500", "6,900,000")]:
            with self.subTest(page=page[:20]), self.assertRaises(ValueError):
                ops.cargo_marketing_cost(page)
        ops.check_cash(HOME, 95000000, 5000000)
        for page, cost in [(HOME, 95000001), ("login", 1), (HOME, float("nan"))]:
            with self.assertRaises(ValueError):
                ops.check_cash(page, cost, 5000000)

    def test_cargo_does_not_replace_airline_timer(self):
        page = """<tr><td class='glyphicons-star'>航空聲譽</td><td id='aTimer'></td></tr>
        <tr><td class='glyphicons-cargo'>貨運聲譽</td><td id='cTimer'></td></tr>
        <script>timer('aTimer',99);timer('cTimer',86400);</script>"""
        self.assertEqual(collector._parse_active_marketing(page), {"airline": 99, "cargo": 86400})

    def test_cargo_purchase_checks_quote_and_confirms_active(self):
        active = "<tr><td class='glyphicons-cargo'></td><td id='cTimer'></td></tr><script>timer('cTimer',86400);</script>"
        with patch.object(collector, "_ensure_login"), patch.object(collector.time, "sleep"), \
             patch.object(collector, "fetch", side_effect=[CARGO, HOME, active]), \
             patch.object(collector, "_do_curl", return_value="ok") as write:
            ok, _, remaining = collector._purchase_marketing("cargo4_24h", "货运", known_inactive=True)
        self.assertTrue(ok)
        self.assertEqual(remaining, 86400)
        self.assertIn("type=2&c=4&mode=do&d=6", write.call_args.args[0])
        with patch.object(collector, "_ensure_login"), \
             patch.object(collector, "fetch", side_effect=[CARGO, HOME.replace("100,000,000", "5,000,000")]), \
             patch.object(collector, "_do_curl") as write:
            ok, message, _ = collector._purchase_marketing("cargo4_24h", "货运", known_inactive=True)
        self.assertFalse(ok)
        self.assertIn("现金保护", message)
        write.assert_not_called()

    def task(self):
        return {"kind": "maintenance", "status": "running", "account": "test-owner",
                "params": {"fid": "123", "reg": "TEST-1"}}

    def context(self, status=None, settings=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for obj, name, value in [
            (server, "_current_operation_settings", settings or {"auto_maintenance": True}),
            (server, "_latest_home_maintenance", dict(STATUS if status is None else status)),
            (server, "_find_fleet_row", {"飞机ID": "123"}),
            (server, "_retrofit_blocks_takeoff", None),
            (server, "_account_manually_stopped", False),
            (server, "_publish_log", None), (server, "_refresh_market_after_spend", None),
        ]:
            stack.enter_context(patch.object(obj, name, return_value=value))
        self.save = stack.enter_context(patch.object(server, "_save_pending_tasks"))
        self.fetch = stack.enter_context(patch.object(collector, "fetch", side_effect=[MAINT, HOME]))
        self.write = stack.enter_context(patch.object(collector, "_do_curl", return_value="ok"))
        self.takeoff = stack.enter_context(patch.object(server, "_add_takeoff_task"))
        return stack

    def test_persist_before_write_and_timeout_never_rebuys(self):
        task = self.task()
        self.context()
        def write(*args, **kwargs):
            self.assertIn("maintenance_submitted", task["params"])
            self.save.assert_called_once_with(owner="test-owner", strict=True)
            raise TimeoutError("lost response")
        self.write.side_effect = write
        server._run_maintenance_task(task, Mock())
        server._run_maintenance_task(task, Mock())
        self.write.assert_called_once()
        self.assertEqual(task["status"], "pending")
        self.assertIn("不重复扣款", task["error"])

    def test_storage_failure_prevents_purchase(self):
        task = self.task()
        self.context()
        self.save.side_effect = OSError("disk full")
        server._run_maintenance_task(task, Mock())
        self.write.assert_not_called()
        self.assertNotIn("maintenance_submitted", task["params"])

    def test_maintenance_cash_guard_and_changed_state_prevent_purchase(self):
        for home in [HOME.replace("100,000,000", "5,100,000"),
                     HOME.replace("grounded: 0", "grounded: 1")]:
            with self.subTest(home=home), self.context():
                self.fetch.side_effect = [MAINT, home]
                server._run_maintenance_task(self.task(), Mock())
                self.save.assert_not_called()
                self.write.assert_not_called()

    def test_takeoff_guard_uses_account_thresholds(self):
        with patch.object(server, "_current_operation_settings",
                          return_value={"min_a_check_hours": 20, "max_wear_for_takeoff": 50}):
            self.assertIsNotNone(server._takeoff_maintenance_block(
                "TEST-1", {"距A-Check小时": "19", "损坏率%": "0"}))
            self.assertIsNotNone(server._takeoff_maintenance_block(
                "TEST-1", {"距A-Check小时": "100", "损坏率%": "55"}))

    def test_existing_maintenance_waits_then_recovers_takeoff(self):
        task = self.task()
        with self.context({**STATUS, "维护改装结束时间戳": time.time()+1000}):
            server._run_maintenance_task(task, Mock())
            self.write.assert_not_called()
            self.assertGreater(task["trigger_at"], time.time()+1000)
        with self.context({**STATUS, "距A-Check小时": "400", "损坏率%": "0"}):
            server._run_maintenance_task(task, Mock())
            self.assertEqual(task["status"], "done")
            self.takeoff.assert_called_once()
            self.write.assert_not_called()

    def test_grounded_disabled_unknown_and_inflight_never_purchase(self):
        cases = [({**STATUS, "停飞": "1"}, {}),
                 ({**STATUS, "预计落地时间戳": time.time()+500}, {}),
                 ({}, {}), (STATUS, {"auto_maintenance": False})]
        for status, settings in cases:
            with self.subTest(status=status, settings=settings), self.context(status, settings):
                server._run_maintenance_task(self.task(), Mock())
                self.write.assert_not_called()
                self.takeoff.assert_not_called()

    def test_dedupe_preserves_submission_and_account_isolation(self):
        queues = {"one": [], "two": []}
        with patch.object(server, "_tasks_by_account", queues), \
             patch.object(server, "_current_operation_settings", return_value={"auto_maintenance": True}), \
             patch.object(server, "_read_csv", return_value=[]), \
             patch.object(server, "_save_pending_tasks"), \
             patch.object(server, "_task_owner_key", return_value="one"):
            server._ensure_maintenance_tasks({"123": STATUS})
            task = queues["one"][0]
            task["params"]["maintenance_submitted"] = {"action": "check"}
            task["trigger_at"] = time.time()+20000
            server._ensure_maintenance_tasks({"123": STATUS})
            server._add_pending_task("maintenance", "again", 0, {"fid": "123", "reg": "NEW"})
            self.assertEqual(len(queues["one"]), 1)
            self.assertIn("maintenance_submitted", task["params"])
            self.assertGreater(task["trigger_at"], time.time())
            with patch.object(server, "_task_owner_key", return_value="two"):
                server._ensure_maintenance_tasks({"123": STATUS})
            self.assertEqual(len(queues["two"]), 1)

    def test_cargo_toggle_is_independent_and_defaults_off(self):
        settings = panel_store.normalize_settings({"auto_marketing": False, "auto_cargo_marketing": "1"})
        self.assertTrue(settings["auto_cargo_marketing"])
        self.assertFalse(settings["auto_maintenance"])
        with patch.object(server, "_current_operation_settings", return_value=settings), \
             patch.object(server, "_tasks_by_account", {}), \
             patch.object(server, "_add_pending_task") as add:
            server._ensure_marketing_tasks()
        add.assert_called_once()
        self.assertEqual(add.call_args.args[3], {"campaign": "cargo"})

    def test_long_running_collector_registers_cargo_before_starting_process(self):
        email = "cargo-start@example.invalid"
        owner = account_storage.account_key(email)
        queues, observed = {}, []
        old_account = getattr(server._task_account_ctx, "account", None)
        old_paths = getattr(server._task_account_ctx, "paths", None)
        self.addCleanup(setattr, server._task_account_ctx, "account", old_account)
        self.addCleanup(setattr, server._task_account_ctx, "paths", old_paths)
        run = {"account_email": email, "account_key": owner, "mode": "loop_resume",
               "settings": {"auto_marketing": False, "auto_cargo_marketing": True},
               "paths": {"fleet": Path("unused"), "maint": Path("unused")}}
        def start(*args, **kwargs):
            observed.extend((t["account"], t["params"]["campaign"])
                            for t in queues.get(owner, []))
            raise RuntimeError("test ends after startup")
        with patch.object(server, "_tasks_by_account", queues), \
             patch.object(server, "_save_pending_tasks"), \
             patch.object(server, "_read_csv", return_value=[]), \
             patch.object(server, "_safe_external_error"), \
             patch.object(server, "_broadcast_sse"), \
             patch.object(server.subprocess, "Popen", side_effect=start):
            server._runner(run)
        self.assertEqual(observed, [(owner, "cargo")])
