"""SQLite ownership, migrations, and consistent snapshots."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    (
        """CREATE TABLE profiles (
            mint TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL)""",
        """CREATE TABLE pairs (
            pair TEXT PRIMARY KEY, mint TEXT NOT NULL, created INTEGER)""",
        """CREATE TABLE observations (
            pair TEXT REFERENCES pairs(pair), observed INTEGER NOT NULL,
            price TEXT NOT NULL, score INTEGER NOT NULL, risk TEXT NOT NULL,
            detail TEXT NOT NULL, PRIMARY KEY(pair, observed))""",
        "CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), cents INTEGER NOT NULL CHECK(cents>=0))",
        "INSERT INTO account VALUES (1, 2000)",
        """CREATE TABLE positions (
            id INTEGER PRIMARY KEY, mint TEXT NOT NULL, pair TEXT NOT NULL, symbol TEXT NOT NULL,
            entry TEXT NOT NULL, opened INTEGER NOT NULL, entry_quote INTEGER NOT NULL,
            stake INTEGER NOT NULL CHECK(stake BETWEEN 1 AND 400),
            entry_fee INTEGER NOT NULL, fee TEXT NOT NULL, slip TEXT NOT NULL,
            stop TEXT NOT NULL, take TEXT NOT NULL, duration INTEGER NOT NULL,
            closed INTEGER, exit TEXT, proceeds INTEGER, exit_fee INTEGER, reason TEXT)""",
        "CREATE UNIQUE INDEX one_open ON positions(mint) WHERE closed IS NULL",
        """CREATE TABLE audit (
            position INTEGER REFERENCES positions(id), kind TEXT NOT NULL,
            at INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
            PRIMARY KEY(position, kind))""",
    ),
    (
        "CREATE INDEX observations_time ON observations(observed, pair)",
        "CREATE TABLE watch (address TEXT PRIMARY KEY, label TEXT NOT NULL)",
        """CREATE TABLE activity (
            address TEXT NOT NULL, signature TEXT NOT NULL, block_time INTEGER, success INTEGER,
            PRIMARY KEY(address, signature))""",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
        """CREATE TABLE notifications (
            recipient INTEGER, key TEXT, category TEXT NOT NULL, message TEXT NOT NULL,
            created INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(recipient, key))""",
    ),
    (
        "ALTER TABLE positions ADD COLUMN entry_pair_age INTEGER CHECK(entry_pair_age >= 0)",
        """ALTER TABLE positions ADD COLUMN entry_risk TEXT CHECK(
            entry_risk IN ('CLEAR','ELEVATED','HIGH','UNKNOWN'))""",
    ),
)


def owned(path):
    path = Path(path).resolve()
    if ROOT not in path.parents:
        raise ValueError("SQLite path must be inside solana_ultimate_next")
    return path


class Store:
    def __init__(self, path=None):
        self.path = owned(path if path is not None else ROOT / "runtime" / "paper.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.transaction(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError("Unsupported schema version")
            for index, statements in enumerate(MIGRATIONS[version:], version + 1):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={index}")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, write=False):
        with self.connection() as db:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def rows(self, sql, args=()):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def status(self):
        with self.transaction() as db:
            return {
                "integrity": [row[0] for row in db.execute("PRAGMA integrity_check")],
                "foreign_key_violations": len(db.execute("PRAGMA foreign_key_check").fetchall()),
                "version": db.execute("PRAGMA user_version").fetchone()[0],
                "bytes": self.path.stat().st_size,
            }

    def backup(self):
        path = owned(self.path.parent / f"backup-{uuid4().hex}.sqlite")
        with self.connection() as source:
            with sqlite_connection(path) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        return path

    @staticmethod
    def recover(backup, destination):
        backup, destination = owned(backup), owned(destination)
        with sqlite_connection(backup.as_uri() + "?mode=ro", uri=True) as source:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if (not 1 <= version <= len(MIGRATIONS)
                    or source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or source.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise ValueError("Backup validation failed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.touch(exist_ok=False)
            try:
                with sqlite_connection(destination) as target:
                    source.backup(target)
                return Store(destination)
            except Exception:
                destination.unlink()
                raise


@contextmanager
def sqlite_connection(path, **options):
    db = sqlite3.connect(path, **options)
    try:
        yield db
    finally:
        db.close()
