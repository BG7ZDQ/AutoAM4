"""?????00/30 ???29/59 ????? 06:00 ??????"""
from bootstrap import *


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


