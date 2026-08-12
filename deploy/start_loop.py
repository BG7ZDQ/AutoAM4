"""systemd ExecStartPost helper: start the regular AM4 loop after the dashboard is ready."""
from __future__ import annotations

import json
import re
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:5000"


def main() -> None:
    last_error: Exception | None = None
    for _ in range(30):
        try:
            with urlopen(BASE + "/", timeout=3) as response:
                page = response.read().decode("utf-8", errors="replace")
            match = re.search(r'let CSRF="([^"]+)"', page)
            if not match:
                raise RuntimeError("dashboard did not expose a CSRF token")
            request = Request(
                BASE + "/api/run",
                data=json.dumps({"mode": "loop"}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": match.group(1),
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
