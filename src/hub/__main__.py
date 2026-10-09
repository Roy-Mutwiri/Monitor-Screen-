"""`python -m hub` — run the hub with uvicorn, or admin helpers.

  python -m hub serve [--host 0.0.0.0] [--port 8080]
  python -m hub hash-password            (prints HUB_ADMIN_PASSWORD_HASH for a password typed on stdin)
  python -m hub pairing-code [--label L] (direct DB access; for the compose host)
"""
from __future__ import annotations

import argparse
import getpass
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hub")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8080)
    sub.add_parser("hash-password")
    pc = sub.add_parser("pairing-code"); pc.add_argument("--label", default=""); pc.add_argument("--workspace", default="")
    args = p.parse_args(argv)
    if args.cmd == "hash-password":
        from .config import hash_password
        pw = getpass.getpass("Admin password: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        print(hash_password(pw))
        return 0
    from .config import HubSettings
    settings = HubSettings.from_env()
    if args.cmd == "pairing-code":
        from .db import make_engine, make_session_factory
        from .services import HubService
        sf = make_session_factory(make_engine(settings.database_url))
        with sf() as session:
            svc = HubService(session, pairing_ttl=settings.pairing_ttl_seconds)
            ws = svc.ensure_workspace(args.workspace or settings.default_workspace)
            code, row = svc.create_pairing_code(ws.id, args.label, "cli")
            session.commit()
        print(f"pairing code: {code}  (expires {row.expires_utc}, single use)")
        return 0
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
