"""SQLite storage. No passwords are ever stored here – the IPTV server checks the password every time."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
-- The server a login last worked on, tried first next time. Not a password, just a server id.
CREATE TABLE IF NOT EXISTS last_server (
    username   TEXT PRIMARY KEY COLLATE NOCASE,
    server_id  TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    username   TEXT PRIMARY KEY COLLATE NOCASE,
    reason     TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    username    TEXT NOT NULL COLLATE NOCASE,
    device_id   TEXT NOT NULL,
    device_name TEXT,
    app_version TEXT,
    ip          TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (username, device_id)
);
CREATE TABLE IF NOT EXISTS login_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    username TEXT,
    ip       TEXT,
    result   TEXT NOT NULL,
    server   TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_log_ts ON login_log(ts);
"""


@dataclass
class Device:
    device_id: str
    device_name: str | None
    app_version: str | None
    ip: str | None
    first_seen: int
    last_seen: int


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # ---- remembered server ---------------------------------------------------------------

    def last_server(self, username: str) -> str | None:
        row = self._db.execute("SELECT server_id FROM last_server WHERE username = ?", (username,)).fetchone()
        return row[0] if row else None

    def remember_server(self, username: str, server_id: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO last_server(username, server_id, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET server_id = excluded.server_id, updated_at = excluded.updated_at",
                (username, server_id, int(time.time())),
            )

    def forget_server(self, username: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM last_server WHERE username = ?", (username,)).rowcount

    # ---- blocks --------------------------------------------------------------------------

    def blocked(self) -> list[tuple[str, str, int]]:
        return self._db.execute("SELECT username, reason, created_at FROM blocks ORDER BY created_at DESC").fetchall()

    def block_reason(self, username: str) -> str | None:
        row = self._db.execute("SELECT reason FROM blocks WHERE username = ?", (username,)).fetchone()
        return None if row is None else (row[0] or "")

    def block(self, username: str, reason: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO blocks(username, reason, created_at) VALUES (?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET reason = excluded.reason",
                (username, reason, int(time.time())),
            )

    def unblock(self, username: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM blocks WHERE username = ?", (username,)).rowcount

    # ---- devices -------------------------------------------------------------------------

    def devices(self, username: str, since: int = 0) -> list[Device]:
        rows = self._db.execute(
            "SELECT device_id, device_name, app_version, ip, first_seen, last_seen FROM devices "
            "WHERE username = ? AND last_seen >= ? ORDER BY last_seen DESC",
            (username, since),
        ).fetchall()
        return [Device(*r) for r in rows]

    def touch_device(self, username: str, device_id: str, device_name: str | None,
                     app_version: str | None, ip: str | None) -> None:
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "INSERT INTO devices(username, device_id, device_name, app_version, ip, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(username, device_id) DO UPDATE SET "
                "device_name = excluded.device_name, app_version = excluded.app_version, "
                "ip = excluded.ip, last_seen = excluded.last_seen",
                (username, device_id, device_name, app_version, ip, now, now),
            )

    def forget_devices(self, username: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM devices WHERE username = ?", (username,)).rowcount

    # ---- log -----------------------------------------------------------------------------

    def log(self, username: str | None, ip: str | None, result: str, server: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO login_log(ts, username, ip, result, server) VALUES (?,?,?,?,?)",
                (int(time.time()), username, ip, result, server),
            )

    def recent_log(self, username: str | None = None, limit: int = 30) -> list[tuple]:
        if username:
            q = "SELECT ts, username, ip, result, server FROM login_log WHERE username = ? ORDER BY id DESC LIMIT ?"
            args: tuple = (username, limit)
        else:
            q = "SELECT ts, username, ip, result, server FROM login_log ORDER BY id DESC LIMIT ?"
            args = (limit,)
        return self._db.execute(q, args).fetchall()

    def prune_log(self, older_than_days: int = 90) -> int:
        cutoff = int(time.time()) - older_than_days * 86400
        with self._lock:
            return self._db.execute("DELETE FROM login_log WHERE ts < ?", (cutoff,)).rowcount
