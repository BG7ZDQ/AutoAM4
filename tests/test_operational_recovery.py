"""Regression coverage for delayed fleet enrollment and stale build plans."""
from bootstrap import *


class OperationalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.owner = account_storage.account_key("tests@example.invalid")
        self.tasks = []
        self.context = patch.object(server, "_task_owner_key", return_value=self.owner)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.queues = patch.dict(server._tasks_by_account, {self.owner: self.tasks})
        self.queues.start()
        self.addCleanup(self.queues.stop)

    def test_delayed_batch_registers_every_aircraft_once(self):
        # The last enrollment arrives well after the common two-minute deadline.
        with patch.object(server, "_find_fleet_row", return_value=None), \
             patch.object(server, "_retrofit_blocks_takeoff", return_value=None), \
             patch.object(server, "_read_csv", return_value=[]), \
             patch.object(server, "_save_pending_tasks"), \
             patch.object(server.time, "time", return_value=1300), \
             patch.object(route_planner, "takeoff_route") as takeoff:
            for _ in range(2):
                for i in range(172):
                    server._accept_takeoff_takeover({
                        "fid": str(i + 1), "reg": f"JET-{i}",
                        "route_id": str(1000 + i), "trigger_at": 1120,
                        "ready_at": 1000, "reason": "全量扫描发现",
                    })
        self.assertEqual(len(self.tasks), 172)
        self.assertTrue(all(t["status"] == "pending" for t in self.tasks))
        self.assertTrue(all(t["trigger_at"] == 1120 for t in self.tasks))
        takeoff.assert_not_called()

    def test_delayed_enrollment_preserves_later_maintenance_constraint(self):
        with patch.object(server, "_find_fleet_row", return_value=None), \
             patch.object(server, "_retrofit_blocks_takeoff", return_value=None), \
             patch.object(server, "_read_csv", return_value=[]), \
             patch.object(server, "_save_pending_tasks"), \
             patch.object(server, "_publish_log"):
            payload = {"fid": "1", "reg": "JET", "route_id": "2",
                       "trigger_at": 5120, "ready_at": 5000, "reason": "返场结束"}
            server._accept_takeoff_takeover(payload)
            server._accept_takeoff_takeover({
                **payload, "trigger_at": 1120, "ready_at": 1000,
                "reason": "全量扫描发现",
            })
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.tasks[0]["trigger_at"], 5120)
        self.assertEqual(self.tasks[0]["params"]["reason"], "返场结束")

    def test_invalid_enrollment_is_rejected_and_retrofit_still_blocks(self):
        payload = {"fid": "1", "reg": "JET", "route_id": "2"}
        for trigger in (0, -1, float("nan"), float("inf")):
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                server._accept_takeoff_takeover({**payload, "trigger_at": trigger})
        with patch.object(server, "_find_fleet_row", return_value=None), \
             patch.object(server, "_retrofit_blocks_takeoff", return_value="未完成"), \
             patch.object(server, "_publish_log"):
            server._accept_takeoff_takeover({**payload, "trigger_at": 1000})
        self.assertEqual(self.tasks, [])

    def test_persistence_bounds_history_without_losing_active_or_safety_tasks(self):
        protected = [
            {"id": "pending", "kind": "takeoff", "status": "pending"},
            {"id": "running", "kind": "takeoff", "status": "running"},
            {"id": "failed", "kind": "takeoff", "status": "failed"},
            {"id": "retrofit", "kind": "retrofit", "status": "failed"},
            {"id": "marketing", "kind": "marketing", "status": "pending"},
            {"id": "first", "kind": "takeoff", "status": "done",
             "params": {"reason": "改装完成"}},
        ]
        self.tasks.extend(protected)
        self.tasks.extend({"id": f"t{i}", "kind": "takeoff", "status": "done",
                           "completed_at": i} for i in range(26000))
        other = [{"kind": "takeoff", "status": "pending", "id": "other"}]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(server._tasks_by_account, {"other": other}), \
             patch.object(server, "_broadcast_sse"):
            path = Path(tmp) / "pending.json"
            server._save_pending_tasks(path, owner=self.owner)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 506)
        self.assertEqual(saved[-1]["id"], "t25999")
        self.assertEqual(saved[6]["id"], "t25500")
        self.assertTrue(all(self.tasks[i] is task for i, task in enumerate(protected)))
        self.assertEqual(other[0]["id"], "other")

    def test_operating_build_does_not_overwrite_refreshed_route_or_seats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {"fleet": root / "fleet.csv", "builds": root / "builds.csv",
                     "hubs": root / "hubs.json"}
            paths["hubs"].write_text(json.dumps([
                {"hub_id": "old", "name": "Cameroon, Yaounde"},
                {"hub_id": "new", "name": "China, Kunming"},
            ]), encoding="utf-8")
            stale = {"飞机ID": "22689157", "注册号": "A380-8-17",
                     "航班号": "MA-155", "起飞机场代码": "FKYS",
                     "起飞机场名称": "Yaounde", "到达机场代码": "UUEE",
                     "到达机场名称": "Moscow", "经济舱座位": "118",
                     "商务舱座位": "145", "头等舱座位": "64",
                     "枢纽分类": "Cameroon, Yaounde", "建设状态": "建设中"}
            live = {**stale, "起飞机场代码": "ZPPP", "起飞机场名称": "Kunming, China",
                    "到达机场代码": "OMDB", "到达机场名称": "Dubai, UAE",
                    "经济舱座位": "59", "商务舱座位": "125", "头等舱座位": "97"}
            build = {"reg": "A380-8-17", "fid": "22689157", "hub_id": "old",
                     "origin_airport_id": "822", "dest_airport_id": "3404",
                     "economy": "118", "business": "145", "first": "64"}
            airports = {"822": {"icao": "FKYS", "name": "Yaounde"},
                        "3404": {"icao": "UUEE", "name": "Moscow"}}
            with patch.object(server, "_paths", return_value=paths), \
                 patch.object(route_planner, "airport_by_id", side_effect=airports.get), \
                 patch.object(route_planner, "fetch_aircraft_fleet_row", return_value=live):
                server._write_fleet_csv([stale])
                server._save_builds([build])
                server._refresh_fleet_row("A380-8-17", "22689157", "old", finalize_build=True)
                for _ in range(2):
                    server._mark_build("A380-8-17", status="operating")
                row = server._read_csv(paths["fleet"])[0]
                self.assertEqual(row["起飞机场代码"], "ZPPP")
                self.assertEqual(row["到达机场代码"], "OMDB")
                self.assertEqual([row[k] for k in ("经济舱座位", "商务舱座位", "头等舱座位")],
                                 ["59", "125", "97"])
                self.assertEqual(row["枢纽分类"], "China, Kunming")
                self.assertEqual(row["航班号"], "MA-155")
                self.assertEqual(row["建设状态"], "")

    def test_operating_build_does_not_recreate_missing_aircraft(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "_paths", return_value={"fleet": Path(tmp) / "fleet.csv"}), \
             patch.object(server, "_write_fleet_csv") as write:
            server._merge_build_into_fleet({"reg": "SOLD", "fid": "1"}, building=False)
        write.assert_not_called()

    def test_single_aircraft_timestamp_uses_same_timezone_as_full_collection(self):
        expected = datetime(2026, 10, 22, 20, 0, tzinfo=collector.BJT)
        with patch.object(route_planner, "_do_curl", return_value="<html>detail</html>"), \
             patch.object(route_planner, "_load_airports", return_value=[]), \
             patch.object(route_planner, "_now_bjt", return_value=expected):
            row = route_planner.fetch_aircraft_fleet_row("1", "Hub", "JET")
        self.assertEqual(row["最后更新时间"], "2026-10-22 20:00:00")


if __name__ == "__main__":
    unittest.main()
