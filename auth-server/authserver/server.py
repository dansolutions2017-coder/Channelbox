"""HTTP API.

POST /v1/login   – check a login against your servers, answer which server the app should use
GET  /v1/config  – app settings you control (minimum version, update link, notice)
GET  /healthz    – for uptime checks
/admin           – web page to manage servers, blocks and see logins (needs ADMIN_PASSWORD)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import xtream
from .config import ServerList, Settings, load_app_config
from .ratelimit import SlidingWindow
from .store import Store

log = logging.getLogger("authserver")

# User-facing messages: the app shows `message` as-is.
MESSAGES = {
    "invalid_credentials": "Wrong username or password.",
    "expired": "Your subscription has expired. Please contact us to renew.",
    "banned": "This account has been suspended. Please contact us.",
    "disabled": "This account is not active. Please contact us.",
    "blocked": "This account has been suspended. Please contact us.",
    "too_many_devices": "This account is already used on the maximum number of devices.",
    "rate_limited": "Too many attempts. Please wait a few minutes and try again.",
    "server_unreachable": "Our server isn't answering right now. Please try again in a few minutes.",
    "bad_request": "Please enter your username and password.",
}

STATUS = {
    "invalid_credentials": 401,
    "expired": 403, "banned": 403, "disabled": 403, "blocked": 403, "too_many_devices": 403,
    "rate_limited": 429,
    "server_unreachable": 503,
    "bad_request": 400,
}

_SAFE = re.compile(r"^[\x21-\x7e]{1,128}$")  # printable ASCII, no spaces


def error(code: str, extra: dict | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"ok": False, "error": code, "message": MESSAGES[code]}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=STATUS[code], headers=headers)


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


async def find_account(servers: ServerList, store: Store, username: str, password: str, timeout: float):
    """Which server has this login? Returns (result or None, unreachable_count, remembered_was_down).

    1. The server this login worked on last time is asked first (fast path, one request).
    2. Otherwise every other enabled server is asked at the same time; the first one in your list
       order that knows the login wins.
    """
    enabled = servers.enabled()
    remembered_id = store.last_server(username)
    remembered = next((s for s in enabled if s.id == remembered_id), None)
    unreachable = 0
    remembered_down = False

    if remembered:
        r = await xtream.check(remembered, username, password, timeout)
        if r.found:
            return r, 0, False
        if r.outcome == "unreachable":
            unreachable += 1
            remembered_down = True

    others = [s for s in enabled if s is not remembered]
    if others:
        results = await xtream.check_many(others, username, password, timeout)
        for r in results:
            if r.outcome == "unreachable":
                unreachable += 1
                log.warning("server %s unreachable: %s", r.server.name, r.detail)
        hit = next((r for r in results if r.found), None)
        if hit:
            return hit, unreachable, remembered_down
    return None, unreachable, remembered_down


def create_app(settings: Settings | None = None, store: Store | None = None) -> Starlette:
    settings = settings or Settings()
    store = store or Store(settings.db_path)
    servers = ServerList(settings.servers_file)
    ip_limiter = SlidingWindow(settings.ip_limit, 300)
    user_fail_limiter = SlidingWindow(settings.user_fail_limit, 900)

    async def login(request: Request) -> JSONResponse:
        ip = client_ip(request, settings)

        wait = ip_limiter.retry_after(ip)
        if wait:
            return error("rate_limited", {"retry_after": wait}, {"Retry-After": str(wait)})
        ip_limiter.hit(ip)

        try:
            body = await request.json()
        except Exception:
            return error("bad_request")
        if not isinstance(body, dict):
            return error("bad_request")
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        device_id = str(body.get("device_id") or "").strip()[:128]
        device_name = str(body.get("device_name") or "").strip()[:128] or None
        app_version = str(body.get("app_version") or "").strip()[:32] or None
        if not _SAFE.match(username) or not (1 <= len(password) <= 128):
            return error("bad_request")

        user_key = username.lower()
        wait = user_fail_limiter.retry_after(user_key)
        if wait:
            store.log(username, ip, "rate_limited")
            return error("rate_limited", {"retry_after": wait}, {"Retry-After": str(wait)})

        if store.block_reason(username) is not None:
            store.log(username, ip, "blocked")
            return error("blocked")

        enabled = servers.enabled()
        if not enabled:
            store.log(username, ip, "no_servers")
            return error("server_unreachable")

        result, unreachable, remembered_down = await find_account(
            servers, store, username, password, settings.server_timeout)

        if result is None:
            if unreachable == len(enabled) or remembered_down:
                # Their server (or every server) didn't answer – we couldn't actually check.
                store.log(username, ip, "unreachable")
                return error("server_unreachable")
            if unreachable:
                # Not found on the servers that answered; one may be down. Don't count a strike.
                store.log(username, ip, "invalid_some_down")
                return error("invalid_credentials")
            user_fail_limiter.hit(user_key)
            store.log(username, ip, "invalid")
            return error("invalid_credentials")

        store.remember_server(username, result.server.id)

        if result.outcome != "ok":
            store.log(username, ip, result.outcome, result.server.name)
            return error(result.outcome, {"expires_at": result.expires_at})

        user_fail_limiter.reset(user_key)

        if device_id:
            if settings.max_devices > 0:
                recent = store.devices(username, since=int(time.time()) - 30 * 86400)
                ids = {d.device_id for d in recent}
                if device_id not in ids and len(ids) >= settings.max_devices:
                    store.log(username, ip, "too_many_devices", result.server.name)
                    return error("too_many_devices")
            store.touch_device(username, device_id, device_name, app_version, ip)

        store.log(username, ip, "ok", result.server.name)
        return JSONResponse({
            "ok": True,
            "server": result.server.url,
            "username": username,
            "expires_at": result.expires_at,
            "max_connections": result.max_connections,
            "checked_at": int(time.time()),
            "config": load_app_config(settings.app_config_file),
        }, headers={"Cache-Control": "no-store"})

    async def app_config(request: Request) -> JSONResponse:
        return JSONResponse(load_app_config(settings.app_config_file), headers={"Cache-Control": "max-age=300"})

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "servers": len(servers.enabled())})

    async def housekeeping() -> None:
        while True:
            await asyncio.sleep(3600)
            ip_limiter.sweep()
            user_fail_limiter.sweep()
            store.prune_log(90)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(housekeeping())
        log.info("auth server ready: %d servers, max_devices=%s, admin page %s",
                 len(servers.enabled()), settings.max_devices, "on" if settings.admin_password else "off")
        yield
        task.cancel()

    routes = [
        Route("/v1/login", login, methods=["POST"]),
        Route("/v1/config", app_config, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
    if settings.admin_password:
        from .admin import admin_routes
        routes += admin_routes(settings, store, servers)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    app.state.servers = servers
    return app


def main() -> None:
    import os
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        workers=1,
        proxy_headers=False,      # we read X-Forwarded-For ourselves (TRUST_PROXY)
        access_log=False,
    )


if __name__ == "__main__":
    main()
