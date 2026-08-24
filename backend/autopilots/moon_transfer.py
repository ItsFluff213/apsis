"""Transfer from a parking orbit to one of the same parent body's own moons
(e.g. Kerbin -> Mun or Minmus) -- a patched-conic transfer computed and
executed entirely in-house, no external planner needed. This is a
fundamentally different (and much simpler) problem than the interplanetary
case in backend/interplanetary.py: it's a single-parent phase-angle
transfer, not a multi-gravity-assist search, which is exactly why
KSP-MGA-Planner refuses it ("origin and destination must orbit the same
body" -- Mun orbits Kerbin, not the Sun, so it's out of that tool's scope
entirely).

Sequence: wait for the correct transfer window (phase angle between the
vessel and the target moon), burn to raise apoapsis out to the moon's own
orbital radius, coast until the moon's gravity actually captures the vessel
(kRPC's vessel.orbit.body flips to the moon on its own once inside its
sphere of influence), then circularize at whatever periapsis the arrival
trajectory produced. Same "approximate, not a precision solver" spirit as
the rest of this project's maneuver planning -- the phase-angle targeting
gets you a real capture, not a millimeter-perfect one.
"""

import math

from backend.autopilots import maneuver


def _angle_of(position):
    """Angle (rad) of a position vector projected onto the parent's
    equatorial-ish plane (x/z, per kRPC's non_rotating_reference_frame
    convention) -- consistent with the same planar simplification the rest
    of this project's phase/plane-change math already uses."""
    x, _, z = position
    return math.atan2(z, x)


def _wait_for_transfer_window(client, vessel, job, parent, moon):
    """Warps/waits until the vessel's phase relative to the moon matches
    what's needed so a Hohmann-style transfer arrives when the moon is
    actually there."""
    sc = client.space_center
    frame = parent.non_rotating_reference_frame
    mu = parent.gravitational_parameter

    r1 = vessel.orbit.semi_major_axis
    r2 = moon.orbit.semi_major_axis
    a_transfer = (r1 + r2) / 2.0
    transfer_time = math.pi * math.sqrt(a_transfer ** 3 / mu)

    moon_rate = 2 * math.pi / moon.orbit.period
    vessel_rate = 2 * math.pi / vessel.orbit.period

    # At the moment of departure, the moon needs to be far enough ahead
    # that it arrives at the transfer's arrival point (180 deg around from
    # departure) exactly when the vessel does.
    required_phase = (math.pi - moon_rate * transfer_time) % (2 * math.pi)

    def phase_now():
        vp = vessel.position(frame)
        mp = moon.position(frame)
        return (_angle_of(mp) - _angle_of(vp)) % (2 * math.pi)

    def signed_error():
        return (phase_now() - required_phase + math.pi) % (2 * math.pi) - math.pi

    relative_rate = vessel_rate - moon_rate  # rad/s the phase closes at (vessel is normally faster/lower)
    if abs(relative_rate) < 1e-9:
        return  # degenerate (shouldn't happen for a real parking orbit vs. a real moon)

    job.message = f"waiting for transfer window to {moon.name}"
    # Warp most of the way there based on the current estimate, then
    # fine-tune with short waits close in -- avoids both a razor-precision
    # closed-form wait (fragile) and a slow real-time crawl the whole way.
    for _ in range(200):
        job.check_abort()
        err = signed_error()
        if abs(err) < math.radians(0.5):
            return
        dt = err / relative_rate
        if dt < 0:
            dt += (2 * math.pi) / abs(relative_rate)
        if dt > 20:
            sc.warp_to(sc.ut + dt - 15)
            job.sleep(0.5)
        else:
            job.sleep(0.2)


def run_moon_transfer(client, vessel, job, moon_name, target_periapsis_m):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    parent = vessel.orbit.body
    moon = next((b for b in parent.satellites if b.name == moon_name), None)
    if moon is None:
        raise ValueError(f"{moon_name!r} is not a satellite of {parent.name}")

    _wait_for_transfer_window(client, vessel, job, parent, moon)

    job.message = f"burning for {moon.name} transfer"
    target_apoapsis_m = moon.orbit.semi_major_axis - parent.equatorial_radius
    node = maneuver.change_apoapsis_node(client, vessel, target_apoapsis_m, burn_at="periapsis")
    maneuver.execute_node(client, vessel, job, node)

    job.message = f"coasting to {moon.name}"
    if vessel.orbit.time_to_apoapsis > 30:
        sc.warp_to(sc.ut + vessel.orbit.time_to_apoapsis - 20)
    while vessel.orbit.body != moon:
        job.check_abort()
        job.sleep(1)

    job.message = f"circularizing at {moon.name}"
    node = maneuver.change_apoapsis_node(client, vessel, target_periapsis_m, burn_at="periapsis")
    maneuver.execute_node(client, vessel, job, node)

    job.message = f"arrived at {moon.name}"
