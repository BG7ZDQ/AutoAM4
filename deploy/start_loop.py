"""systemd ExecStartPost helper: start the regular AM4 loop after the dashboard is ready.

应用已启用面板登录，本机服务调用改用持久化服务令牌（src/.service_token），
不依赖浏览器会话与页面 CSRF。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:5000"
TOKEN_FILE = Path(__file__).resolve().parents[1] / "src" / ".service_token"


def main() -> None:
    last_error: Exception | None = None
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise SystemExit(f"无法读取服务令牌 {TOKEN_FILE}: {exc}") from exc
    if not token:
        raise SystemExit(f"服务令牌为空：{TOKEN_FILE}")
    for _ in range(30):
        try:
            request = Request(
                BASE + "/api/run",
                data=json.dumps({"mode": "loop_resume"}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Service-Token": token,
                },
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(result.get("msg") or "loop start was rejected")
            return
        except (OSError, URLError, ValueError, RuntimeError) as exc:
            last_error = exc
            time.sleep(1)
    raise SystemExit(f"AM4 loop did not start: {last_error}")


if __name__ == "__main__":
    main()
