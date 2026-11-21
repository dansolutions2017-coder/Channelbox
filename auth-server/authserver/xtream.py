"""Checks a username/password against a server's Xtream API (player_api.php).

The server stays the source of truth: password, expiry, status and connection limit all come from it.
Standard library only (urllib), run in worker threads so the web server stays responsive.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Server

USER_AGENT = "Mozilla/5.0 (Linux; Android 12) AuthCheck/1.0"


@dataclass
class CheckResult:
    # ok | invalid | expired | banned | disabled | unreachable
    outcome: str
    server: Server
    expires_at: str | None = None          # ISO 8601 UTC, None = never expires
    max_connections: int | None = None
    active_connections: int | None = None
    detail: str | None = None

    @property
    def found(self) -> bool:
        """The password was right on this server (whatever the account's status)."""
        return self.outcome not in ("invalid", "unreachable")


class Unreachable(Exception):
    pass


def _fetch_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512 * 1024)
    except urllib.error.HTTPError as e:
        # Many servers answer 401/403 for wrong logins instead of auth=0.
        if e.code in (401, 403):
            return {"user_info": {"auth": 0}}
        raise Unreachable(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unreachable(str(getattr(e, "reason", e))) from e
    try:
        data = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise Unreachable("answer wasn't JSON (is this an Xtream address?)") from e
    # A wrong login sometimes comes back as [] or {"user_info": []}.
    if not isinstance(data, dict):
        return {"user_info": {"auth": 0}}
    return data


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def interpret(server: Server, data: dict) -> CheckResult:
    info = data.get("user_info")
    if not isinstance(info, dict) or _int(info.get("auth")) != 1:
        return CheckResult("invalid", server)
    exp = _int(info.get("exp_date"))
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None
    status = str(info.get("status") or "Active").strip().lower()
    result = CheckResult(
        "ok", server,
        expires_at=expires_at,
        max_connections=_int(info.get("max_connections")),
        active_connections=_int(info.get("active_cons")),
    )
    if status == "expired" or (exp and exp < datetime.now(tz=timezone.utc).timestamp()):
        result.outcome = "expired"
    elif status == "banned":
        result.outcome = "banned"
    elif status != "active":
        result.outcome = "disabled"
        result.detail = status
    return result


def check_sync(server: Server, username: str, password: str, timeout: float) -> CheckResult:
    query = urllib.parse.urlencode({"username": username, "password": password})
    try:
        data = _fetch_json(f"{server.url}/player_api.php?{query}", timeout)
    except Unreachable as e:
        return CheckResult("unreachable", server, detail=str(e))
    return interpret(server, data)


async def check(server: Server, username: str, password: str, timeout: float) -> CheckResult:
    return await asyncio.to_thread(check_sync, server, username, password, timeout)


async def check_many(servers: list[Server], username: str, password: str, timeout: float) -> list[CheckResult]:
    """All servers at once (so one slow server doesn't make the login slow). Same order as `servers`."""
    return list(await asyncio.gather(*(check(s, username, password, timeout) for s in servers)))
