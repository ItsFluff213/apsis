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


def _wait_for_transfer_window(client, vessel, job, parent, moon, max_miss_fraction=0.35, max_synodic_waits=6):
    """Warps/waits until the vessel's phase relative to the moon matches
    what's needed so a Hohmann-style transfer arrives when the moon is
    actually there -- and keeps waiting through additional orbits (as many
    synodic periods as it takes, up to max_synodic_waits) rather than
    settling for the first mathematically-close-enough moment.

    The old version accepted any window within a flat 0.5 degree angular
    tolerance, regardless of distance -- at Mun's orbital radius that's a
    ~100km miss, which is not automatically "clean" relative to a specific
    moon's actual SOI size. Instead this converts the angular error into an
    actual predicted miss distance at the moon's orbital radius (arc length
    approximation, valid for the small angles we're accepting) and requires
    it to be comfortably inside the moon's own sphere of influence -- with
    max_miss_fraction as the safety margin (0.35 leaves room for the
    existing capture-burn fallback to still work if arrival isn't dead
    center). Since the exact required phase relationship recurs every
    synodic period in this simplified model, "waiting longer" means holding
    out for a tighter angular match, not a geometrically different window.
    """
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

    synodic_period = (2 * math.pi) / abs(relative_rate)
    max_miss_m = moon.sphere_of_influence * max_miss_fraction
    max_angle_err = max_miss_m / r2  # small-angle: miss distance ~= r2 * angle error

    job.message = f"waiting for transfer window to {moon.name}"
    synodic_waits = 0
    while True:
        # Warp most of the way there based on the current estimate, then
        # fine-tune with short waits close in -- avoids both a razor-
        # precision closed-form wait (fragile) and a slow real-time crawl.
        for _ in range(200):
            job.check_abort()
            err = signed_error()
            if abs(err) < max_angle_err:
                return
            dt = err / relative_rate
            if dt < 0:
                dt += synodic_period
            if dt > 20:
                sc.warp_to(sc.ut + dt - 15)
                job.sleep(0.5)
            else:
                job.sleep(0.2)
        # Shouldn't normally fall out of the inner loop (it only exits via
        # the abs(err) check), but if the closed-form dt estimate keeps
        # overshooting for some reason, don't spin forever -- push forward
        # by a full synodic period and try converging again, up to
        # max_synodic_waits times.
        synodic_waits += 1
        if synodic_waits >= max_synodic_waits:
            job.message = f"could not find a clean transfer window to {moon.name} in time, using best available"
            return
        sc.warp_to(sc.ut + synodic_period)


def run_moon_transfer(client, vessel, job, moon_name, target_periapsis_m, target_inclination_deg=None):
    """target_inclination_deg is relative to the MOON once captured there,
    not to the parent body -- "polar" describes the final orbit around the
    moon, not the Kerbin-relative parking orbit used to get there. Applied
    as a separate plane-change burn after circularizing (see below); the
    transfer itself only ever burns prograde and can't change planes."""
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    parent = vessel.orbit.body
    moon = next((b for b in parent.satellites if b.name == moon_name), None)
    if moon is None:
        raise ValueError(f"{moon_name!r} is not a satellite of {parent.name}")

    # This phase-angle transfer only works from a parking orbit that's
    # roughly in the moon's own orbital plane (matching the "angle
    # projected onto the equatorial-ish plane" simplification _angle_of
    # relies on throughout). A steeply-inclined parking orbit (confirmed
    # live: 90 degrees / polar) makes that projection degenerate -- it
    # barely sweeps through any range of angles at all -- so the transfer
    # burn ends up aimed nowhere near the moon. A parking orbit inclination
    # this far from the moon's own is a sign the caller wanted a polar
    # orbit *around the moon*, which this function achieves separately
    # (see the plane-change burn near the end), not by launching polar.
    plane_mismatch_deg = abs(math.degrees(vessel.orbit.inclination) - math.degrees(moon.orbit.inclination))
    if plane_mismatch_deg > 20:
        raise ValueError(
            f"parking orbit inclination ({math.degrees(vessel.orbit.inclination):.1f} deg) is too far from "
            f"{moon.name}'s own orbital plane ({math.degrees(moon.orbit.inclination):.1f} deg) for this transfer "
            f"to work -- launch into a near-equatorial parking orbit instead, then pass target_inclination_deg "
            f"here if you want a polar (or any other inclined) orbit around {moon.name} itself."
        )

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

    # Safety check, before anything else: the phase-angle transfer only
    # controls WHEN the vessel arrives relative to the moon, not the exact
    # closest-approach distance (that needs real 3D targeting this simple
    # model doesn't do). A "clean" phase match could still line up a
    # near-direct hit on the moon's surface -- we want a slingshot/capture
    # around it, never straight into it. periapsis_altitude is well-defined
    # even for a hyperbolic flyby (unlike apoapsis), so check it the moment
    # SOI entry is detected, well before actually reaching that periapsis.
    min_safe_periapsis_m = max(target_periapsis_m * 0.5, moon.equatorial_radius * 0.05)
    if vessel.orbit.periapsis_altitude < min_safe_periapsis_m:
        job.message = f"correcting course -- raw arrival would pass too close to {moon.name}"
        node = maneuver.raise_periapsis_now(client, vessel, min_safe_periapsis_m * 1.5)
        maneuver.execute_node(client, vessel, job, node)

    # A phase-angle Hohmann-style transfer aims for a capture, but doesn't
    # precisely target a bound arrival -- entering the moon's SOI is often
    # still a HYPERBOLIC flyby (eccentricity >= 1), not an actual capture,
    # especially with only an approximate transfer window. A hyperbolic
    # orbit has no real apoapsis (kRPC reports a negative apoapsis_altitude
    # and a meaningless time_to_apoapsis for one), so the next step's
    # "burn at apoapsis" would be undefined -- confirmed live: this silently
    # produced a huge, wrong burn that flung a real vessel out of Kerbin's
    # SOI entirely into a solar orbit, on top of never reaching Mun. If
    # we're still on an escape trajectory, do a capture burn first: at the
    # flyby's periapsis (always well-defined, hyperbolic or not), lower
    # apoapsis down to something well inside the moon's SOI so the orbit
    # actually becomes bound before the shaping/circularizing burns below.
    if vessel.orbit.eccentricity >= 1:
        job.message = f"capturing at {moon.name} (arrival was a flyby, not a capture)"
        capture_apoapsis_m = max(target_periapsis_m * 4, moon.sphere_of_influence * 0.5)
        node = maneuver.change_apoapsis_node(client, vessel, capture_apoapsis_m, burn_at="periapsis")
        maneuver.execute_node(client, vessel, job, node)

    # Two burns, not one -- the capture orbit's own periapsis (wherever the
    # arrival trajectory happened to put it) is not target_periapsis_m, and
    # calling change_apoapsis_node(target_periapsis_m, burn_at="periapsis")
    # here (the old code) silently assumed it was: that treats
    # target_periapsis_m as a new APOAPSIS while leaving the actual arrival
    # periapsis untouched, which is nonsense whenever the arrival periapsis
    # doesn't happen to already be close to the target (confirmed live: a
    # real transfer's capture periapsis was nowhere near 30km, so "set
    # apoapsis to 30km, periapsis stays wherever it is" produced a huge,
    # wrong burn instead of a clean circular capture orbit). Correct
    # sequence: first pin periapsis to the target altitude by burning at
    # the (far, slow, cheap) capture apoapsis, then circularize by burning
    # at that new periapsis.
    job.message = f"shaping periapsis at {moon.name}"
    node = maneuver.change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis")
    maneuver.execute_node(client, vessel, job, node)

    job.message = f"circularizing at {moon.name}"
    node = maneuver.circularize_node(client, vessel, at="periapsis")
    maneuver.execute_node(client, vessel, job, node)

    if target_inclination_deg is not None:
        job.message = f"adjusting inclination around {moon.name}"
        node = maneuver.change_inclination_node(client, vessel, target_inclination_deg)
        maneuver.execute_node(client, vessel, job, node)

    job.message = f"arrived at {moon.name}"
