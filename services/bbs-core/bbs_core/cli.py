"""bbs-core command line: serve, migrate, create-admin, backup, verify, restore, prune, ..."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from .config import ConfigError, load_settings
from .db import Database, integrity_check, migrate
from .errors import BBSError
from .logs import event, setup


def _bbs():  # type: ignore[no-untyped-def]
    from .service import BBS

    settings = load_settings()
    return BBS(settings, Database(settings.paths.db_path))


def cmd_serve(_: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    bbs = _bbs()
    log = setup("bbs-core", bbs.cfg.logging.level)
    if not {"ssh", "admin"} <= set(bbs.s.service_tokens):
        raise ConfigError("both BBS_SSH_GATEWAY_TOKEN_FILE and BBS_ADMIN_GATEWAY_TOKEN_FILE must be set")
    applied = migrate(bbs.db, bbs.s.paths.migrations_dir)
    bbs.seed()
    pruned = bbs.prune()
    event(log, "core_starting", node=bbs.cfg.node.id, migrations_applied=applied, pruned=pruned,
          admins=bbs.admin_count())
    if bbs.admin_count() == 0:
        event(log, "no_administrator", 30, hint="run scripts/bootstrap-admin.sh")
    uvicorn.run(create_app(bbs), host="0.0.0.0", port=8080, workers=1, log_config=None, access_log=False,
                server_header=False, date_header=False, limit_concurrency=64, timeout_keep_alive=30)
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    bbs = _bbs()
    applied = migrate(bbs.db, bbs.s.paths.migrations_dir)
    bbs.seed()
    print(json.dumps({"migrations_applied": applied}))
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    s = load_settings()
    print(json.dumps({"ok": True, "node": s.bbs.node.id, "boards": len(s.boards), "peers": len(s.peers),
                      "service_tokens": sorted(s.service_tokens)}))
    return 0


def cmd_create_admin(a: argparse.Namespace) -> int:
    bbs = _bbs()
    migrate(bbs.db, bbs.s.paths.migrations_dir)
    key = sys.stdin.readline().strip() if a.key == "-" else a.key
    out = bbs.bootstrap_admin(a.username, a.callsign, a.display_name, key)
    print(json.dumps(out))
    return 0


def cmd_admin_count(_: argparse.Namespace) -> int:
    print(_bbs().admin_count())
    return 0


def cmd_backup(a: argparse.Namespace) -> int:
    from .backup import backup

    bbs = _bbs()
    out = backup(bbs.db, Path(a.dest) if a.dest else bbs.s.paths.backup_dir)
    print(json.dumps(out))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    from .backup import verify

    out = verify(Path(a.file))
    print(json.dumps(out))
    return 0 if out.get("integrity") == "ok" and out.get("checksum", "ok") == "ok" else 1


def cmd_restore(a: argparse.Namespace) -> int:
    from .backup import restore

    bbs = _bbs()
    print(json.dumps(restore(bbs.db, Path(a.file), bbs.s.paths.migrations_dir)))
    return 0


def cmd_integrity(a: argparse.Namespace) -> int:
    bbs = _bbs()
    with bbs.db.session() as conn:
        result = integrity_check(conn, quick=a.quick)
    print(json.dumps({"result": result}))
    return 0 if result == "ok" else 1


def cmd_prune(_: argparse.Namespace) -> int:
    print(json.dumps(_bbs().prune()))
    return 0


def cmd_openapi(_: argparse.Namespace) -> int:
    from .api import openapi_schema

    print(json.dumps(openapi_schema(_bbs()), indent=2))
    return 0


def cmd_peer_card(a: argparse.Namespace) -> int:
    from .peers import make_card

    s = load_settings()
    key = sys.stdin.readline().strip()
    if not key.startswith("ssh-ed25519 "):
        raise ConfigError("the node public key (secrets/node_ed25519_key.pub) was not given on stdin")
    print(make_card(s.bbs.node.id, a.host, a.port, key, s.bbs.node.name))
    return 0


def cmd_peer_add(a: argparse.Namespace) -> int:
    from . import peers

    s = load_settings()
    peer, replaced = peers.add(Path(a.file), a.card, s.bbs.node.id, a.own_key)
    print(json.dumps({"peer": peer.id, "name": peer.name, "address": peer.address, "port": peer.port,
                      "updated_existing": replaced}))
    return 0


def cmd_peer_remove(a: argparse.Namespace) -> int:
    from . import peers

    if not peers.remove(Path(a.file), a.id):
        print(f"error: no peer {a.id} in {a.file}", file=sys.stderr)
        return 1
    print(json.dumps({"removed": a.id.upper()}))
    return 0


def cmd_peer_list(_: argparse.Namespace) -> int:
    s = load_settings()
    for p in s.peers:
        print(f"{p.id:<24} {'enabled ' if p.enabled else 'disabled'} {(p.address or '(dials in)') + ':' + str(p.port):<32} {p.name}")
    if not s.peers:
        print("no peers configured")
    return 0


def cmd_healthcheck(_: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=4) as r:
            ok = json.loads(r.read()).get("status") == "ok"
    except Exception:
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bbs-core")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("check-config").set_defaults(fn=cmd_check_config)
    ca = sub.add_parser("create-admin", help="create an ACTIVE administrator (public key on stdin with --key -)")
    ca.add_argument("--username", required=True)
    ca.add_argument("--callsign")
    ca.add_argument("--display-name")
    ca.add_argument("--key", default="-")
    ca.set_defaults(fn=cmd_create_admin)
    sub.add_parser("admin-count").set_defaults(fn=cmd_admin_count)
    b = sub.add_parser("backup")
    b.add_argument("--dest")
    b.set_defaults(fn=cmd_backup)
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.set_defaults(fn=cmd_verify)
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.set_defaults(fn=cmd_restore)
    i = sub.add_parser("integrity-check")
    i.add_argument("--quick", action="store_true")
    i.set_defaults(fn=cmd_integrity)
    sub.add_parser("prune").set_defaults(fn=cmd_prune)
    sub.add_parser("openapi").set_defaults(fn=cmd_openapi)
    sub.add_parser("healthcheck").set_defaults(fn=cmd_healthcheck)
    pc = sub.add_parser("peer-card", help="print this node's peer card (node public key on stdin)")
    pc.add_argument("--host", default=None, help="public host/IP peers dial; omit if behind NAT")
    pc.add_argument("--port", type=int, default=2224)
    pc.set_defaults(fn=cmd_peer_card)
    pa = sub.add_parser("peer-add", help="add a peer from its card to a peers.yaml file")
    pa.add_argument("--file", required=True)
    pa.add_argument("--card", required=True)
    pa.add_argument("--own-key")
    pa.set_defaults(fn=cmd_peer_add)
    pr = sub.add_parser("peer-remove")
    pr.add_argument("--file", required=True)
    pr.add_argument("--id", required=True)
    pr.set_defaults(fn=cmd_peer_remove)
    sub.add_parser("peer-list").set_defaults(fn=cmd_peer_list)
    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR:\n{exc}", file=sys.stderr)
        return 2
    except BBSError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
