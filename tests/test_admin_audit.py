"""管理员审计日志：写入、轮转备份与 30 天清理。"""
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import *


class AdminAuditTests(unittest.TestCase):
    def test_audit_writes_line_with_actor(self):
        tmp = Path(tempfile.mkdtemp()) / "audit.log"
        with patch.object(server, "_AUDIT_LOG", tmp), \
             patch.object(server, "_real_user",
                          return_value={"username": "admin"}), \
             patch.object(server, "_now_bjt",
                          return_value=datetime(2026, 8, 15, 10, 0, 0,
                                                tzinfo=server.BJT)):
            server._audit("重置密码", target="bob", result="ok")
        text = tmp.read_text(encoding="utf-8")
        self.assertIn("2026-08-15 10:00:00", text)
        self.assertIn("admin", text)
        self.assertIn("重置密码", text)
        self.assertIn("bob", text)

    def test_audit_actor_env_without_session(self):
        tmp = Path(tempfile.mkdtemp()) / "audit.log"
        with patch.object(server, "_AUDIT_LOG", tmp):
            server._audit("创建管理员", target="root")
        self.assertIn("env", tmp.read_text(encoding="utf-8"))

    def test_rotate_and_cleanup_old_logs(self):
        tmp = Path(tempfile.mkdtemp())
        audit = tmp / "admin_audit.log"
        audit.write_text("旧内容\n", encoding="utf-8")
        old = tmp / "admin_audit_20260701_000000.log.bak"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (time.time() - 31 * 86400, time.time() - 31 * 86400))
        recent = tmp / "admin_audit_20260814_000000.log.bak"
        recent.write_text("x", encoding="utf-8")

        out_root = tmp / "outputs"
        acct = out_root / "acct"
        acct.mkdir(parents=True)
        old_run = acct / "run_log_20260701_000000.txt.bak"
        old_run.write_text("x", encoding="utf-8")
        os.utime(old_run, (time.time() - 31 * 86400, time.time() - 31 * 86400))
        recent_run = acct / "run_log_20260814_000000.txt.bak"
        recent_run.write_text("x", encoding="utf-8")

        with patch.object(server, "_AUDIT_LOG", audit), \
             patch.object(server, "OUTPUTS_ROOT", out_root):
            server._rotate_audit_log()
            server._cleanup_old_logs()

        self.assertFalse(audit.exists())
        self.assertEqual(len(list(tmp.glob("admin_audit_*.log.bak"))), 2)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertFalse(old_run.exists())
        self.assertTrue(recent_run.exists())

    def test_admin_apis_write_audit(self):
        tmp = Path(tempfile.mkdtemp()) / "audit.log"
        uid = panel_store.create_user(
            "auditbob", "bob-pass-1",
            am4_email="auditbob@example.com", am4_password="p")
        panel_store.set_user_status(uid, "active")
        with patch.object(server, "_AUDIT_LOG", tmp):
            client = server.app.test_client()
            client.post("/api/login",
                        json={"username": "testadmin", "password": "test-pass-1"},
                        headers={"X-CSRF-Token": server._csrf_token})
            client.post("/api/admin/users/%d/password" % uid,
                        json={"password": "newpass-1"},
                        headers={"X-CSRF-Token": server._csrf_token})
            client.post("/api/admin/impersonate", json={"user_id": uid},
                        headers={"X-CSRF-Token": server._csrf_token})
        text = tmp.read_text(encoding="utf-8")
        self.assertIn("登录", text)
        self.assertIn("重置密码", text)
        self.assertIn("模拟进入", text)
        self.assertIn("auditbob", text)


if __name__ == "__main__":
    unittest.main()
