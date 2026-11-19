"""Database: migrations, persistence, transactions, crash safety, backup and restore, config."""

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from bbs_core.backup import backup, restore, verify
from bbs_core.config import ConfigError, load_settings
from bbs_core.db import Database, integrity_check, migrate
from bbs_core.errors import BBSError
from conftest import ROOT, make_bbs


def test_pragmas(bbs):
    with bbs.db.session() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ssh_keys(user_id, key_type, key_data, fingerprint, created_at) VALUES (999,'a','b','c','d')")


def test_migrations_idempotent_and_tamper_evident(env, tmp_path):
    bbs = make_bbs(env)
    assert migrate(bbs.db, Path(env["BBS_MIGRATIONS_DIR"])) == []
    mdir = tmp_path / "mig"
    shutil.copytree(env["BBS_MIGRATIONS_DIR"], mdir)
    (mdir / "0001_initial.sql").write_text((mdir / "0001_initial.sql").read_text() + "\n-- edited\n")
    with pytest.raises(RuntimeError, match="changed after it was applied"):
        migrate(bbs.db, mdir)


def test_failed_migration_rolls_back(env, tmp_path):
    bbs = make_bbs(env)
    mdir = tmp_path / "mig"
    shutil.copytree(env["BBS_MIGRATIONS_DIR"], mdir)
    (mdir / "0099_broken.sql").write_text("CREATE TABLE half_done(x INTEGER);\nTHIS IS NOT SQL;\n")
    with pytest.raises(RuntimeError, match="rolled back"):
        migrate(bbs.db, mdir)
    with bbs.db.session() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2


def test_data_persists_across_restart(env, ssh_api, active_user):
    _, _, s = active_user
    n = ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "Persist", "body": "me"}).json()["number"]
    fresh = make_bbs(env)  # a new process opening the same file
    with fresh.db.session() as conn:
        assert conn.execute("SELECT subject FROM messages WHERE id = ?", (n,)).fetchone()[0] == "Persist"
    # The session token survives a core restart too (sessions are in the database).
    from fastapi.testclient import TestClient

    from bbs_core.api import create_app
    from conftest import SSH_TOKEN

    c2 = TestClient(create_app(fresh))
    r = c2.get(f"/v1/messages/{n}", headers={"X-Service-Token": SSH_TOKEN, "Authorization": f"Bearer {s}"})
    assert r.status_code == 200


def test_failed_operation_leaves_no_partial_state(bbs, ssh_api, active_user, monkeypatch):
    _, _, s = active_user
    with bbs.db.session() as conn:
        before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-transaction")

    # Emergency posts write an audit row after the message INSERT, inside the same transaction.
    monkeypatch.setattr(bbs, "audit", boom)
    r = ssh_api.call("POST", "/v1/messages", s, json={"board": "EMERGENCY", "subject": "x", "body": "y", "area": "Fife"})
    assert r.status_code == 500
    with bbs.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
        assert integrity_check(conn) == "ok"


def test_killed_writer_does_not_corrupt(bbs):
    """SIGKILL a process in the middle of a large write transaction."""
    script = textwrap.dedent(f"""
        import sqlite3, sys, time
        conn = sqlite3.connect({str(bbs.db.path)!r}, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        for i in range(200000):
            conn.execute("INSERT INTO audit_log(ts, action, outcome) VALUES ('t', 'flood', 'success')")
            if i == 5000:
                print("writing", flush=True)
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "writing"
    time.sleep(0.2)
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    with bbs.db.session() as conn:
        assert integrity_check(conn) == "ok"
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='flood'").fetchone()[0] == 0
    # And the database is still writable afterwards.
    bbs.audit_now(None, "after_crash", "success")


def test_backup_verify_restore(bbs, env, ssh_api, active_user, tmp_path):
    _, _, s = active_user
    ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "Before backup", "body": "keep"})
    out = backup(bbs.db, Path(env["BBS_BACKUP_DIR"]))
    f = Path(env["BBS_BACKUP_DIR"]) / out["file"]
    assert f.exists() and out["integrity"] == "ok"
    v = verify(f)
    assert v["integrity"] == "ok" and v["checksum"] == "ok" and v["bulletins"] == 1
    ssh_api.call("POST", "/v1/messages", s, json={"board": "GENERAL", "subject": "After backup", "body": "lost"})
    res = restore(bbs.db, f, Path(env["BBS_MIGRATIONS_DIR"]))
    assert res["previous_db_kept_as"].startswith("bbs.sqlite3.pre-restore-")
    with bbs.db.session() as conn:
        subjects = [r[0] for r in conn.execute("SELECT subject FROM messages")]
        assert subjects == ["Before backup"]
        assert integrity_check(conn) == "ok"


def test_restore_refuses_damaged_backup(bbs, env):
    out = backup(bbs.db, Path(env["BBS_BACKUP_DIR"]))
    f = Path(env["BBS_BACKUP_DIR"]) / out["file"]
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    assert verify(f)["checksum"] == "MISMATCH"
    with pytest.raises(RuntimeError, match="refusing"):
        restore(bbs.db, f, Path(env["BBS_MIGRATIONS_DIR"]))


def test_backup_restores_on_replacement_host(bbs, env, tmp_path, ssh_api, active_user):
    """Restore into an empty data directory, as on a newly installed Pi."""
    out = backup(bbs.db, Path(env["BBS_BACKUP_DIR"]))
    new_env = dict(env, BBS_DB_PATH=str(tmp_path / "newpi" / "bbs.sqlite3"))
    Path(new_env["BBS_DB_PATH"]).parent.mkdir()
    s = load_settings(new_env)
    res = restore(Database(s.paths.db_path), Path(env["BBS_BACKUP_DIR"]) / out["file"], s.paths.migrations_dir)
    assert res["previous_db_kept_as"] is None
    fresh = make_bbs(new_env)
    assert fresh.admin_count() == 1


def test_config_validation(env, tmp_path):
    cfg = Path(env["BBS_CONFIG_DIR"])
    good = (cfg / "bbs.yaml").read_text()
    for bad in (good.replace("id: SCOTLAND-HUB-01", "id: bad id"),
                good.replace("guest_idle_timeout_s: 600", "guest_idle_timeout_s: 5"),
                good + "\nunknown_section: {}\n",
                "node: [not, a, mapping]\n"):
        (cfg / "bbs.yaml").write_text(bad)
        with pytest.raises(ConfigError):
            load_settings(env)
    (cfg / "bbs.yaml").write_text(good)
    with pytest.raises(ConfigError, match="BBS_NODE_ID|node.id|pattern"):
        load_settings(dict(env, BBS_NODE_ID="lower case"))
    with pytest.raises(ConfigError, match="not found"):
        load_settings(dict(env, BBS_SSH_GATEWAY_TOKEN_FILE=str(tmp_path / "missing")))
    (tmp_path / "short").write_text("tooshort")
    with pytest.raises(ConfigError, match="shorter"):
        load_settings(dict(env, BBS_SSH_GATEWAY_TOKEN_FILE=str(tmp_path / "short")))
    with pytest.raises(ConfigError, match="identical"):
        load_settings(dict(env, BBS_ADMIN_GATEWAY_TOKEN_FILE=env["BBS_SSH_GATEWAY_TOKEN_FILE"]))
    (cfg / "boards.yaml").write_text("boards:\n  - name: GENERAL\n  - name: GENERAL\n")
    with pytest.raises(ConfigError, match="unique"):
        load_settings(env)


def test_bootstrap_admin_validation(bbs):
    with pytest.raises(BBSError):
        bbs.bootstrap_admin("root", None, None, "ssh-ed25519 AAAA")
    with pytest.raises(BBSError):
        bbs.bootstrap_admin("sysopx", None, None, "not a key")


def test_prune_retention(bbs):
    with bbs.db.session() as conn:
        conn.execute("INSERT INTO audit_log(ts, action, outcome) VALUES ('2000-01-01T00:00:00Z', 'ancient', 'success')")
    out = bbs.prune()
    assert out["audit"] == 1


def test_openapi_schema_up_to_date(bbs):
    """shared/schemas/openapi.json must match the code (regenerate with scripts/export-openapi.sh)."""
    import json

    from bbs_core.api import openapi_schema

    current = json.loads((ROOT / "shared/schemas/openapi.json").read_text())
    assert current == json.loads(json.dumps(openapi_schema(bbs)))
