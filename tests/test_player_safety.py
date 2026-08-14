"""?????A-Check/???/????/????????"""
from bootstrap import *


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


