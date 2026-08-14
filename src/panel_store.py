"""面板用户与账号存储（SQLite，零新依赖）。

一个网站账户（登录）绑定一个唯一的 AM4 游戏账号 + 一组运行设置。
注册后状态为 pending，由管理员审核通过（active）后才能登录。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("AM4_PANEL_DB", str(ROOT / "data" / "panel.db")))

# 每个账号的 env 设置默认值（与 .env.example 对应）
DEFAULT_SETTINGS: dict = {
    "cost_index": 200,
    "min_fuel": 200000,
    "cash_reserve": 5000000,
    "max_resource_spend": 25000000,
    "fuel_buy_below": 500,
    "co2_buy_below": 125,
    "min_a_check_hours": 5,
    "max_wear_for_takeoff": 80,
    # 操作开关
    "auto_marketing": True,
    "auto_buy_fuel": True,
    "auto_buy_co2": True,
    "auto_takeoff": True,
}

_SETTING_ENV_MAP: dict[str, str] = {
    "cost_index": "AM4_COST_INDEX",
    "min_fuel": "AM4_MIN_FUEL",
    "cash_reserve": "AM4_CASH_RESERVE",
    "max_resource_spend": "AM4_MAX_RESOURCE_SPEND",
    "fuel_buy_below": "AM4_FUEL_BUY_BELOW",
    "co2_buy_below": "AM4_CO2_BUY_BELOW",
    "min_a_check_hours": "AM4_MIN_A_CHECK_HOURS",
    "max_wear_for_takeoff": "AM4_MAX_WEAR_FOR_TAKEOFF",
}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9 _/]{2,8}$")

_AUTO_ENV_MAP: dict[str, str] = {
    "auto_marketing": "AM4_AUTO_MARKETING",
    "auto_buy_fuel": "AM4_AUTO_BUY_FUEL",
    "auto_buy_co2": "AM4_AUTO_BUY_CO2",
    "auto_takeoff": "AM4_AUTO_TAKEOFF",
}

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    approved_at REAL
);
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    am4_email TEXT NOT NULL DEFAULT '',
    am4_password TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
"""


def normalize_email(email: str) -> str:
    """游戏账号邮箱归一化：去空白 + 大小写折叠，用于唯一绑定判断。"""
    return (email or "").strip().casefold()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表（幂等），并记录 schema 版本。"""
    with _lock, _conn() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        # 一个网站账户只能绑定一个唯一的 AM4 游戏账号。
        # 表达式唯一索引兼容旧库；若旧数据里已有重复绑定，跳过建索引并告警，
        # 绝不静默合并或覆盖任何一方的数据。
        dup = conn.execute(
            "SELECT lower(trim(am4_email)) FROM accounts WHERE am4_email != '' "
            "GROUP BY lower(trim(am4_email)) HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if dup is None:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email_norm "
                "ON accounts(lower(trim(am4_email))) WHERE am4_email != ''"
            )
        else:
            print(
                f"⚠ panel_store: 检测到重复绑定的游戏账号（{dup[0]}），"
                "已跳过唯一索引迁移，请人工去重后重启", flush=True)
    _harden_perms()


def _harden_perms() -> None:
    """Linux 上收紧数据库目录/文件权限：账号库含 AM4 明文密码。"""
    if os.name == "nt":
        return
    try:
        DB_PATH.parent.chmod(0o700)
    except Exception:
        pass
    try:
        DB_PATH.chmod(0o600)
    except Exception:
        pass


def _row_to_user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def admin_exists() -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE is_admin = 1 AND status = 'active' LIMIT 1"
        ).fetchone()
    return row is not None


def _account_binding_exists(conn: sqlite3.Connection, email_norm: str) -> bool:
    """是否存在绑定该游戏邮箱的账号记录（查询与插入之间竞态的兜底判定）。"""
    row = conn.execute(
        "SELECT 1 FROM accounts WHERE lower(trim(am4_email)) = ? LIMIT 1",
        (email_norm,),
    ).fetchone()
    return row is not None


def create_user(
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    status: str = "pending",
    am4_email: str = "",
    am4_password: str = "",
    settings: dict | None = None,
) -> int:
    """创建用户及其 1:1 绑定的 AM4 账号记录，返回用户 id。"""
    raw_username = username or ""
    if raw_username != raw_username.strip():
        raise ValueError("用户名头尾不能有空格")
    username = raw_username.strip()
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("用户名仅允许 2~8 位英文字母或数字、空格、下划线或斜杠")
    if len(password) < 6:
        raise ValueError("密码至少 6 个字符")
    now = time.time()
    normalized = normalize_settings(settings or {})
    with _lock, _conn() as conn:
        email_norm = normalize_email(am4_email)
        if email_norm and _account_binding_exists(conn, email_norm):
            raise ValueError("该游戏账号已绑定其他网站账户")
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, status, created_at, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    username,
                    generate_password_hash(password, method="pbkdf2:sha256"),
                    1 if is_admin else 0,
                    status,
                    now,
                    now if status == "active" else None,
                ),
            )
            uid = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在") from None
        try:
            conn.execute(
                "INSERT INTO accounts (user_id, am4_email, am4_password, settings, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, am4_email or "", am4_password or "",
                 json.dumps(normalized, ensure_ascii=False), now),
            )
        except sqlite3.IntegrityError:
            # 多进程并发时，唯一索引可能先于预检查命中；确认确有同邮箱绑定才按
            # “邮箱重复”报告，其他完整性冲突原样上抛，避免掩盖真实错误。
            if email_norm and _account_binding_exists(conn, email_norm):
                raise ValueError("该游戏账号已绑定其他网站账户") from None
            raise
    return uid


def get_user_by_username(username: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_user(row)


def get_user_by_id(uid: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return _row_to_user(row)


def verify_password(user: dict, password: str) -> bool:
    try:
        return check_password_hash(user.get("password_hash", ""), password or "")
    except Exception:
        return False


def set_user_status(uid: int, status: str) -> None:
    if status not in ("active", "pending", "disabled"):
        raise ValueError(f"非法状态: {status}")
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE users SET status = ?, approved_at = ? WHERE id = ?",
            (status, time.time() if status == "active" else None, uid),
        )


def set_user_password(uid: int, password: str) -> None:
    """重置用户登录密码（管理员入口），不触碰绑定账号的 AM4 凭据。"""
    if len(password or "") < 6:
        raise ValueError("密码至少 6 个字符")
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password, method="pbkdf2:sha256"), uid),
        )
        if cur.rowcount == 0:
            raise ValueError("用户不存在")


def delete_user(uid: int) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))


def normalize_settings(raw: dict) -> dict:
    """清洗设置：保留已知键、布尔开关转 bool、数值键转 int。"""
    out = dict(DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key.startswith("auto_"):
            out[key] = _coerce_bool(value)
            continue
        parsed = _coerce_int(value)
        if parsed is None:
            continue
        lo, hi = _SETTING_BOUNDS.get(key, (None, None))
        if lo is not None:
            parsed = max(lo, parsed)
        if hi is not None:
            parsed = min(hi, parsed)
        out[key] = int(parsed)
    return out


_SETTING_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "cost_index": (0, 999),
    "min_fuel": (0, 10 ** 15),
    "cash_reserve": (0, 10 ** 15),
    "max_resource_spend": (0, 10 ** 15),
    "fuel_buy_below": (0, 10 ** 9),
    "co2_buy_below": (0, 10 ** 9),
    "min_a_check_hours": (0, 24 * 365),
    "max_wear_for_takeoff": (0, 100),
}


def _coerce_bool(value) -> bool:
    """布尔开关解析：布尔直接通过；数字按非零；字符串按常见真值词表。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _coerce_int(value) -> int | None:
    """数值解析：接受带千分位逗号的数字字符串；解析失败返回 None。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def get_account(user_id: int) -> dict | None:
    """返回账号记录，settings 与默认值合并；无账号时返回 None。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        settings = json.loads(data.get("settings") or "{}")
    except json.JSONDecodeError:
        settings = {}
    data["settings"] = normalize_settings(settings)
    return data


def update_account(
    user_id: int,
    *,
    am4_email: str | None = None,
    am4_password: str | None = None,
    settings: dict | None = None,
) -> None:
    current = get_account(user_id)
    new_settings = normalize_settings(settings if settings is not None
                                      else (current or {}).get("settings", {}))
    email_norm = normalize_email(am4_email) if am4_email is not None else None
    with _lock, _conn() as conn:
        if email_norm:
            dup = conn.execute(
                "SELECT 1 FROM accounts WHERE lower(trim(am4_email)) = ? "
                "AND user_id != ? LIMIT 1",
                (email_norm, user_id),
            ).fetchone()
            if dup is not None:
                raise ValueError("该游戏账号已绑定其他网站账户")
        if current is None:
            conn.execute(
                "INSERT INTO accounts (user_id, am4_email, am4_password, settings, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, am4_email or "", am4_password or "",
                 json.dumps(new_settings, ensure_ascii=False), time.time()),
            )
        else:
            conn.execute(
                "UPDATE accounts SET am4_email = ?, am4_password = ?, settings = ?, updated_at = ? "
                "WHERE user_id = ?",
                (
                    am4_email if am4_email is not None else current["am4_email"],
                    am4_password if am4_password is not None else current["am4_password"],
                    json.dumps(new_settings, ensure_ascii=False),
                    time.time(),
                    user_id,
                ),
            )


def list_users() -> list[dict]:
    """管理员视图：用户 + 绑定账号摘要。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.is_admin, u.status, u.created_at, u.approved_at, "
            "a.am4_email, a.updated_at AS account_updated_at "
            "FROM users u LEFT JOIN accounts a ON a.user_id = u.id "
            "ORDER BY u.id"
        ).fetchall()
    return [dict(r) for r in rows]


def account_status_for_email(email: str) -> str | None:
    """按游戏邮箱查询绑定网站账户的状态；未绑定返回 None。

    历史数据若存在重复绑定，非 active 状态优先返回，保证“停用即保护”不遗漏。
    """
    email_norm = normalize_email(email)
    if not email_norm:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.status FROM users u "
            "JOIN accounts a ON a.user_id = u.id "
            "WHERE lower(trim(a.am4_email)) = ? "
            "ORDER BY CASE WHEN u.status = 'active' THEN 1 ELSE 0 END "
            "LIMIT 1",
            (email_norm,),
        ).fetchone()
    return str(row["status"]) if row else None


def settings_to_env(settings: dict) -> dict[str, str]:
    """把账号设置转成采集进程使用的环境变量。"""
    normalized = normalize_settings(settings)
    env = {
        _SETTING_ENV_MAP[k]: str(normalized[k])
        for k in _SETTING_ENV_MAP
    }
    env.update({
        _AUTO_ENV_MAP[k]: ("1" if normalized[k] else "0")
        for k in _AUTO_ENV_MAP
    })
    return env


init_db()
