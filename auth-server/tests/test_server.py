"""End-to-end tests: real uvicorn server + fake IPTV servers speaking player_api.php."""
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
import uvicorn

from authserver.config import Settings
from authserver.server import create_app
from authserver.store import Store

FUTURE = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
PAST = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())

SERVER_A = {
    "alice": ("pw1", {"status": "Active", "exp_date": str(FUTURE), "max_connections": "2"}),
    "olduser": ("pw2", {"status": "Expired", "exp_date": str(PAST)}),
    "banned1": ("pw3", {"status": "Banned", "exp_date": str(FUTURE)}),
    "lifetime": ("pw4", {"status": "Active", "exp_date": None, "max_connections": "1"}),
    "dup": ("pwd", {"status": "Active", "exp_date": str(FUTURE)}),
}
SERVER_B = {
    "bob": ("pwb", {"status": "Active", "exp_date": str(FUTURE), "max_connections": "1"}),
    "dup": ("pwd", {"status": "Active", "exp_date": str(FUTURE)}),
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fake_server(accounts, style="auth0"):
    hits = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] += 1
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            user, pw = q.get("username", [""])[0], q.get("password", [""])[0]
            acct = accounts.get(user)
            if u.path != "/player_api.php":
                self.send_response(404); self.end_headers(); return
            if acct and acct[0] == pw:
                body = {"user_info": {"auth": 1, "username": user, **acct[1]}, "server_info": {}}
            elif style == "http401":
                self.send_response(401); self.end_headers(); return
            else:
                body = {"user_info": {"auth": 0}}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", hits


@pytest.fixture
def env(tmp_path):
    a_srv, a_url, a_hits = fake_server(SERVER_A)
    b_srv, b_url, b_hits = fake_server(SERVER_B, style="http401")
    dead_url = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    servers_file = tmp_path / "servers.json"
    servers_file.write_text(json.dumps({"servers": [
        {"id": "a", "name": "A", "url": a_url},
        {"id": "b", "name": "B", "url": b_url},
    ]}))
    (tmp_path / "app.json").write_text(json.dumps({"min_version": "1.0.0", "notice": "hi"}))
    started = []

    def start(**overrides):
        s = Settings(
            db_path=str(tmp_path / "auth.db"),
            servers_file=str(servers_file),
            app_config_file=str(tmp_path / "app.json"),
            admin_password="", server_timeout=2, trust_proxy=True,
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        store = Store(s.db_path)
        app = create_app(s, store)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        started.append(server)
        return f"http://127.0.0.1:{port}", store, app.state.servers

    yield {"start": start, "a_url": a_url, "b_url": b_url, "dead_url": dead_url,
           "a_hits": a_hits, "b_hits": b_hits, "servers_file": servers_file}
    for s in started:
        s.should_exit = True
    a_srv.shutdown(); b_srv.shutdown()


def login(base, username, password, device="dev-1", ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return requests.post(f"{base}/v1/login", json={
        "username": username, "password": password, "device_id": device,
        "device_name": "Fire TV", "app_version": "1.0.0"}, headers=headers, timeout=10)


# ---- login API -------------------------------------------------------------------------------

def test_finds_the_right_server(env):
    base, store, _ = env["start"]()
    r = login(base, "bob", "pwb")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["server"] == env["b_url"] and j["max_connections"] == 1
    assert j["config"]["notice"] == "hi"
    assert "pwb" not in r.text
    assert store.last_server("bob") == "b"
    assert store.devices("bob")[0].device_name == "Fire TV"


def test_remembered_server_is_asked_alone_next_time(env):
    base, store, _ = env["start"]()
    login(base, "bob", "pwb")
    a_before, b_before = env["a_hits"]["n"], env["b_hits"]["n"]
    assert login(base, "bob", "pwb").json()["ok"]
    assert env["a_hits"]["n"] == a_before          # A not asked
    assert env["b_hits"]["n"] == b_before + 1


def test_customer_moved_to_another_server(env):
    base, store, _ = env["start"]()
    store.remember_server("alice", "b")            # last time on B, but now lives on A
    j = login(base, "alice", "pw1").json()
    assert j["ok"] and j["server"] == env["a_url"]
    assert store.last_server("alice") == "a"


def test_list_order_decides_when_login_is_on_two_servers(env):
    base, store, servers = env["start"]()
    assert login(base, "dup", "pwd").json()["server"] == env["a_url"]
    store.forget_server("dup")
    servers.move("b", -1)                          # B first now
    assert login(base, "dup", "pwd").json()["server"] == env["b_url"]


def test_lifetime_account_has_no_expiry(env):
    base, *_ = env["start"]()
    j = login(base, "lifetime", "pw4").json()
    assert j["ok"] and j["expires_at"] is None


def test_wrong_password_and_unknown_user_look_the_same(env):
    base, *_ = env["start"]()
    wrong, unknown = login(base, "alice", "nope"), login(base, "ghost", "x")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_expired_and_banned(env):
    base, *_ = env["start"]()
    r = login(base, "olduser", "pw2")
    assert r.status_code == 403 and r.json()["error"] == "expired" and r.json()["expires_at"]
    assert login(base, "banned1", "pw3").json()["error"] == "banned"
    assert login(base, "olduser", "wrong").json()["error"] == "invalid_credentials"


def test_block_overrides_server(env):
    base, store, _ = env["start"]()
    store.block("alice", "chargeback")
    assert login(base, "alice", "pw1").json()["error"] == "blocked"
    store.unblock("alice")
    assert login(base, "alice", "pw1").json()["ok"]


def test_switched_off_server_is_skipped(env):
    base, store, servers = env["start"]()
    servers.update("b", enabled=False)
    assert login(base, "bob", "pwb").status_code == 401
    servers.update("b", enabled=True)
    assert login(base, "bob", "pwb").json()["ok"]


def test_hand_edited_servers_json_is_picked_up(env):
    base, *_ = env["start"]()
    f = env["servers_file"]
    data = json.loads(f.read_text())
    data["servers"] = [s for s in data["servers"] if s["id"] != "b"]
    time.sleep(0.05)
    f.write_text(json.dumps(data))
    os.utime(f, (time.time() + 2, time.time() + 2))   # make sure mtime changes
    assert login(base, "bob", "pwb").status_code == 401
    assert requests.get(f"{base}/healthz", timeout=5).json()["servers"] == 1


def test_one_server_down_still_finds_login_on_others(env):
    base, store, servers = env["start"]()
    servers.add("Dead", env["dead_url"])
    j = login(base, "bob", "pwb").json()
    assert j["ok"] and j["server"] == env["b_url"]


def test_wrong_password_while_a_server_is_down_is_not_a_strike(env):
    base, store, servers = env["start"](user_fail_limit=2)
    servers.add("Dead", env["dead_url"])
    for _ in range(4):
        assert login(base, "alice", "bad").json()["error"] == "invalid_credentials"
    assert login(base, "alice", "pw1").json()["ok"]   # never rate-limited


def test_their_server_down_is_503(env):
    base, store, servers = env["start"]()
    dead = servers.add("Dead", env["dead_url"])
    store.remember_server("carol", dead.id)
    r = login(base, "carol", "x")
    assert r.status_code == 503 and r.json()["error"] == "server_unreachable"


def test_all_servers_down_or_none_is_503(env):
    base, store, servers = env["start"]()
    servers.save([])
    assert login(base, "alice", "pw1").status_code == 503
    servers.add("Dead", env["dead_url"])
    assert login(base, "alice", "pw1").status_code == 503


def test_user_fail_rate_limit_and_reset_on_success(env):
    base, *_ = env["start"](user_fail_limit=3)
    login(base, "alice", "x"); login(base, "alice", "x")
    assert login(base, "alice", "pw1").json()["ok"]
    for _ in range(3):
        login(base, "alice", "x")
    r = login(base, "alice", "pw1")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_ip_rate_limit(env):
    base, *_ = env["start"](ip_limit=3)
    for _ in range(3):
        login(base, "ghost", "x", ip="9.9.9.9")
    assert login(base, "ghost", "x", ip="9.9.9.9").status_code == 429
    assert login(base, "ghost", "x", ip="8.8.8.8").status_code == 401


def test_device_limit(env):
    base, store, _ = env["start"](max_devices=2)
    assert login(base, "alice", "pw1", device="d1").json()["ok"]
    assert login(base, "alice", "pw1", device="d2").json()["ok"]
    assert login(base, "alice", "pw1", device="d3").json()["error"] == "too_many_devices"
    assert login(base, "alice", "pw1", device="d1").json()["ok"]
    store.forget_devices("alice")
    assert login(base, "alice", "pw1", device="d3").json()["ok"]


def test_bad_requests(env):
    base, *_ = env["start"]()
    assert requests.post(f"{base}/v1/login", data="not json", timeout=5).status_code == 400
    assert requests.post(f"{base}/v1/login", json=[1, 2], timeout=5).status_code == 400
    assert login(base, "", "x").status_code == 400
    assert login(base, "has space", "x").status_code == 400
    assert login(base, "alice", "").status_code == 400
    assert requests.get(f"{base}/v1/login", timeout=5).status_code == 405


def test_config_and_health(env):
    base, *_ = env["start"]()
    assert requests.get(f"{base}/v1/config", timeout=5).json()["min_version"] == "1.0.0"
    assert requests.get(f"{base}/healthz", timeout=5).json() == {"ok": True, "servers": 2}


def test_log_never_contains_password(env):
    base, store, _ = env["start"]()
    login(base, "alice", "pw1"); login(base, "alice", "secretwrong")
    dump = "\n".join(str(r) for r in store.recent_log())
    assert "pw1" not in dump and "secretwrong" not in dump
    assert [r[3] for r in store.recent_log("alice")] == ["invalid", "ok"]


# ---- admin page --------------------------------------------------------------------------------

def admin_session(base, password="letmein"):
    s = requests.Session()
    r = s.post(f"{base}/admin/login", data={"password": password}, allow_redirects=False, timeout=5)
    return s, r


def csrf_of(html):
    return re.search(r"name=csrf value='([0-9a-f]+)'", html).group(1)


def test_admin_off_without_password(env):
    base, *_ = env["start"]()
    assert requests.get(f"{base}/admin", timeout=5).status_code == 404


def test_admin_requires_sign_in(env):
    base, *_ = env["start"](admin_password="letmein")
    r = requests.get(f"{base}/admin", allow_redirects=False, timeout=5)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    s, r = admin_session(base, "wrong")
    assert "Wrong password" in r.text and "auth_admin" not in s.cookies
    s, r = admin_session(base)
    assert r.status_code == 303 and "auth_admin" in s.cookies
    page = s.get(f"{base}/admin", timeout=5).text
    assert "Servers" in page and env["a_url"] in page


def test_admin_login_rate_limited(env):
    base, *_ = env["start"](admin_password="letmein")
    for _ in range(10):
        admin_session(base, "nope")
    _, r = admin_session(base)          # even the right password waits
    assert "Too many attempts" in r.text


def test_admin_forms_need_csrf(env):
    base, _, servers = env["start"](admin_password="letmein")
    s, _ = admin_session(base)
    s.post(f"{base}/admin/servers/add", data={"name": "X", "url": "http://x.example.com"}, timeout=5)
    assert len(servers.all()) == 2       # refused


def test_admin_manage_servers(env):
    base, _, servers = env["start"](admin_password="letmein")
    s, _ = admin_session(base)
    tok = csrf_of(s.get(f"{base}/admin", timeout=5).text)

    r = s.post(f"{base}/admin/servers/add", data={"csrf": tok, "name": "New", "url": "line.new.com:8080"}, timeout=5)
    assert "Added New" in r.text
    new = [x for x in servers.all() if x.name == "New"][0]
    assert new.url == "http://line.new.com:8080"
    saved = json.loads(env["servers_file"].read_text())        # written to servers.json
    assert any(x["name"] == "New" for x in saved["servers"])

    s.post(f"{base}/admin/servers/{new.id}/up", data={"csrf": tok}, timeout=5)
    assert [x.name for x in servers.all()] == ["A", "New", "B"]
    s.post(f"{base}/admin/servers/{new.id}/edit", data={"csrf": tok, "name": "Renamed", "url": "http://other.com"}, timeout=5)
    assert servers.get(new.id).url == "http://other.com" and servers.get(new.id).name == "Renamed"
    s.post(f"{base}/admin/servers/{new.id}/toggle", data={"csrf": tok}, timeout=5)
    assert servers.get(new.id).enabled is False
    r = s.post(f"{base}/admin/servers/add", data={"csrf": tok, "name": "Bad", "url": "not a url"}, timeout=5)
    assert "doesn" in r.text and len(servers.all()) == 3
    s.post(f"{base}/admin/servers/{new.id}/delete", data={"csrf": tok}, timeout=5)
    assert [x.name for x in servers.all()] == ["A", "B"]


def test_admin_test_login_and_block(env):
    base, store, _ = env["start"](admin_password="letmein")
    s, _ = admin_session(base)
    tok = csrf_of(s.get(f"{base}/admin", timeout=5).text)
    r = s.post(f"{base}/admin/test", data={"csrf": tok, "username": "bob", "password": "pwb"}, timeout=10)
    assert "Login works" in r.text and "Not on this server" in r.text and "pwb" not in r.text
    assert store.last_server("bob") is None             # test doesn't save anything

    s.post(f"{base}/admin/block", data={"csrf": tok, "username": "bob", "reason": "late payment"}, timeout=5)
    assert login(base, "bob", "pwb").json()["error"] == "blocked"
    page = s.get(f"{base}/admin?user=bob", timeout=5).text
    assert "late payment" in page and "blocked" in page
    s.post(f"{base}/admin/unblock", data={"csrf": tok, "username": "bob"}, timeout=5)
    assert login(base, "bob", "pwb").json()["ok"]
    page = s.get(f"{base}/admin?user=bob", timeout=5).text
    assert "Fire TV" in page and "last server: B" in page


def test_admin_escapes_html(env):
    base, store, servers = env["start"](admin_password="letmein")
    store.block("<script>x</script>", "<b>r</b>")
    s, _ = admin_session(base)
    page = s.get(f"{base}/admin", timeout=5).text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
