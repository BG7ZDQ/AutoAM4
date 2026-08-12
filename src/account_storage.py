"""账号级输出目录命名与旧目录迁移。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def normalize_account(email: str) -> str:
    return (email or "unknown").strip().casefold()


def legacy_account_key(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", normalize_account(email)).strip("_") or "unknown"


def account_key(email: str) -> str:
    """可读前缀 + 邮箱稳定哈希，避免清洗后的目录名发生碰撞。"""
    normalized = normalize_account(email)
    prefix = legacy_account_key(normalized)[:48]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def account_output_dir(outputs_root: Path, email: str, *, migrate_legacy: bool = True) -> Path:
    """返回哈希目录；首次升级时把当前账号的旧目录内容迁入。"""
    target = outputs_root / account_key(email)
    legacy = outputs_root / legacy_account_key(email)
    outputs_root.mkdir(parents=True, exist_ok=True)
    if migrate_legacy and legacy != target and legacy.is_dir():
        if not target.exists():
            # 同一卷内目录重命名是原子的，正常升级不产生半迁移状态。
            legacy.replace(target)
            return target
        # 上次迁移若中途失败，逐项幂等续传剩余文件；冲突时停止，绝不静默混用。
        for item in list(legacy.iterdir()):
            destination = target / item.name
            if destination.exists():
                if (item.is_file() and destination.is_file()
                        and item.read_bytes() == destination.read_bytes()):
                    item.unlink()
                    continue
                raise FileExistsError(f"账号目录迁移冲突: {item.name}")
            item.replace(destination)
        try:
            legacy.rmdir()
        except OSError:
            pass
    target.mkdir(parents=True, exist_ok=True)
    return target
