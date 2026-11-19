"""Online backups with SQLite's backup API (safe while the BBS is running), and restore."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .db import Database, integrity_check, migrate, ts


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_file(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return integrity_check(conn)
    finally:
        conn.close()


def backup(db: Database, dest_dir: Path) -> dict:
    """Copy the live database page-by-page, verify the copy, gzip it, write a checksum."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts().replace("-", "").replace(":", "")
    final = dest_dir / f"bbs-{stamp}.sqlite3.gz"
    with tempfile.TemporaryDirectory(dir=dest_dir, prefix=".tmp-") as tmp:
        raw = Path(tmp) / "bbs.sqlite3"
        src = db.connect()
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst, pages=256)
        finally:
            dst.close()
            src.close()
        # The copy is a standalone database: switch it out of WAL so it is one file.
        conn = sqlite3.connect(raw)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            conn.close()
        check = _check_file(raw)
        if check != "ok":
            raise RuntimeError(f"backup copy failed integrity_check: {check}")
        part = Path(tmp) / final.name
        with raw.open("rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        os.chmod(part, 0o640)
        digest = _sha256(part)
        os.replace(part, final)  # atomic: a half-written backup never has the final name
    (final.with_suffix(final.suffix + ".sha256")).write_text(f"{digest}  {final.name}\n")
    return {"file": final.name, "sha256": digest, "size_bytes": final.stat().st_size, "integrity": "ok", "at": ts()}


def verify(path: Path) -> dict:
    """Check a .sqlite3.gz backup: checksum (if present), decompress, integrity_check."""
    sha_file = path.with_suffix(path.suffix + ".sha256")
    result: dict = {"file": path.name}
    if sha_file.exists():
        expected = sha_file.read_text().split()[0]
        actual = _sha256(path)
        result["checksum"] = "ok" if expected == actual else "MISMATCH"
        if expected != actual:
            result["integrity"] = "not checked"
            return result
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "check.sqlite3"
        with gzip.open(path, "rb") as fin, raw.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
        result["integrity"] = _check_file(raw)
        conn = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)
        try:
            result["schema_version"] = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            result["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            result["bulletins"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
    return result


def restore(db: Database, path: Path, migrations_dir: Path) -> dict:
    """Replace the database with a verified backup. bbs-core must be stopped first.

    The current database (if any) is kept beside it as bbs.sqlite3.pre-restore-<time>.
    """
    v = verify(path)
    if v.get("checksum") == "MISMATCH" or v.get("integrity") != "ok":
        raise RuntimeError(f"refusing to restore: {json.dumps(v)}")
    target = db.path
    stamp = ts().replace("-", "").replace(":", "")
    kept = None
    staging = target.with_name(target.name + ".restoring")
    with gzip.open(path, "rb") as fin, staging.open("wb") as fout:
        shutil.copyfileobj(fin, fout)
    if target.exists():
        kept = target.with_name(f"{target.name}.pre-restore-{stamp}")
        # Checkpoint first so the kept copy is complete without its -wal file.
        try:
            c = sqlite3.connect(target)
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
        except sqlite3.Error:
            pass  # a corrupt database may refuse; keep the file as-is
        os.replace(target, kept)
    for suffix in ("-wal", "-shm"):
        side = target.with_name(target.name + suffix)
        if side.exists():
            side.unlink()
    os.replace(staging, target)
    applied = migrate(db, migrations_dir)
    return {"restored_from": path.name, "previous_db_kept_as": kept.name if kept else None,
            "migrations_applied": applied, "verify": v}
