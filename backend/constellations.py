"""Constellation business logic: geostationary altitude, and slotting a
new satellite evenly into a constellation regardless of when it launched.
"""

import math

from backend import db


def geostationary_altitude_m(body):
    """Altitude of a circular equatorial orbit whose period matches the
    body's own rotation -- a satellite there holds a fixed longitude."""
    t = body.rotational_period
    mu = body.gravitational_parameter
    r = (mu * t ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)
    return r - body.equatorial_radius


def target_altitude_and_inclination(constellation, body):
    if constellation["kind"] == "communications":
        return geostationary_altitude_m(body), 0.0
    return constellation["altitude_m"], constellation["inclination_deg"]


def _longitude_of(body, vessel):
    """Body-fixed longitude (degrees) of a vessel's current position --
    used for communications (geostationary) slotting, where satellites
    are conventionally described by a fixed longitude."""
    return vessel.flight(body.reference_frame).longitude


def compute_target_slot_deg(client, registry, constellation, new_vessel_id):
    """Where (in degrees, 0-360) the new satellite should end up relative
    to the constellation's other members, so they end up evenly spaced.
    For communications constellations this is a body-fixed longitude; for
    custom constellations it's a relative angular position (true anomaly)
    within the shared orbit. Returns None if there are no other members
    yet (any slot is fine)."""
    sc = client.space_center
    body = None
    for b in sc.bodies.values():
        if b.name == constellation["body"]:
            body = b
            break
    if body is None:
        raise ValueError(f"unknown body {constellation['body']!r}")

    other_positions = []
    for vessel_id in constellation["members"]:
        if vessel_id == new_vessel_id:
            continue
        vessel = registry.get_vessel_object(vessel_id)
        if vessel is None:
            continue
        if constellation["kind"] == "communications":
            other_positions.append(_longitude_of(body, vessel) % 360)
        else:
            other_positions.append(math.degrees(vessel.orbit.true_anomaly) % 360)

    if not other_positions:
        return 0.0

    other_positions.sort()
    n = len(other_positions)
    # Find the largest gap between consecutive members (wrapping around
    # 360) and target its midpoint.
    best_gap = -1
    best_mid = 0.0
    for i in range(n):
        a = other_positions[i]
        b = other_positions[(i + 1) % n] + (360 if i == n - 1 else 0)
        gap = b - a
        if gap > best_gap:
            best_gap = gap
            best_mid = (a + gap / 2.0) % 360
    return best_mid
