"""The vessel ID system: keeps every craft kRPC can see mapped to a
persistent friendly identity, backed by sqlite so it survives restarts.

kRPC (the version this targets, 0.6.0) does not expose any persistent GUID
for a vessel or its parts -- there is no Vessel.id or Part.id over the RPC
API at all (checked directly against a running game). So the identity key
here is the vessel's in-game name, disambiguated when multiple vessels
currently share a name. This is a real, known limitation: renaming a craft
in KSP starts a new identity in our registry rather than following the old
one. Good enough for now; revisit if it becomes a practical problem.

Vessel type/role is in-game-driven, same as part roles: tag the vessel's
controlling part (its probe core or command pod) with a category from
db.VALID_TYPES (e.g. "booster", or "booster.stage1" to also carry a detail)
and that's authoritative every sync. Only vessels with no such tag fall
back to kRPC's own VesselType inference, and then to "unknown" -- the
dashboard has no way to set this itself, matching how part roles work.
"""

import logging

from backend import db, parts
from backend.krpc_client import NotConnected

logger = logging.getLogger("vessel_registry")

# KSP's own vessel.type values (VesselType enum, .name is lowercase, e.g.
# "relay"), mapped down to our simpler type set. Used only as a fallback
# when the vessel's core has no recognized role tag.
_KSP_TYPE_MAP = {
    "ship": "unknown",
    "probe": "probe",
    "relay": "satellite",
    "rover": "unknown",
    "lander": "lander",
    "station": "station",
    "base": "station",
    "debris": "unknown",
}


def _keyed_vessels(vessels):
    """Yields (key, vessel) pairs, disambiguating vessels that currently
    share the same in-game name (e.g. "Ship", "Ship #2").

    Confirmed live: sc.vessels is NOT returned in a stable order between
    calls -- with two same-named vessels, which one kRPC lists first can
    flip from one poll to the next. Assigning "#1"/"#2" by raw enumeration
    order therefore let the SAME physical vessel's key swap back and forth
    every couple seconds, which silently broke everything keyed on vessel
    id downstream: the frontend's card-reuse-by-id logic (each swap looked
    like "vessel disappeared, new one appeared", tearing down and rebuilding
    the DOM node mid-interaction), the in-memory running-job map (a job
    started under one key became invisible the moment the key flipped to
    the other vessel), and per-vessel sqlite rows. Confirmed live: this is
    what made a real launch look like it silently wasn't happening --
    the ascent job kept running, just under a key the UI had already
    swapped away from.

    Each kRPC vessel proxy carries a private but per-connection-stable
    `_object_id` (confirmed live: identical across repeated sc.vessels
    calls for the same physical vessel, within one connection's lifetime).
    Sorting by it before assigning suffixes makes the disambiguation itself
    stable for as long as the backend stays connected, instead of depending
    on kRPC's enumeration order."""
    ordered = sorted(vessels, key=lambda v: getattr(v, "_object_id", 0))
    seen_counts = {}
    for vessel in ordered:
        seen_counts[vessel.name] = seen_counts.get(vessel.name, 0) + 1
        n = seen_counts[vessel.name]
        key = vessel.name if n == 1 else f"{vessel.name} #{n}"
        yield key, vessel


def _resolve_type(vessel):
    """(type, detail) for a vessel: core tag first, else a remembered
    default for this same core part (see db.get/set_core_role_default),
    else kRPC's own inference, else unknown/None."""
    category, detail = parts.get_vessel_role_tag(vessel)
    if category in db.VALID_TYPES:
        try:
            core_name = vessel.parts.controlling.name
        except Exception:
            core_name = None
        if core_name:
            db.set_core_role_default(core_name, category, detail or "")
        return category, detail

    try:
        core_name = vessel.parts.controlling.name
    except Exception:
        core_name = None
    if core_name:
        learned_category, learned_detail = db.get_core_role_default(core_name)
        if learned_category:
            # Write it back onto the core's own tag too, same as if it had
            # been tagged in-game/from the dashboard -- keeps the core tag
            # authoritative and visible in the in-game Role cycler, rather
            # than this auto-classification being an invisible side-channel.
            try:
                vessel.parts.controlling.tag = (
                    f"{learned_category}.{learned_detail}" if learned_detail else learned_category
                )
            except Exception:
                pass
            return learned_category, learned_detail

    return _KSP_TYPE_MAP.get(vessel.type.name, "unknown"), None


class VesselRegistry:
    def __init__(self, krpc_client):
        self._client = krpc_client

    @property
    def is_connected(self):
        """Whether kRPC is currently reachable. Exposed here so callers
        (the telemetry websocket) don't have to reach through to the
        private client handle to find out."""
        return self._client.is_connected

    def sync(self):
        """Scan every vessel kRPC currently knows about, register any new
        ones, and re-apply each vessel's core tag as its type (authoritative
        every time, so re-tagging in-game takes effect immediately). Cheap
        to call frequently (e.g. once per telemetry tick)."""
        sc = self._client.space_center
        for key, vessel in _keyed_vessels(sc.vessels):
            resolved_type, _ = _resolve_type(vessel)
            if db.get(key) is None:
                db.upsert_seen(key, default_name=vessel.name)
                logger.info("Registered new vessel %s as %s", key, resolved_type)
            else:
                db.upsert_seen(key, default_name=vessel.name)
            db.set_type(key, resolved_type)

    def list_vessels(self, include_telemetry=True):
        from backend import telemetry as telemetry_mod

        if not self._client.is_connected:
            return []

        try:
            sc = self._client.space_center
            self.sync()
            keyed = list(_keyed_vessels(sc.vessels))
        except NotConnected:
            return []
        except Exception as exc:
            # kRPC is connected but a scene transition or vessel
            # destruction/staging event mid-call left something stale
            # (e.g. "No such vessel <guid>", or a scene-restricted
            # procedure). Skip this tick rather than killing the whole
            # telemetry stream -- the next tick will have a fresh list.
            logger.warning("list_vessels: transient error, skipping this tick: %s", exc)
            return []

        rows = db.all_rows()
        result = []
        for key, vessel in keyed:
            try:
                meta = rows.get(key, {})
                _, role_detail = _resolve_type(vessel)
                entry = {
                    "id": key,
                    "name": meta.get("name", vessel.name),
                    "ksp_name": vessel.name,
                    "type": meta.get("type", "unknown"),
                    "role_detail": role_detail,
                    "notes": meta.get("notes", ""),
                    "is_active": vessel == sc.active_vessel,
                    "roles": parts.get_role_summary(vessel),
                }
            except Exception as exc:  # vessel was destroyed between listing and reading it
                logger.warning("list_vessels: skipping vessel %s, went stale: %s", key, exc)
                continue
            if include_telemetry:
                try:
                    entry["telemetry"] = telemetry_mod.get_telemetry(vessel)
                except Exception as exc:  # vessel may be on rails / out of physics range
                    entry["telemetry"] = {"error": str(exc)}
            result.append(entry)
        return result

    def get_vessel_object(self, vessel_id: str):
        sc = self._client.space_center
        for key, vessel in _keyed_vessels(sc.vessels):
            if key == vessel_id:
                return vessel
        return None

    def rename(self, vessel_id: str, name: str):
        if db.get(vessel_id) is None:
            raise KeyError(f"unknown vessel id {vessel_id}")
        db.set_name(vessel_id, name)
