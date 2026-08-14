"""??????????????"""
from bootstrap import *


class MarketingScheduleTests(unittest.TestCase):
    def test_relogin_must_reach_authenticated_homepage(self):
        with patch.object(
            collector, "_do_curl",
            side_effect=["login-page", "login-response", "login-page"],
        ), patch.object(collector, "_mark_account") as mark:
            with self.assertRaisesRegex(RuntimeError, "重新登录未成功"):
                collector._relogin()
        mark.assert_not_called()

    def test_marketing_purchase_can_reuse_known_inactive_page(self):
        active = "<tr><td><span class='glyphicons glyphicons-leaf'></span></td><td id='eTimer'></td></tr><script>timer('eTimer',43200);</script>"
        with patch.object(collector, "_ensure_login"), \
             patch.object(collector, "fetch", return_value=active) as fetch, \
             patch.object(collector, "_do_curl", return_value="ok"), \
             patch.object(collector.time, "sleep"):
            ok, _message, remaining = collector._purchase_marketing(
                "eco_12h", "环保营销（12 小时）", known_inactive=True)
        self.assertTrue(ok)
        self.assertEqual(remaining, 43200)
        self.assertEqual(fetch.call_count, 1)

    def test_marketing_purchase_never_writes_after_invalid_precheck(self):
        with patch.object(collector, "_ensure_login"), \
             patch.object(collector, "fetch", return_value=""), \
             patch.object(collector, "_do_curl") as write:
            ok, message, _remaining = collector._purchase_marketing(
                "eco_12h", "环保营销（12 小时）")
        self.assertFalse(ok)
        self.assertIn("未执行购买", message)
        write.assert_not_called()

    def test_purchase_failure_is_redetectable(self):
        self.assertIn("不足", collector._marketing_response_error(
            "<div class='alert'>餘額不足，無法購買</div>"
        ))
        self.assertEqual(collector._marketing_response_error(
            "<div class='alert alert-success'>Campaign started</div>"
        ), "")

    def test_active_marketing_remaining_time(self):
        page = """
        <tr><td><span class='glyphicons glyphicons-star'></span> 航空聲譽</td><td id='aTimer'></td></tr>
        <tr><td><span class='glyphicons glyphicons-leaf'></span> 環保</td><td id='eTimer'></td></tr>
        <script>timer('aTimer',18922);timer('eTimer',1200);</script>
        """
        self.assertEqual(
            collector._parse_active_marketing(page),
            {"airline": 18922, "eco": 1200},
        )


