"""Admin web page at /admin: servers (add, edit, reorder, switch off, delete), test a login,
look up a customer, block/unblock, recent logins.

Signed in with ADMIN_PASSWORD. Session cookie is HttpOnly + Secure + SameSite=Strict and every form
carries a CSRF token. Plain HTML, no JavaScript needed.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import time
import urllib.parse
from datetime import datetime

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import xtream
from .config import ServerList, Settings, valid_url, normalize_url
from .ratelimit import SlidingWindow
from .store import Store

COOKIE = "auth_admin"
SESSION_HOURS = 12
e = html.escape


def fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "–"


CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f141a;color:#e8edf2}
main{max-width:1100px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 12px;color:#9fb0c2;text-transform:uppercase;letter-spacing:.05em}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
section{background:#161d26;border:1px solid #243041;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #243041;vertical-align:middle}
th{color:#8394a8;font-weight:600;font-size:12px}tr:last-child td{border-bottom:0}
input[type=text],input[type=password],input[type=url]{background:#0f141a;border:1px solid #2c3a4d;color:#e8edf2;border-radius:8px;padding:7px 9px;font:inherit;width:100%}
button{background:#2c3a4d;color:#e8edf2;border:0;border-radius:8px;padding:7px 11px;font:inherit;cursor:pointer;white-space:nowrap}
button:hover{background:#38485f}button.primary{background:#3d7bfd}button.primary:hover{background:#5590ff}button.danger{background:#5a2330}button.danger:hover{background:#7a2c3e}
form.inline{display:inline}.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}.row>button{flex:0}
.msg{background:#1d3a2a;border:1px solid #2d6b47;padding:10px 12px;border-radius:8px;margin-bottom:16px}
.err{background:#3a1d24;border-color:#6b2d3c}.muted{color:#8394a8}.off{opacity:.5}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#243041}
.ok{background:#1d4a32;color:#8ee0b0}.bad{background:#4a1d27;color:#f0a0b0}.warn{background:#4a3d1d;color:#f0d890}
code{background:#0f141a;padding:1px 5px;border-radius:5px;font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}@media(max-width:800px){.grid{grid-template-columns:1fr}}
"""

OUTCOME_PILL = {"ok": "ok", "invalid": "bad", "unreachable": "warn", "expired": "warn",
                "banned": "bad", "disabled": "bad", "blocked": "bad", "too_many_devices": "warn",
                "invalid_some_down": "bad", "rate_limited": "warn", "no_servers": "warn"}


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content=noindex><title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
    )


def admin_routes(settings: Settings, store: Store, servers: ServerList) -> list[Route]:
    secret = secrets.token_bytes(32)          # sessions end when the server restarts
    login_limiter = SlidingWindow(10, 900)
    pw_hash = hashlib.sha256(settings.admin_password.encode()).digest()

    def sign(value: str) -> str:
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    def new_session() -> str:
        expires = int(time.time()) + SESSION_HOURS * 3600
        nonce = secrets.token_hex(8)
        return f"{expires}.{nonce}.{sign(f'{expires}.{nonce}')}"

    def session_ok(request: Request) -> str | None:
        raw = request.cookies.get(COOKIE, "")
        parts = raw.split(".")
        if len(parts) != 3:
            return None
        expires, nonce, sig = parts
        if not hmac.compare_digest(sig, sign(f"{expires}.{nonce}")):
            return None
        if not expires.isdigit() or int(expires) < time.time():
            return None
        return raw

    def csrf_for(session: str) -> str:
        return sign("csrf:" + session)[:32]

    async def form(request: Request) -> dict[str, str]:
        body = (await request.body())[:64 * 1024].decode("utf-8", errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}

    def back(msg: str = "", err: bool = False) -> RedirectResponse:
        q = urllib.parse.urlencode({"err" if err else "msg": msg}) if msg else ""
        return RedirectResponse(f"/admin{'?' + q if q else ''}", status_code=303)

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for") if settings.trust_proxy else None
        return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")

    # ---- sign in / out -------------------------------------------------------------------

    def login_page(error: str = "") -> HTMLResponse:
        err = f"<div class='msg err'>{e(error)}</div>" if error else ""
        return page("Sign in", f"""
<section style="max-width:360px;margin:80px auto"><h1 style="margin-bottom:14px">Login server admin</h1>{err}
<form method=post action="/admin/login"><input type=password name=password placeholder="Admin password" autofocus required>
<div style="margin-top:10px"><button class=primary style="width:100%">Sign in</button></div></form></section>""")

    async def login_get(request: Request) -> Response:
        if session_ok(request):
            return back()
        return login_page()

    async def login_post(request: Request) -> Response:
        ip = client_ip(request)
        wait = login_limiter.retry_after(ip)
        if wait:
            return login_page(f"Too many attempts. Try again in {wait // 60 + 1} minutes.")
        f = await form(request)
        given = hashlib.sha256(f.get("password", "").encode()).digest()
        if not hmac.compare_digest(given, pw_hash):
            login_limiter.hit(ip)
            return login_page("Wrong password.")
        login_limiter.reset(ip)
        resp = back()
        resp.set_cookie(COOKIE, new_session(), max_age=SESSION_HOURS * 3600, httponly=True,
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                        samesite="strict", path="/admin")
        return resp

    async def logout(request: Request) -> Response:
        resp = RedirectResponse("/admin/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/admin")
        return resp

    # ---- guard for every page below ----------------------------------------------------------

    def guarded(handler, post: bool = True):
        async def wrapper(request: Request) -> Response:
            session = session_ok(request)
            if not session:
                return RedirectResponse("/admin/login", status_code=303)
            f: dict[str, str] = {}
            if post:
                f = await form(request)
                if not hmac.compare_digest(f.get("csrf", ""), csrf_for(session)):
                    return back("The page had expired – please try again.", err=True)
            return await handler(request, f, session)
        return wrapper

    # ---- dashboard ---------------------------------------------------------------------------

    def render(session: str, request: Request, extra_top: str = "", lookup: str = "") -> HTMLResponse:
        csrf = f"<input type=hidden name=csrf value='{csrf_for(session)}'>"
        msg = request.query_params.get("msg")
        err = request.query_params.get("err")
        notice = (f"<div class=msg>{e(msg)}</div>" if msg else "") + (f"<div class='msg err'>{e(err)}</div>" if err else "")

        all_servers = servers.all()
        rows = []
        for i, s in enumerate(all_servers):
            rows.append(f"""<tr class="{'' if s.enabled else 'off'}">
<td class=muted>{i + 1}</td>
<td><form method=post action="/admin/servers/{e(s.id)}/edit" class=row>{csrf}
  <input type=text name=name value="{e(s.name)}" style="max-width:160px"><input type=text name=url value="{e(s.url)}"><button>Save</button></form></td>
<td>{'<span class="pill ok">On</span>' if s.enabled else '<span class=pill>Off</span>'}</td>
<td style="white-space:nowrap">
 <form method=post action="/admin/servers/{e(s.id)}/up" class=inline>{csrf}<button title="Try earlier">▲</button></form>
 <form method=post action="/admin/servers/{e(s.id)}/down" class=inline>{csrf}<button title="Try later">▼</button></form>
 <form method=post action="/admin/servers/{e(s.id)}/toggle" class=inline>{csrf}<button>{'Turn off' if s.enabled else 'Turn on'}</button></form>
 <form method=post action="/admin/servers/{e(s.id)}/delete" class=inline onsubmit="return confirm('Delete {e(s.name)}?')">{csrf}<button class=danger>Delete</button></form>
</td></tr>""")
        servers_table = (
            "<table><tr><th>#</th><th>Name and Xtream address</th><th>Status</th><th></th></tr>" + "".join(rows) + "</table>"
            if rows else "<p class=muted>No servers yet. Add the first one below. Until then, nobody can log in.</p>"
        )

        blocked_rows = "".join(
            f"<tr><td><code>{e(u)}</code></td><td>{e(r or '')}</td><td class=muted>{fmt_ts(t)}</td>"
            f"<td><form method=post action='/admin/unblock' class=inline>{csrf}<input type=hidden name=username value='{e(u)}'><button>Unblock</button></form></td></tr>"
            for u, r, t in store.blocked()
        ) or "<tr><td colspan=4 class=muted>Nobody is blocked.</td></tr>"

        log_rows = "".join(
            f"<tr><td class=muted>{fmt_ts(ts)}</td><td><a href='/admin?user={e(urllib.parse.quote(u or ''))}' style='color:inherit'><code>{e(u or '')}</code></a></td>"
            f"<td><span class='pill {OUTCOME_PILL.get(res, '')}'>{e(res)}</span></td><td>{e(srv or '')}</td><td class=muted>{e(ip or '')}</td></tr>"
            for ts, u, ip, res, srv in store.recent_log(None, 40)
        ) or "<tr><td colspan=5 class=muted>No logins yet.</td></tr>"

        return page("Login server", f"""
<header><h1>Login server</h1><form method=post action="/admin/logout">{csrf}<button>Sign out</button></form></header>
{notice}{extra_top}
<section><h2>Servers</h2>
<p class=muted style="margin-top:-6px">Customers type only their username and password. Each login is checked against these servers.
The server it worked on last time is asked first, then all the others at once. If a login exists on more than one, the higher one in this list wins.</p>
{servers_table}
<form method=post action="/admin/servers/add" class=row style="margin-top:12px">{csrf}
<input type=text name=name placeholder="Name (e.g. Main)" style="max-width:180px">
<input type=text name=url placeholder="Xtream address, e.g. http://line.example.com:8080" required>
<button class=primary>Add server</button></form></section>

<div class=grid>
<section><h2>Test a login</h2>
<form method=post action="/admin/test">{csrf}
<div class=row><input type=text name=username placeholder=Username required autocomplete=off>
<input type=password name=password placeholder=Password required autocomplete=new-password><button class=primary>Test</button></div>
<p class=muted style="margin-bottom:0">Asks every server. Nothing is saved, and the password isn't logged.</p></form></section>

<section><h2>Look up a customer</h2>
<form method=get action="/admin" class=row><input type=text name=user placeholder=Username value="{e(lookup)}" required><button>Look up</button></form>
{user_details(lookup, csrf) if lookup else ''}</section>
</div>

<section><h2>Blocked logins</h2>
<form method=post action="/admin/block" class=row style="margin-bottom:10px">{csrf}
<input type=text name=username placeholder=Username required><input type=text name=reason placeholder="Reason (only you see it)"><button class=danger>Block</button></form>
<table><tr><th>Username</th><th>Reason</th><th>Since</th><th></th></tr>{blocked_rows}</table></section>

<section><h2>Recent logins</h2>
<table><tr><th>When</th><th>Username</th><th>Result</th><th>Server</th><th>IP</th></tr>{log_rows}</table></section>
""")

    def user_details(username: str, csrf: str) -> str:
        last = store.last_server(username)
        srv = servers.get(last) if last else None
        reason = store.block_reason(username)
        devs = store.devices(username)
        dev_rows = "".join(
            f"<tr><td>{e(d.device_name or '?')}</td><td class=muted>v{e(d.app_version or '?')}</td><td class=muted>{fmt_ts(d.last_seen)}</td><td class=muted>{e(d.ip or '')}</td></tr>"
            for d in devs) or "<tr><td colspan=4 class=muted>No devices yet.</td></tr>"
        hist = "".join(
            f"<tr><td class=muted>{fmt_ts(ts)}</td><td><span class='pill {OUTCOME_PILL.get(res, '')}'>{e(res)}</span></td><td>{e(s or '')}</td></tr>"
            for ts, _, _, res, s in store.recent_log(username, 8)) or "<tr><td colspan=3 class=muted>Never logged in.</td></tr>"
        hidden = f"<input type=hidden name=username value='{e(username)}'>"
        return f"""<div style="margin-top:12px">
<p><b><code>{e(username)}</code></b> · last server: {e(srv.name) if srv else '<span class=muted>none</span>'}
 · {'<span class="pill bad">blocked</span>' if reason is not None else '<span class="pill ok">not blocked</span>'}</p>
<table><tr><th>Device</th><th>App</th><th>Last seen</th><th>IP</th></tr>{dev_rows}</table>
<div style="margin:10px 0">
<form method=post action="/admin/devices/forget" class=inline>{csrf}{hidden}<button>Reset devices</button></form>
<form method=post action="/admin/forget-server" class=inline>{csrf}{hidden}<button>Forget last server</button></form></div>
<table><tr><th>When</th><th>Result</th><th>Server</th></tr>{hist}</table></div>"""

    async def dashboard(request: Request, f, session) -> Response:
        return render(session, request, lookup=request.query_params.get("user", "").strip())

    # ---- actions -----------------------------------------------------------------------------

    async def add_server(request, f, session):
        url = normalize_url(f.get("url", ""))
        if not valid_url(url):
            return back("That doesn't look like an address. Example: http://line.example.com:8080", err=True)
        s = servers.add(f.get("name", ""), url)
        return back(f"Added {s.name}. Use 'Test a login' to check it.")

    async def edit_server(request, f, session):
        sid = request.path_params["sid"]
        url = normalize_url(f.get("url", ""))
        if not valid_url(url):
            return back("That doesn't look like an address.", err=True)
        servers.update(sid, name=f.get("name", "").strip() or url, url=url)
        return back("Saved.")

    async def toggle_server(request, f, session):
        s = servers.get(request.path_params["sid"])
        if s:
            servers.update(s.id, enabled=not s.enabled)
            return back(f"{s.name} turned {'off' if s.enabled else 'on'}.")
        return back()

    async def move_up(request, f, session):
        servers.move(request.path_params["sid"], -1)
        return back()

    async def move_down(request, f, session):
        servers.move(request.path_params["sid"], +1)
        return back()

    async def delete_server(request, f, session):
        s = servers.get(request.path_params["sid"])
        if s:
            servers.delete(s.id)
            return back(f"Deleted {s.name}.")
        return back()

    async def test_login(request, f, session):
        username, password = f.get("username", "").strip(), f.get("password", "")
        targets = servers.all()
        if not username or not password or not targets:
            return back("Enter a username and password (and add a server first).", err=True)
        results = await xtream.check_many(targets, username, password, settings.server_timeout)
        label = {"ok": ("ok", "Login works"), "invalid": ("bad", "Not on this server"),
                 "unreachable": ("warn", "Didn't answer"), "expired": ("warn", "Expired"),
                 "banned": ("bad", "Banned"), "disabled": ("bad", "Disabled")}
        rows = "".join(
            f"<tr class=\"{'' if r.server.enabled else 'off'}\"><td>{e(r.server.name)}{'' if r.server.enabled else ' (off)'}</td>"
            f"<td><span class='pill {label[r.outcome][0]}'>{label[r.outcome][1]}</span></td>"
            f"<td class=muted>{e(r.expires_at[:10]) if r.expires_at else ('never expires' if r.found else '')}</td>"
            f"<td class=muted>{e(str(r.max_connections)) + ' connection(s)' if r.max_connections else ''}</td>"
            f"<td class=muted>{e(r.detail or '')}</td></tr>"
            for r in results)
        box = f"<section><h2>Test result for <code>{e(username)}</code></h2><table>{rows}</table></section>"
        return render(session, request, extra_top=box)

    async def block(request, f, session):
        u = f.get("username", "").strip()
        if not u:
            return back("Enter a username.", err=True)
        store.block(u, f.get("reason", "").strip())
        return back(f"Blocked {u}.")

    async def unblock(request, f, session):
        u = f.get("username", "").strip()
        store.unblock(u)
        return back(f"Unblocked {u}.")

    async def forget_devices(request, f, session):
        u = f.get("username", "").strip()
        n = store.forget_devices(u)
        return RedirectResponse(f"/admin?user={urllib.parse.quote(u)}&msg={urllib.parse.quote(f'Cleared {n} device(s).')}", status_code=303)

    async def forget_server(request, f, session):
        u = f.get("username", "").strip()
        store.forget_server(u)
        return RedirectResponse(f"/admin?user={urllib.parse.quote(u)}&msg=" + urllib.parse.quote("Done – all servers are asked at their next login."), status_code=303)

    return [
        Route("/admin/login", login_get, methods=["GET"]),
        Route("/admin/login", login_post, methods=["POST"]),
        Route("/admin/logout", guarded(lambda r, f, s: logout(r)), methods=["POST"]),
        Route("/admin", guarded(dashboard, post=False), methods=["GET"]),
        Route("/admin/servers/add", guarded(add_server), methods=["POST"]),
        Route("/admin/servers/{sid}/edit", guarded(edit_server), methods=["POST"]),
        Route("/admin/servers/{sid}/toggle", guarded(toggle_server), methods=["POST"]),
        Route("/admin/servers/{sid}/up", guarded(move_up), methods=["POST"]),
        Route("/admin/servers/{sid}/down", guarded(move_down), methods=["POST"]),
        Route("/admin/servers/{sid}/delete", guarded(delete_server), methods=["POST"]),
        Route("/admin/test", guarded(test_login), methods=["POST"]),
        Route("/admin/block", guarded(block), methods=["POST"]),
        Route("/admin/unblock", guarded(unblock), methods=["POST"]),
        Route("/admin/devices/forget", guarded(forget_devices), methods=["POST"]),
        Route("/admin/forget-server", guarded(forget_server), methods=["POST"]),
    ]
