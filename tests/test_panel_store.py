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
        ps.create_user("mailer1", "mail-pass-1", am4_email="shared@example.com")
        with self.assertRaises(ValueError):
            ps.create_user("mailer2", "mail-pass-2", am4_email="SHARED@example.com ")

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
