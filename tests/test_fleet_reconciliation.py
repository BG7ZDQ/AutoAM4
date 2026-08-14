"""????????????????????"""
from bootstrap import *


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


