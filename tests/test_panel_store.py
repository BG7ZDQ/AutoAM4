"""?????panel_store????????/??/??/???"""
from bootstrap import *


import panel_store as ps




class PanelStoreTests(unittest.TestCase):
    def test_register_approve_login_flow(self):
        uid = ps.create_user(
            "bob", "bob-pass-1", am4_email="bob@example.com",
            am4_password="game-pw", settings={"auto_takeoff": False},
        )
        user = ps.get_user_by_id(uid)
        self.assertEqual(user["status"], "pending")
        self.assertTrue(ps.verify_password(user, "bob-pass-1"))
        self.assertFalse(ps.verify_password(user, "wrong"))
        ps.set_user_status(uid, "active")
        self.assertEqual(ps.get_user_by_id(uid)["status"], "active")
        self.assertIsNotNone(ps.get_user_by_id(uid)["approved_at"])

    def test_settings_defaults_and_env_mapping(self):
        uid = ps.create_user("carol", "carol-pass-1")
        settings = ps.get_account(uid)["settings"]
        self.assertEqual(settings["cost_index"], 200)
        self.assertTrue(settings["auto_marketing"])
        env = ps.settings_to_env(settings)
        self.assertEqual(env["AM4_COST_INDEX"], "200")
        self.assertEqual(env["AM4_AUTO_MARKETING"], "1")
        ps.update_account(uid, settings={
            "fuel_buy_below": 600, "auto_buy_fuel": False,
        })
        updated = ps.get_account(uid)["settings"]
        self.assertEqual(updated["fuel_buy_below"], 600)
        self.assertFalse(updated["auto_buy_fuel"])
        self.assertEqual(updated["co2_buy_below"], 125)  # 默认值保留

    def test_duplicate_username_rejected(self):
        ps.create_user("dup", "dup-pass-1")
        with self.assertRaises(ValueError):
            ps.create_user("dup", "dup-pass-2")

    def test_duplicate_am4_email_rejected(self):
        ps.create_user("mailera", "mail-pass-1", am4_email="shared@example.com")
        with self.assertRaises(ValueError):
            ps.create_user("mailerb", "mail-pass-2", am4_email="SHARED@example.com ")

    def test_account_status_for_email_lookup(self):
        uid = ps.create_user(
            "statuse", "statuse-pass-1", am4_email="status@example.com")
        try:
            self.assertIsNone(ps.account_status_for_email("missing@example.com"))
            self.assertEqual(
                ps.account_status_for_email("STATUS@example.com "), "pending")
            ps.set_user_status(uid, "active")
            self.assertEqual(
                ps.account_status_for_email("status@example.com"), "active")
            ps.set_user_status(uid, "disabled")
            self.assertEqual(
                ps.account_status_for_email("status@example.com"), "disabled")
        finally:
            ps.delete_user(uid)

    def test_duplicate_email_race_reports_binding_conflict(self):
        # 预检查与插入之间被并发进程抢先：唯一索引触发 IntegrityError 后，
        # 必须按真实冲突来源报告“邮箱重复”，而不是吞掉或误报其他完整性错误。
        ps.create_user("raceda", "raced-pass-1", am4_email="race@example.com")

        class _EmptyResult:
            def fetchone(self):
                return None

        class _SkipFirstPrecheck:
            def __init__(self, real):
                self._real = real
                self._skipped = False

            def execute(self, sql, params=()):
                if (not self._skipped and isinstance(sql, str)
                        and "SELECT 1 FROM accounts" in sql):
                    self._skipped = True
                    return _EmptyResult()
                return self._real.execute(sql, params)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._real.__exit__(exc_type, exc, tb)

            def __getattr__(self, name):
                return getattr(self._real, name)

        real = ps._conn()
        try:
            with patch.object(ps, "_conn", return_value=_SkipFirstPrecheck(real)):
                with self.assertRaisesRegex(ValueError, "已绑定"):
                    ps.create_user(
                        "racedb", "raced-pass-2", am4_email="race@example.com")
        finally:
            real.close()
        # 失败路径整体回滚，不能留下半成品用户
        self.assertIsNone(ps.get_user_by_username("racedb"))

    def test_username_charset_restricted(self):
        # 超长、特殊字符拒绝
        with self.assertRaises(ValueError):
            ps.create_user("a" * 9, "pass-1234")
        with self.assertRaises(ValueError):
            ps.create_user("bob<script>", "pass-1234")
        # 头尾空格拒绝
        with self.assertRaises(ValueError):
            ps.create_user("  Bob", "pass-1234")
        with self.assertRaises(ValueError):
            ps.create_user("Bob  ", "pass-1234")
        with self.assertRaises(ValueError):
            ps.create_user("    ", "pass-1234")
        # 字母、数字、空格、下划线、斜杠允许
        ps.create_user("player1", "pass-1234")
        ps.create_user("Bob Mar", "pass-1234")
        ps.create_user("a/b_c", "pass-1234")

    def test_settings_coerce_bool_and_bounds(self):
        uid = ps.create_user("setter", "setter-pass-1")
        ps.update_account(uid, settings={
            "auto_buy_fuel": "false", "cost_index": 99999,
            "max_wear_for_takeoff": -5,
        })
        settings = ps.get_account(uid)["settings"]
        self.assertFalse(settings["auto_buy_fuel"])
        self.assertEqual(settings["cost_index"], 999)
        self.assertEqual(settings["max_wear_for_takeoff"], 0)

    def test_weak_input_rejected(self):
        with self.assertRaises(ValueError):
            ps.create_user("x", "pass")
        with self.assertRaises(ValueError):
            ps.create_user("ok", "123")

    def test_admin_flow(self):
        uid = ps.create_user("root", "root-pass-1", is_admin=True, status="active")
        self.assertTrue(ps.admin_exists())
        self.assertEqual(ps.get_user_by_id(uid)["is_admin"], 1)

    def test_list_users_joins_accounts(self):
        ps.create_user("dave", "dave-pass-1", am4_email="dave@example.com")
        rows = ps.list_users()
        dave = next(r for r in rows if r["username"] == "dave")
        self.assertEqual(dave["am4_email"], "dave@example.com")

    def test_delete_user_cascades(self):
        uid = ps.create_user("eve", "eve-pass-1", am4_email="eve@example.com")
        ps.delete_user(uid)
        self.assertIsNone(ps.get_user_by_id(uid))
        self.assertIsNone(ps.get_account(uid))

    def test_set_user_password(self):
        uid = ps.create_user("pwuser", "oldpass1", is_admin=True, status="active")
        ps.set_user_password(uid, "newpass1")
        self.assertTrue(ps.verify_password(ps.get_user_by_id(uid), "newpass1"))
        self.assertFalse(ps.verify_password(ps.get_user_by_id(uid), "oldpass1"))
        with self.assertRaises(ValueError):
            ps.set_user_password(uid, "123")


if __name__ == "__main__":
    unittest.main()
