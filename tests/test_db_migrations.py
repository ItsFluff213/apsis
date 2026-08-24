"""Tests for the database migration path in backend/db.py.

These matter because the failure mode is silent and destructive: a user
upgrades, their existing data/autopilot.db is migrated in place, and if a
migration is wrong they lose vessel names and constellations with no
obvious error. So the tests build databases in the *old* shapes that
really shipped and check the data survives the upgrade.
"""

import sqlite3

import pytest

from backend import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db at a throwaway file for the duration of one test."""
    path = tmp_path / "autopilot.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _primary_key_columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r["pk"] > 0]


class TestFreshDatabase:
    def test_creates_and_stamps_latest_version(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert int(row["value"]) == db.SCHEMA_VERSION

    def test_vessels_has_composite_primary_key(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        assert _primary_key_columns(conn, "vessels") == ["id", "save_profile"]

    def test_init_is_idempotent(self, temp_db):
        db.init_db()
        db.init_db()
        db.init_db()
        conn = _connect(temp_db)
        assert _primary_key_columns(conn, "vessels") == ["id", "save_profile"]


class TestUpgradeFromAncientDatabase:
    """The oldest shape: before save profiles existed at all."""

    @pytest.fixture(autouse=True)
    def old_database(self, temp_db):
        conn = _connect(temp_db)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE vessels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'unknown',
                notes TEXT DEFAULT '',
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE constellations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL,
                altitude_m REAL, inclination_deg REAL NOT NULL DEFAULT 0,
                created TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE constellation_members (
                constellation_id INTEGER NOT NULL,
                vessel_id TEXT NOT NULL,
                joined TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (constellation_id, vessel_id)
            );
            CREATE TABLE core_role_defaults (
                part_name TEXT PRIMARY KEY, category TEXT NOT NULL, detail TEXT DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO vessels (id, name, type, notes) VALUES (?, ?, ?, ?)",
            ("Jeb's Ride", "Jeb's Ride", "capsule", "do not lose"),
        )
        conn.execute(
            "INSERT INTO constellations (name, body, kind, altitude_m) VALUES (?, ?, ?, ?)",
            ("Comms", "Kerbin", "communications", None),
        )
        conn.commit()
        conn.close()

    def test_upgrade_succeeds(self, temp_db):
        db.init_db()  # must not raise
        conn = _connect(temp_db)
        assert int(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]) == db.SCHEMA_VERSION

    def test_user_vessel_data_survives(self, temp_db):
        """The whole point: custom names and notes are user-entered and
        cannot be regenerated, so a rebuild must copy them across."""
        db.init_db()
        conn = _connect(temp_db)
        row = conn.execute("SELECT * FROM vessels WHERE id = ?", ("Jeb's Ride",)).fetchone()
        assert row is not None
        assert row["notes"] == "do not lose"
        assert row["type"] == "capsule"
        assert row["save_profile"] == "default"

    def test_constellations_survive(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        row = conn.execute("SELECT * FROM constellations WHERE name = 'Comms'").fetchone()
        assert row is not None
        assert row["save_profile"] == "default"

    def test_vessels_primary_key_is_rebuilt(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        assert _primary_key_columns(conn, "vessels") == ["id", "save_profile"]

    def test_same_name_in_two_profiles_no_longer_collides(self, temp_db):
        """The bug the composite key exists to fix: with the old
        single-column PK this raised UNIQUE constraint failed on every
        telemetry tick once a second profile saw a same-named vessel."""
        db.init_db()
        conn = _connect(temp_db)
        conn.execute(
            "INSERT INTO vessels (id, name, type, save_profile) VALUES (?, ?, ?, ?)",
            ("Jeb's Ride", "Jeb's Ride", "capsule", "career-two"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM vessels").fetchone()["c"] == 2

    def test_no_leftover_scratch_table(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "vessels_old" not in names


class TestUpgradeFromIntermediateDatabase:
    """The shape after save_profile columns were added but before the
    primary keys were rebuilt -- the version most existing users are on."""

    @pytest.fixture(autouse=True)
    def old_database(self, temp_db):
        conn = _connect(temp_db)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE vessels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'unknown',
                notes TEXT DEFAULT '',
                save_profile TEXT NOT NULL DEFAULT 'default',
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE constellations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL,
                altitude_m REAL, inclination_deg REAL NOT NULL DEFAULT 0,
                save_profile TEXT NOT NULL DEFAULT 'default',
                created TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE constellation_members (
                constellation_id INTEGER NOT NULL, vessel_id TEXT NOT NULL,
                save_profile TEXT NOT NULL DEFAULT 'default',
                joined TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (constellation_id, vessel_id)
            );
            CREATE TABLE core_role_defaults (
                part_name TEXT PRIMARY KEY, category TEXT NOT NULL, detail TEXT DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO vessels (id, name, type, notes, save_profile) VALUES (?, ?, ?, ?, ?)",
            ("Station Alpha", "Station Alpha", "station", "keep", "career-one"),
        )
        conn.commit()
        conn.close()

    def test_upgrade_preserves_non_default_profile(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        row = conn.execute("SELECT * FROM vessels WHERE id = 'Station Alpha'").fetchone()
        assert row["save_profile"] == "career-one"
        assert row["notes"] == "keep"

    def test_primary_keys_rebuilt(self, temp_db):
        db.init_db()
        conn = _connect(temp_db)
        assert _primary_key_columns(conn, "vessels") == ["id", "save_profile"]
        assert _primary_key_columns(conn, "core_role_defaults") == ["part_name", "save_profile"]


class TestMigrationsOnlyRunOnce:
    def test_second_init_does_not_rerun_migrations(self, temp_db, monkeypatch):
        db.init_db()

        calls = []

        def spy(conn):
            calls.append(1)

        monkeypatch.setattr(
            db, "MIGRATIONS", [(1, "spy", spy), (2, "spy", spy), (3, "spy", spy)],
        )
        db.init_db()
        assert calls == [], "already-applied migrations must not run again"
