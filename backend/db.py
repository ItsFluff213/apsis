"""Sqlite persistence for vessel metadata (the ID/registry system).

kRPC exposes no persistent vessel GUID (see backend/vessel_registry.py), so
the primary key here is the vessel's in-game name (disambiguated if more
than one vessel currently shares a name). Everything else (friendly name,
craft type, notes) is metadata we attach to that key.

Everything here is additionally scoped to a "save profile" -- kRPC exposes
no way to identify which KSP save is currently loaded, so this can't be
detected automatically. Instead the dashboard lets you name the active
profile (see get_active_profile/set_active_profile, and the header control
in the frontend); every vessel/constellation/core-role-default read or
write is implicitly scoped to whichever profile is currently active, so
switching saves doesn't mix one save's constellations/roles into another's.
Rows created before this existed default to the "default" profile.
"""

import logging
import sqlite3
from contextlib import contextmanager

from backend.paths import APP_DIR

logger = logging.getLogger("db")

DB_PATH = APP_DIR / "data" / "autopilot.db"

DEFAULT_PROFILE = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS vessels (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT DEFAULT '',
    save_profile TEXT NOT NULL DEFAULT 'default',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, save_profile)
);

CREATE TABLE IF NOT EXISTS constellations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT NOT NULL, -- 'communications' or 'custom'
    altitude_m REAL,     -- required for 'custom'; computed from the body for 'communications'
    inclination_deg REAL NOT NULL DEFAULT 0,
    save_profile TEXT NOT NULL DEFAULT 'default',
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS constellation_members (
    constellation_id INTEGER NOT NULL REFERENCES constellations(id) ON DELETE CASCADE,
    vessel_id TEXT NOT NULL,
    save_profile TEXT NOT NULL DEFAULT 'default',
    joined TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (constellation_id, vessel_id)
);

CREATE TABLE IF NOT EXISTS core_role_defaults (
    part_name TEXT NOT NULL,
    category TEXT NOT NULL,
    detail TEXT DEFAULT '',
    save_profile TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (part_name, save_profile)
);
"""

VALID_TYPES = {"unknown", "booster", "satellite", "station", "capsule", "lander", "probe", "docking"}


# --- Migrations ---------------------------------------------------------
# Numbered and recorded, rather than re-derived by inspecting PRAGMA output
# on every startup. Each entry is (version, description, function), runs
# exactly once against a database at the preceding version, and the reached
# version is stored in `meta`. Adding a migration means appending to this
# list; never edit an entry that has already shipped, since someone's
# database has already run it.
#
# A brand-new database gets the current shape from SCHEMA directly and is
# simply stamped at the latest version without running any of these.

def _migrate_add_profile_columns(conn):
    """Save profiles arrived after these tables already existed in real
    databases. A failure here just means the column was already present.

    `vessels` is included defensively. The previous version of this code
    omitted it, and the next migration then rebuilds the vessels table with
    a `SELECT ... save_profile ... FROM vessels_old` -- which fails outright
    on a database old enough to predate the column. Verified: removing this
    one line makes the ancient-database upgrade tests fail.
    """
    for statement in (
        "ALTER TABLE vessels ADD COLUMN save_profile TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE constellations ADD COLUMN save_profile TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE constellation_members ADD COLUMN save_profile TEXT NOT NULL DEFAULT 'default'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass  # column already exists


def _migrate_vessels_composite_pk(conn):
    """vessels needs save_profile in its PRIMARY KEY -- id alone isn't
    enough, since two profiles legitimately contain vessels with the same
    in-game name.

    Confirmed live as a real bug, not theoretical: with the old
    single-column PK, switching to a second profile threw "UNIQUE
    constraint failed: vessels.id" on every single tick the moment a vessel
    with a name already used in another profile was seen again, silently
    breaking the vessel list entirely.

    SQLite cannot alter a primary key in place, so the table is rebuilt.
    This one holds real user data (custom names, notes) that cannot be
    regenerated, so rows are copied across rather than dropped.
    """
    pk_cols = [row["name"] for row in conn.execute("PRAGMA table_info(vessels)").fetchall() if row["pk"] > 0]
    if pk_cols == ["id", "save_profile"]:
        return
    conn.execute("ALTER TABLE vessels RENAME TO vessels_old")
    conn.execute(
        "CREATE TABLE vessels ("
        "id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'unknown', "
        "notes TEXT DEFAULT '', save_profile TEXT NOT NULL DEFAULT 'default', "
        "first_seen TEXT NOT NULL DEFAULT (datetime('now')), "
        "last_seen TEXT NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (id, save_profile))"
    )
    conn.execute(
        "INSERT INTO vessels (id, name, type, notes, save_profile, first_seen, last_seen) "
        "SELECT id, name, type, notes, save_profile, first_seen, last_seen FROM vessels_old"
    )
    conn.execute("DROP TABLE vessels_old")


def _migrate_core_role_defaults_pk(conn):
    """Same composite-key problem as vessels, but this table is pure
    derived cache -- relearned automatically the next time each core part
    is seen tagged -- so it is cheaper to drop and recreate than to copy."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(core_role_defaults)").fetchall()}
    if "save_profile" in cols:
        return
    conn.execute("DROP TABLE core_role_defaults")
    conn.execute(
        "CREATE TABLE core_role_defaults ("
        "part_name TEXT NOT NULL, category TEXT NOT NULL, detail TEXT DEFAULT '', "
        "save_profile TEXT NOT NULL DEFAULT 'default', PRIMARY KEY (part_name, save_profile))"
    )


MIGRATIONS = [
    (1, "add save_profile columns", _migrate_add_profile_columns),
    (2, "rebuild vessels with composite primary key", _migrate_vessels_composite_pk),
    (3, "rebuild core_role_defaults with composite primary key", _migrate_core_role_defaults_pk),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def _get_schema_version(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _set_schema_version(conn, version):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DB_PATH.exists()
    with get_conn() as conn:
        conn.executescript(SCHEMA)

        if is_new:
            # SCHEMA already describes the current shape, so there is
            # nothing to migrate -- just record where we are.
            _set_schema_version(conn, SCHEMA_VERSION)
            return

        current = _get_schema_version(conn)
        for version, description, migrate in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Applying database migration %d: %s", version, description)
            migrate(conn)
            _set_schema_version(conn, version)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- Active save profile ---

def get_active_profile() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'active_profile'").fetchone()
        return row["value"] if row else DEFAULT_PROFILE


def set_active_profile(name: str):
    name = name.strip() or DEFAULT_PROFILE
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('active_profile', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (name,),
        )


def list_profiles():
    """Every profile name that has ever had data written to it, for a
    dashboard picker -- always includes the currently active one even if
    it's brand new and has no rows yet."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT save_profile FROM vessels "
            "UNION SELECT DISTINCT save_profile FROM constellations "
            "UNION SELECT DISTINCT save_profile FROM core_role_defaults"
        ).fetchall()
        profiles = {r["save_profile"] for r in rows}
    profiles.add(get_active_profile())
    return sorted(profiles)


def upsert_seen(vessel_id: str, default_name: str):
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM vessels WHERE id = ? AND save_profile = ?", (vessel_id, profile)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO vessels (id, name, save_profile) VALUES (?, ?, ?)",
                (vessel_id, default_name, profile),
            )
        else:
            conn.execute(
                "UPDATE vessels SET last_seen = datetime('now') WHERE id = ? AND save_profile = ?",
                (vessel_id, profile),
            )


def get(vessel_id: str):
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vessels WHERE id = ? AND save_profile = ?", (vessel_id, profile)
        ).fetchone()
        return dict(row) if row else None


def all_rows():
    profile = get_active_profile()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM vessels WHERE save_profile = ?", (profile,)).fetchall()
        return {r["id"]: dict(r) for r in rows}


def set_name(vessel_id: str, name: str):
    profile = get_active_profile()
    with get_conn() as conn:
        conn.execute(
            "UPDATE vessels SET name = ? WHERE id = ? AND save_profile = ?", (name, vessel_id, profile)
        )


def set_type(vessel_id: str, vessel_type: str):
    if vessel_type not in VALID_TYPES:
        raise ValueError(f"invalid type '{vessel_type}', must be one of {sorted(VALID_TYPES)}")
    profile = get_active_profile()
    with get_conn() as conn:
        conn.execute(
            "UPDATE vessels SET type = ? WHERE id = ? AND save_profile = ?", (vessel_type, vessel_id, profile)
        )


# --- Constellations ---

CONSTELLATION_KINDS = {"communications", "custom"}


def create_constellation(name: str, body: str, kind: str, altitude_m=None, inclination_deg=0.0):
    if kind not in CONSTELLATION_KINDS:
        raise ValueError(f"invalid kind '{kind}', must be one of {sorted(CONSTELLATION_KINDS)}")
    if kind == "custom" and altitude_m is None:
        raise ValueError("custom constellations require an altitude_m")
    profile = get_active_profile()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM constellations WHERE name = ? AND save_profile = ?", (name, profile)
        ).fetchone()
        if existing is not None:
            raise ValueError(f"a constellation named {name!r} already exists in this save profile")
        cur = conn.execute(
            "INSERT INTO constellations (name, body, kind, altitude_m, inclination_deg, save_profile) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, body, kind, altitude_m, inclination_deg, profile),
        )
        return cur.lastrowid


def update_constellation_orbit(constellation_id: int, altitude_m=None, inclination_deg=None):
    """Edits a custom constellation's target altitude/inclination after
    creation -- e.g. deciding on a better scanning inclination once you've
    already set up the group. Only meaningful for 'custom' constellations;
    'communications' altitude is always auto-computed from the body's
    rotation, not something to hand-edit here. Existing members already on
    station keep flying their old orbit -- this only changes the target
    future deploys aim for."""
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT kind FROM constellations WHERE id = ? AND save_profile = ?", (constellation_id, profile)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown constellation id {constellation_id}")
        if row["kind"] != "custom":
            raise ValueError("only 'custom' constellations have an editable orbit")
        if altitude_m is not None:
            conn.execute(
                "UPDATE constellations SET altitude_m = ? WHERE id = ? AND save_profile = ?",
                (altitude_m, constellation_id, profile),
            )
        if inclination_deg is not None:
            conn.execute(
                "UPDATE constellations SET inclination_deg = ? WHERE id = ? AND save_profile = ?",
                (inclination_deg, constellation_id, profile),
            )


def sync_merge_constellation(name: str, body: str, kind: str, altitude_m, inclination_deg, members):
    """Merges one constellation from a teammate's dashboard into this one's
    active profile -- for two people running separate backend+kRPC
    instances against the same shared (e.g. LMP) save, where vessel names
    are genuinely the same universe's craft on both sides, so sharing
    constellation membership actually means something.

    Matches an existing constellation by name within the current profile:
    if found, its orbit is updated to the incoming values and any members
    not already present are added (existing members are never removed by a
    sync -- a partial/stale pull from a teammate shouldn't be able to drop
    someone's satellite out of a group). If not found, a new constellation
    is created with the incoming members."""
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM constellations WHERE name = ? AND save_profile = ?", (name, profile)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO constellations (name, body, kind, altitude_m, inclination_deg, save_profile) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, body, kind, altitude_m, inclination_deg, profile),
            )
            constellation_id = cur.lastrowid
        else:
            constellation_id = row["id"]
            conn.execute(
                "UPDATE constellations SET altitude_m = ?, inclination_deg = ? WHERE id = ? AND save_profile = ?",
                (altitude_m, inclination_deg, constellation_id, profile),
            )
        for vessel_id in members:
            # Same one-constellation-per-vessel invariant add_constellation_member
            # keeps elsewhere -- a synced-in member displaces any existing
            # (possibly stale/local-only) membership for that same vessel.
            conn.execute(
                "DELETE FROM constellation_members WHERE vessel_id = ? AND save_profile = ? AND constellation_id != ?",
                (vessel_id, profile, constellation_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO constellation_members (constellation_id, vessel_id, save_profile) "
                "VALUES (?, ?, ?)",
                (constellation_id, vessel_id, profile),
            )
        return constellation_id


def list_constellations():
    profile = get_active_profile()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM constellations WHERE save_profile = ? ORDER BY created", (profile,)
        ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            members = conn.execute(
                "SELECT vessel_id FROM constellation_members WHERE constellation_id = ? AND save_profile = ?",
                (entry["id"], profile),
            ).fetchall()
            entry["members"] = [m["vessel_id"] for m in members]
            result.append(entry)
        return result


def get_constellation(constellation_id: int):
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM constellations WHERE id = ? AND save_profile = ?", (constellation_id, profile)
        ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        members = conn.execute(
            "SELECT vessel_id FROM constellation_members WHERE constellation_id = ? AND save_profile = ?",
            (constellation_id, profile),
        ).fetchall()
        entry["members"] = [m["vessel_id"] for m in members]
        return entry


def delete_constellation(constellation_id: int):
    profile = get_active_profile()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM constellation_members WHERE constellation_id = ? AND save_profile = ?",
            (constellation_id, profile),
        )
        conn.execute(
            "DELETE FROM constellations WHERE id = ? AND save_profile = ?", (constellation_id, profile)
        )


def add_constellation_member(constellation_id: int, vessel_id: str):
    profile = get_active_profile()
    with get_conn() as conn:
        # A satellite belongs to at most one constellation at a time
        # (within its own save profile).
        conn.execute(
            "DELETE FROM constellation_members WHERE vessel_id = ? AND save_profile = ?", (vessel_id, profile)
        )
        conn.execute(
            "INSERT INTO constellation_members (constellation_id, vessel_id, save_profile) VALUES (?, ?, ?)",
            (constellation_id, vessel_id, profile),
        )


def remove_constellation_member(vessel_id: str):
    profile = get_active_profile()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM constellation_members WHERE vessel_id = ? AND save_profile = ?", (vessel_id, profile)
        )


def get_member_constellation(vessel_id: str):
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT constellation_id FROM constellation_members WHERE vessel_id = ? AND save_profile = ?",
            (vessel_id, profile),
        ).fetchone()
        return row["constellation_id"] if row else None


# --- Learned core-part -> role defaults ---
#
# Whenever a vessel's controlling part carries an explicit role tag, that
# (part name -> role) association is remembered here. A brand new vessel
# whose core has no tag yet gets that remembered role auto-applied instead
# of sitting as "unknown" -- e.g. once you've tagged one probe core model
# as "satellite", every future launch reusing that same core part is
# auto-classified the same way without retagging it by hand each time.
# Scoped per save profile, same as everything else -- a core part model
# used for satellites in one save might be used for something else entirely
# in another.

def get_core_role_default(part_name: str):
    profile = get_active_profile()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT category, detail FROM core_role_defaults WHERE part_name = ? AND save_profile = ?",
            (part_name, profile),
        ).fetchone()
        return (row["category"], row["detail"] or None) if row else (None, None)


def set_core_role_default(part_name: str, category: str, detail: str = ""):
    profile = get_active_profile()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO core_role_defaults (part_name, category, detail, save_profile) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(part_name, save_profile) DO UPDATE SET category = excluded.category, detail = excluded.detail",
            (part_name, category, detail or "", profile),
        )
