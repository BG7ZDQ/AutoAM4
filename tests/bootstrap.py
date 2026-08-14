"""?????????? src ???????????????????

?????????????????????AM4_DISABLE_SCHEDULER=1
????????????????????????????
"""
import csv
import json
import os
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_TEST_OUTPUTS = tempfile.TemporaryDirectory()
os.environ["AM4_OUTPUTS_DIR"] = _TEST_OUTPUTS.name
os.environ["AM4_DISABLE_SCHEDULER"] = "1"
os.environ["AM4_EMAIL"] = "tests@example.invalid"
os.environ["AM4_PASSWORD"] = "test-password"
_TEST_DB_DIR = tempfile.mkdtemp()
os.environ["AM4_PANEL_DB"] = os.path.join(_TEST_DB_DIR, "panel.db")

import account_storage
import auto_buy
import collector
import fresh_demand
import panel_store
import route_planner
import server
import storage_utils

# ?????????????????????????????????
_TEST_ADMIN_ID = panel_store.create_user(
    "tadmin", "test-pass-1", is_admin=True, status="active",
    am4_email="tests@example.invalid", am4_password="test-password",
)
server._effective_user = lambda: panel_store.get_user_by_id(_TEST_ADMIN_ID)
