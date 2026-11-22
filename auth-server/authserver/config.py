"""Settings (environment variables), the server list (servers.json) and the app config file."""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("AUTH_DB", "data/auth.db"))
    servers_file: str = field(default_factory=lambda: os.environ.get("SERVERS_FILE", "config/servers.json"))
    app_config_file: str = field(default_factory=lambda: os.environ.get("APP_CONFIG_FILE", "config/app_config.json"))
    # Admin web page (/admin). Empty = the page is switched off.
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    # 0 = no limit. Otherwise: how many different devices one login may use (seen in the last 30 days).
    max_devices: int = field(default_factory=lambda: _env_int("MAX_DEVICES", 0))
    server_timeout: float = field(default_factory=lambda: float(os.environ.get("SERVER_TIMEOUT", "8")))
    # Behind Caddy/nginx: take the client IP from X-Forwarded-For.
    trust_proxy: bool = field(default_factory=lambda: _env_bool("TRUST_PROXY", True))
    # Failsafes against unnecessary calls to the IPTV servers (see guard.py).
    cache_ok_seconds: int = field(default_factory=lambda: _env_int("CACHE_OK_SECONDS", 1800))
    cache_fail_seconds: int = field(default_factory=lambda: _env_int("CACHE_FAIL_SECONDS", 600))
    server_down_seconds: int = field(default_factory=lambda: _env_int("SERVER_DOWN_SECONDS", 120))
    server_rate_per_min: int = field(default_factory=lambda: _env_int("SERVER_RATE_PER_MIN", 30))
    ip_limit: int = field(default_factory=lambda: _env_int("RATE_IP_PER_5MIN", 30))
    user_fail_limit: int = field(default_factory=lambda: _env_int("RATE_USER_FAILS_PER_15MIN", 8))


@dataclass
class Server:
    """One IPTV server logins are checked against. Order in the list = which is tried first."""
    id: str
    name: str
    url: str          # Xtream address, e.g. http://line.example.com:8080 (what the app plays from)
    enabled: bool = True


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and len(url) > len("http://x") and " " not in url


class ServerList:
    """servers.json, editable from the admin page or by hand. A hand edit is picked up automatically
    (the file's modification time is checked on every use)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._servers: list[Server] = []
        self._mtime: float | None = None
        self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._servers, self._mtime = [], None
            return
        if mtime == self._mtime:
            return
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        servers = []
        for s in raw.get("servers", []):
            url = normalize_url(str(s.get("url", "")))
            if not valid_url(url):
                continue
            servers.append(Server(
                id=str(s.get("id") or secrets.token_hex(4)),
                name=str(s.get("name") or url),
                url=url,
                enabled=bool(s.get("enabled", True)),
            ))
        self._servers, self._mtime = servers, mtime

    def all(self) -> list[Server]:
        with self._lock:
            self._reload_if_changed()
            return list(self._servers)

    def enabled(self) -> list[Server]:
        return [s for s in self.all() if s.enabled]

    def get(self, server_id: str) -> Server | None:
        return next((s for s in self.all() if s.id == server_id), None)

    def save(self, servers: list[Server]) -> None:
        """Writes atomically (temp file + rename) so a half-written file is never read."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"servers": [asdict(s) for s in servers]}, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self._servers = servers
            self._mtime = self.path.stat().st_mtime

    # Edits used by the admin page.
    def add(self, name: str, url: str) -> Server:
        s = Server(id=secrets.token_hex(4), name=name.strip() or url, url=normalize_url(url))
        self.save(self.all() + [s])
        return s

    def update(self, server_id: str, **changes) -> None:
        servers = self.all()
        for i, s in enumerate(servers):
            if s.id == server_id:
                if "url" in changes:
                    changes["url"] = normalize_url(changes["url"])
                servers[i] = Server(**{**asdict(s), **changes})
        self.save(servers)

    def delete(self, server_id: str) -> None:
        self.save([s for s in self.all() if s.id != server_id])

    def move(self, server_id: str, delta: int) -> None:
        servers = self.all()
        i = next((i for i, s in enumerate(servers) if s.id == server_id), None)
        if i is None:
            return
        j = max(0, min(len(servers) - 1, i + delta))
        servers.insert(j, servers.pop(i))
        self.save(servers)


def load_app_config(path: str) -> dict:
    """What GET /v1/config hands to the app (minimum version, update link, notice…)."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
