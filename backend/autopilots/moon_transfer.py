"""Transfer from a parking orbit to one of the same parent body's own moons
(e.g. Kerbin -> Mun or Minmus) -- a patched-conic transfer computed and
executed entirely in-house, no external planner needed. This is a
fundamentally different (and much simpler) problem than the interplanetary
case in backend/interplanetary.py: it's a single-parent phase-angle
transfer, not a multi-gravity-assist search, which is exactly why
KSP-MGA-Planner refuses it ("origin and destination must orbit the same
body" -- Mun orbits Kerbin, not the Sun, so it's out of that tool's scope
entirely).

Sequence: compute (in closed form, see _plan_direct_transfer) the single
burn -- timing and size -- that sends the vessel directly toward wherever
the moon will actually be, burn it, coast until the moon's gravity actually
captures the vessel (kRPC's vessel.orbit.body flips to the moon on its own
once inside its sphere of influence), run a couple of safety/shaping checks
on arrival, then circularize and optionally rotate to a target inclination.
Not a precision solver (it's still a 2-body patched-conic approximation,
ignoring the moon's own small eccentricity/inclination and any 3-body
effects), but it's an exact solution *within* that approximation -- one
calculated burn aimed at an actual future encounter, not a coarse angular
match followed by hoping the moon's gravity sorts out the rest.
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


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    m = math.sqrt(sum(c * c for c in a)) or 1.0
    return tuple(c / m for c in a)


def _rotate_about_axis(vec, axis, angle):
    """Rodrigues' rotation formula."""
    axis = _norm(axis)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    cross_term = _cross(axis, vec)
    dot_term = _dot(axis, vec) * (1 - cos_a)
    return tuple(vec[i] * cos_a + cross_term[i] * sin_a + axis[i] * dot_term for i in range(3))


def _plan_direct_transfer(client, vessel, job, parent, moon):
    """Computes the single burn (at the vessel's next periapsis) that sends
    it directly toward the moon, in closed form -- no search/wait loop.

    Key fact this relies on: a purely prograde burn at periapsis never
    rotates the orbit's apsis line, so no matter which periapsis passage we
    burn at or how big the burn is, the resulting apoapsis always lands in
    the exact same fixed direction in space (opposite the current periapsis
    direction). That collapses "when do we depart" and "how do we aim" into
    one simple question: at what future time does the moon's own angular
    position match that fixed direction? Once we know that arrival time, the
    exact apoapsis needed to arrive precisely then follows directly from
    Kepler's third law. One calculation, one burn, no loitering in a wide
    Kerbin orbit hoping a coarse angular match happens to be close enough --
    which was the old approach, and why it could end up "aimed somewhere at
    the moon's orbit" instead of at the moon itself.
    """
    sc = client.space_center
    frame = parent.non_rotating_reference_frame
    mu = parent.gravitational_parameter
    o = vessel.orbit

    r_now = vessel.position(frame)
    v_now = vessel.velocity(frame)
    normal = _norm(_cross(r_now, v_now))
    r_hat_now = _norm(r_now)
    periapsis_hat = _norm(_rotate_about_axis(r_hat_now, normal, -o.true_anomaly))
    arrival_dir = tuple(-c for c in periapsis_hat)
    target_angle = _angle_of(arrival_dir)

    moon_angle_now = _angle_of(moon.position(frame))
    moon_rate = 2 * math.pi / moon.orbit.period

    burn_ut = sc.ut + o.time_to_periapsis
    r_peri = parent.equatorial_radius + o.periapsis_altitude

    # Nominal Hohmann half-period, used only to pick the most sensible
    # candidate arrival time among the several that satisfy the angle
    # match (one every lunar orbit) -- prefer whichever is closest to a
    # normal transfer duration rather than a wildly fast or slow one.
    r2 = moon.orbit.semi_major_axis
    a_nominal = (r_peri + r2) / 2.0
    nominal_transfer_time = math.pi * math.sqrt(a_nominal ** 3 / mu)

    best = None
    for k in range(6):
        angle_needed = (target_angle - moon_angle_now) % (2 * math.pi) + k * 2 * math.pi
        arrival_ut = sc.ut + angle_needed / moon_rate
        transfer_time = arrival_ut - burn_ut
        if transfer_time <= 0:
            continue
        new_period = 2 * transfer_time
        a2 = (mu * new_period ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)
        r_apo = 2 * a2 - r_peri
        if r_apo <= r_peri:
            continue  # degenerate for this k -- not a valid outward transfer
        score = abs(transfer_time - nominal_transfer_time)
        if best is None or score < best[0]:
            best = (score, r_apo)

    if best is None:
        raise ValueError(f"could not find a valid direct transfer window to {moon.name}")

    target_apoapsis_m = best[1] - parent.equatorial_radius
    job.message = f"burning for direct {moon.name} intercept"
    return maneuver.change_apoapsis_node(client, vessel, target_apoapsis_m, burn_at="periapsis")


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

    node = _plan_direct_transfer(client, vessel, job, parent, moon)
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
