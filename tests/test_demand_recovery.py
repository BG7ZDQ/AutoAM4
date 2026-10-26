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


    def test_daily_insufficiency_has_at_most_two_business_retries(self):
        for states, expected_calls, expected in [
            (["旺盛"], 1, "旺盛"), (["不足", "旺盛"], 2, "旺盛"),
            (["不足", "不足", "旺盛"], 3, "旺盛"),
            (["不足", "不足", "不足"], 3, "不足"),
            (["不足", "", "不足"], 3, ""), ([""], 1, "")]:
            with self.subTest(states=states), \
                 patch.object(collector, "fetch", return_value="page") as fetch, \
                 patch.object(collector, "parse_aircraft_detail", side_effect=[
                     {"demand_status": state, "demand_business": str(i)} for i, state in enumerate(states)]):
                result = collector.fetch_detail_with_demand_retries("detail-url", referer="fleet", label="TEST")
                self.assertEqual(fetch.call_count, expected_calls)
                self.assertEqual(result["demand_status"], expected)
                if expected == "旺盛":
                    self.assertEqual(result["demand_business"], str(expected_calls-1))
                self.assertTrue(all(c.args[0] == "detail-url" for c in fetch.call_args_list))

    def test_empty_initial_response_does_not_overwrite_fleet(self):
        with patch.object(collector, "fetch", return_value="") as fetch:
            self.assertIsNone(collector.fetch_detail_with_demand_retries("url", referer="fleet", label="TEST"))
            fetch.assert_called_once()
