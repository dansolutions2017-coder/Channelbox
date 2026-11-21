#!/usr/bin/env python3
"""Command-line admin (same things as the /admin page). On the server:
    docker compose exec auth python manage.py <command>

  servers                          list servers in servers.json
  add-server URL [--name NAME]     add a server (Xtream address)
  remove-server NAME_OR_ID
  test USERNAME                    try a login on every server (asks for the password)
  show USERNAME                    last server, block, devices, recent logins
  block USERNAME [--reason TEXT]   refuse this login whatever the server says
  unblock USERNAME
  devices USERNAME --forget        clear the devices a login has used
  log [--user USERNAME] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import datetime

from authserver import xtream
from authserver.config import ServerList, Settings, normalize_url, valid_url
from authserver.store import Store


def fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("servers")
    p = sub.add_parser("add-server"); p.add_argument("url"); p.add_argument("--name", default="")
    p = sub.add_parser("remove-server"); p.add_argument("server")
    p = sub.add_parser("test"); p.add_argument("username")
    p = sub.add_parser("show"); p.add_argument("username")
    p = sub.add_parser("block"); p.add_argument("username"); p.add_argument("--reason", default="")
    p = sub.add_parser("unblock"); p.add_argument("username")
    p = sub.add_parser("devices"); p.add_argument("username"); p.add_argument("--forget", action="store_true")
    p = sub.add_parser("log"); p.add_argument("--user"); p.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    settings = Settings()
    servers = ServerList(settings.servers_file)
    store = Store(settings.db_path)

    if args.cmd == "servers":
        for i, s in enumerate(servers.all(), 1):
            print(f"{i:>2}. {s.name:<20} {s.url:<45} {'on' if s.enabled else 'OFF'}  (id {s.id})")
        if not servers.all():
            print("No servers yet.")

    elif args.cmd == "add-server":
        url = normalize_url(args.url)
        if not valid_url(url):
            raise SystemExit("Not a valid address, e.g. http://line.example.com:8080")
        s = servers.add(args.name, url)
        print(f"Added {s.name} ({s.url}).")

    elif args.cmd == "remove-server":
        match = [s for s in servers.all() if args.server in (s.id, s.name)]
        if not match:
            raise SystemExit("No server with that name or id.")
        servers.delete(match[0].id)
        print(f"Removed {match[0].name}.")

    elif args.cmd == "test":
        password = getpass.getpass("Password: ")
        for r in asyncio.run(xtream.check_many(servers.all(), args.username, password, settings.server_timeout)):
            print(f"{r.server.name:<20} {r.outcome:<12} expires={r.expires_at or ('never' if r.found else '-')}  "
                  f"connections={r.max_connections or '-'}  {r.detail or ''}")

    elif args.cmd == "show":
        u = args.username
        last = store.last_server(u)
        srv = servers.get(last) if last else None
        reason = store.block_reason(u)
        print(f"Login:       {u}")
        print(f"Last server: {srv.name if srv else '-'}")
        print(f"Blocked:     {'yes' + (f' ({reason})' if reason else '') if reason is not None else 'no'}")
        for d in store.devices(u):
            print(f"   device {d.device_name or '?':<24} v{d.app_version or '?':<8} last {fmt_ts(d.last_seen)}  {d.ip or ''}")
        for ts, _, ip, result, s in store.recent_log(u, 10):
            print(f"   {fmt_ts(ts)}  {result:<18} {s or '':<14} {ip or ''}")

    elif args.cmd == "block":
        store.block(args.username, args.reason); print(f"Blocked {args.username}.")

    elif args.cmd == "unblock":
        print("Unblocked." if store.unblock(args.username) else "Wasn't blocked.")

    elif args.cmd == "devices":
        if args.forget:
            print(f"Forgot {store.forget_devices(args.username)} device(s).")
        else:
            for d in store.devices(args.username):
                print(f"{d.device_name or '?':<24} {d.device_id:<40} v{d.app_version or '?'}  last {fmt_ts(d.last_seen)}")

    elif args.cmd == "log":
        for ts, user, ip, result, s in store.recent_log(args.user, args.limit):
            print(f"{fmt_ts(ts)}  {user or '':<20} {result:<18} {s or '':<14} {ip or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
