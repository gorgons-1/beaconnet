"""SQLite access. bbs-core is the only process that opens the database."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_in(**delta: float) -> str:
    return ts(utcnow() + timedelta(**delta))


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Database:
    """Opens short-lived connections with the pragmas this system relies on.

    WAL lets readers run while one writer commits; synchronous=FULL makes each commit
    durable across a power cut (writes are rare, so the cost is negligible).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def init_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError(f"could not enable WAL mode (got {mode})")
        finally:
            conn.close()

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- migrations --------------------------------------------------------------------


def _migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    files = []
    for path in sorted(migrations_dir.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        if not prefix.isdigit():
            raise RuntimeError(f"migration file without a numeric prefix: {path.name}")
        files.append((int(prefix), path))
    versions = [v for v, _ in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError("two migration files share a version number")
    return files


def migrate(db: Database, migrations_dir: Path) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns versions applied.

    A migration that was already applied but whose file changed is an error: the schema
    on disk would no longer match what the code expects.
    """
    db.init_file()
    applied: list[int] = []
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        done = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
        for version, path in _migration_files(migrations_dir):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if version in done:
                if done[version] != checksum:
                    raise RuntimeError(f"migration {path.name} changed after it was applied")
                continue
            # executescript() commits implicitly, so wrap the whole file in one explicit
            # transaction inside the script itself.
            if "'" in path.name:
                raise RuntimeError(f"bad migration file name: {path.name}")
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + f"\nINSERT INTO schema_migrations(version, name, checksum, applied_at)"
                    f" VALUES ({version}, '{path.name}', '{checksum}', '{ts()}');\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise RuntimeError(f"migration {path.name} failed and was rolled back: {exc}") from exc
            applied.append(version)
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            raise RuntimeError(f"foreign key check failed after migration: {len(fk_problems)} rows")
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def integrity_check(conn: sqlite3.Connection, quick: bool = False) -> str:
    pragma = "quick_check" if quick else "integrity_check"
    rows = conn.execute(f"PRAGMA {pragma}").fetchall()
    result = "; ".join(r[0] for r in rows)
    return result
