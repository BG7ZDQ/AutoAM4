"""???/CSRF/?????/???????"""
from bootstrap import *


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
        self.assertIn('type="application/json" id="bootData"', template)
        self.assertIn("let SERVER_BOOTSTRAP=BOOT.dashboard_bootstrap||{}", template)
        self.assertIn("restoreServerSnapshot(restoreDashboardCache())", template)
        self.assertIn("a.length+c.length===0&&fullFd.length+fullFdc.length>0", template)
        self.assertLess(template.index("window.__AM4_CACHE_STATUS='hit'"),
                        template.index("let SERVER_BOOTSTRAP=BOOT.dashboard_bootstrap||{}"))
        self.assertIn("window.__AM4_CACHE_WRITE_STATUS='ok'", template)
        self.assertIn("window.addEventListener('pagehide',saveDashboardCache)", template)
        self.assertIn("function logClass(line)", template)
        self.assertIn("line.includes('not_ready'))return'warn'", template)
        self.assertIn(".console .warn{color:var(--wr)}", template)
        self.assertIn("mobile=matchMedia('(max-width:768px)').matches", template)
        self.assertIn("document.getElementById('sb').textContent=mobile?compact(m.balance):m.balance", template)
        self.assertNotIn("loopCards", template)
        self.assertIn("function ownEvent(d)", template)
        self.assertIn('id="runChipTxt"', template)
        self.assertIn("uc(!!BOOT.initial_running)", template)
        self.assertIn("function chips(c)", template)
        self.assertIn("if(row['_pending_build'])tr.style.background='rgba(255,193,7,.12)'", template)
        self.assertIn("function order(rows)", template)
        self.assertIn("renderHubOptions();applyFleetView();rm()", template)
        self.assertLess(template.index("window.st=function(t)"),
                        template.index("let SERVER_BOOTSTRAP=BOOT.dashboard_bootstrap||{}"))

