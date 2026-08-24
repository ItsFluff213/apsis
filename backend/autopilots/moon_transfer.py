"""Transfer from a parking orbit to one of the same parent body's own moons
(e.g. Kerbin -> Mun or Minmus) -- a patched-conic transfer computed and
executed entirely in-house.

Simpler than the planet-to-planet case in planet_transfer.py: the vessel
never leaves its parent body's sphere of influence, it just raises apoapsis
until the moon's gravity catches it. That means a prograde burn at any
periapsis works, and there is no ejection angle to solve for -- only *when*
to burn, not *where*.

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

from backend import orbital
from backend.autopilots import arrival, maneuver

# The vector/two-body math these used to define privately now lives in
# backend/orbital.py, shared with planet_transfer.py and unit-tested there
# (tests/test_orbital.py). Aliased rather than renamed at every call site
# so the transfer logic below stays diff-able against its live-tested form.
_angle_of = orbital.angle_of
_cross = orbital.cross
_dot = orbital.dot
_norm = orbital.norm
_rotate_about_axis = orbital.rotate_about_axis


def compute_direct_transfer_plan(client, vessel, parent, moon):
    """Pure calculation, no burn -- the closed-form direct-intercept math,
    factored out so it can be used both to actually build the maneuver node
    (see _plan_direct_transfer below) and to preview the planned trajectory
    (e.g. for a dashboard visualization) without touching the game state.

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

    Returns a dict: burn_ut, arrival_ut, target_apoapsis_m, r_peri,
    periapsis_hat, normal (both as (x,y,z) tuples in parent's
    non_rotating_reference_frame) -- everything needed to both build the
    node and to draw the planned ellipse.
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

    first_burn_ut = sc.ut + o.time_to_periapsis
    parking_period = o.period
    r_peri = parent.equatorial_radius + o.periapsis_altitude
    r2 = moon.orbit.semi_major_axis

    # There are two independent knobs here, not one. Which arrival time to
    # target (moon at target_angle) only recurs once per full moon orbit --
    # a coarse knob. But WHEN to actually burn doesn't have to be the very
    # next periapsis: waiting a few extra laps of the current parking orbit
    # before burning is free (pure coasting) and shifts the transfer time
    # in much finer steps (one parking-orbit period at a time, typically
    # tiny compared to the moon's own period). Using only the coarse knob
    # (as an earlier version of this function did) picks whichever
    # once-per-moon-orbit arrival is "closest" to a normal transfer
    # duration, but "closest" among options spaced a full moon-orbit apart
    # can still leave the resulting apoapsis millions of meters away from
    # the moon's actual orbital radius -- confirmed live, a transfer aimed
    # for a 19,300 km apoapsis when the moon only orbits at 12,000 km,
    # missing it entirely despite hitting the angle exactly on time. Using
    # both knobs together, for each coarse arrival-time candidate, picks
    # the number of extra parking-orbit laps that makes the resulting
    # apoapsis land as close as possible to the moon's real orbital radius.
    #
    # Among candidates that are all "good enough" (comfortably inside the
    # moon's own sphere of influence, not just closest in the abstract),
    # prefer whichever is both fast AND cheap -- not just whichever needs
    # the fewest extra laps. The very best-accuracy candidate can require
    # waiting for a specific alignment that's a long real-world wait away
    # (confirmed live: one legitimate candidate needed ~56 hours of game
    # time, ~68 real minutes even at 50x warp) when a slightly-less-
    # perfect-but-still-safely-inside-the-SOI candidate was available much
    # sooner -- and different candidates can also cost noticeably
    # different amounts of dv for the departure burn itself, since they
    # target different apoapsis values. Score each qualifying candidate on
    # both (wait time, burn dv), normalized against the best of each among
    # the pool so neither unit dominates by scale, and pick the lowest
    # combined score.
    a1 = o.semi_major_axis
    v1 = maneuver.vis_viva_speed(mu, r_peri, a1)
    good_enough_m = moon.sphere_of_influence * 0.3
    candidates = []
    for k in range(8):
        angle_needed = (target_angle - moon_angle_now) % (2 * math.pi) + k * 2 * math.pi
        arrival_ut = sc.ut + angle_needed / moon_rate

        a_ideal = (r_peri + r2) / 2.0
        ideal_transfer_time = math.pi * math.sqrt(a_ideal ** 3 / mu)
        desired_burn_ut = arrival_ut - ideal_transfer_time
        if desired_burn_ut < first_burn_ut:
            continue  # would mean burning before the next periapsis even occurs

        laps = round((desired_burn_ut - first_burn_ut) / parking_period)
        burn_ut = first_burn_ut + laps * parking_period
        transfer_time = arrival_ut - burn_ut
        if transfer_time <= 0:
            continue
        new_period = 2 * transfer_time
        a2 = (mu * new_period ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)
        r_apo = 2 * a2 - r_peri
        if r_apo <= r_peri:
            continue  # degenerate for this k -- not a valid outward transfer
        miss = abs(r_apo - r2)  # how far the resulting apoapsis misses the moon's real orbit
        v2 = maneuver.vis_viva_speed(mu, r_peri, a2)
        dv = abs(v2 - v1)
        wait = burn_ut - sc.ut
        candidates.append((miss, r_apo, arrival_ut, burn_ut, dv, wait))

    if not candidates:
        raise ValueError(f"could not find a valid direct transfer window to {moon.name}")

    good = [c for c in candidates if c[0] <= good_enough_m]
    pool = good if good else candidates
    max_wait = max(c[5] for c in pool) or 1.0
    max_dv = max(c[4] for c in pool) or 1.0
    if good:
        best = min(pool, key=lambda c: c[5] / max_wait + c[4] / max_dv)
    else:
        best = min(pool, key=lambda c: c[0])  # none good enough -- fall back to best accuracy
    _, r_apo, arrival_ut, burn_ut, _, _ = best
    return {
        "burn_ut": burn_ut,
        "arrival_ut": arrival_ut,
        "target_apoapsis_m": r_apo - parent.equatorial_radius,
        "r_peri": r_peri,
        "periapsis_hat": periapsis_hat,
        "normal": normal,
    }


def _plan_direct_transfer(client, vessel, job, parent, moon):
    """Builds and returns the actual maneuver node for the direct transfer
    computed by compute_direct_transfer_plan (see there for the math).

    The plan's burn_ut can be several parking-orbit laps in the future, not
    necessarily the very next periapsis -- change_apoapsis_node always
    targets "the next periapsis from right now", so if the plan calls for
    waiting, warp there first so that the next periapsis actually is the
    intended one by the time the node gets created."""
    sc = client.space_center
    plan = compute_direct_transfer_plan(client, vessel, parent, moon)

    # A long wait spent entirely in a low, near-circular parking orbit is
    # slow in real time even at max warp -- KSP throttles rails-warp speed
    # by current altitude, and a flat ~90km orbit never climbs high enough
    # to unlock the faster tiers, confirmed live capping a wait at 50x the
    # whole way through. Raising apoapsis first (leaving periapsis alone,
    # so it doesn't change what change_apoapsis_node needs for the real
    # transfer burn later) means most of the wait is spent much higher up,
    # where far faster warp is available -- then recompute the plan, since
    # the parking orbit's period (and therefore the lap-based timing
    # search) changed.
    wait_s = plan["burn_ut"] - sc.ut
    if wait_s > 600 and vessel.orbit.apoapsis_altitude < 500_000:
        # Scale how high to raise with how long the wait actually is --
        # a bigger wait justifies paying for a bigger (still cheap from
        # LKO) raise to unlock a faster warp tier; a short wait doesn't
        # need it. A higher apoapsis also means a longer parking-orbit
        # period, which coarsens the lap-based timing search below, so
        # this isn't "raise as high as possible" -- just as high as the
        # wait length actually justifies.
        if wait_s > 36_000:
            raise_target = 5_000_000
        elif wait_s > 3_600:
            raise_target = 2_000_000
        else:
            raise_target = 800_000
        job.message = f"raising orbit for faster warp before the {moon.name} transfer window"
        raise_node = maneuver.change_apoapsis_node(client, vessel, raise_target, burn_at="periapsis")
        maneuver.execute_node(client, vessel, job, raise_node)
        plan = compute_direct_transfer_plan(client, vessel, parent, moon)

    if plan["burn_ut"] - sc.ut > 30:
        job.message = f"waiting for the right lap before the {moon.name} transfer burn"
        # sc.warp_to() blocks server-side until it reaches its target --
        # for a long wait (confirmed live: one legitimate plan needed
        # ~56 hours of game time) that meant job.check_abort() was never
        # reached until the whole wait finished, making the job
        # un-abortable for the entire duration. Warp in bounded chunks
        # instead so abort actually gets checked along the way.
        target = plan["burn_ut"] - 20
        chunk = 3600 * 6  # up to 6 game-hours per warp_to call
        while sc.ut < target - 30:
            job.check_abort()
            sc.warp_to(min(sc.ut + chunk, target))
        while sc.ut < plan["burn_ut"] - 5:
            job.check_abort()
            job.sleep(0.2)

    job.message = f"burning for direct {moon.name} intercept"
    return maneuver.change_apoapsis_node(client, vessel, plan["target_apoapsis_m"], burn_at="periapsis")


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

    # Mid-course correction: any real burn takes actual time to execute
    # (not the idealized instant impulse the closed-form plan assumes),
    # and small drift accumulates over a coast this long -- reported live
    # as "the calculation is always a little off". Rather than trying to
    # force the very first burn to be perfect (impossible in practice),
    # use the coast itself: partway to the encounter, re-derive the ideal
    # apoapsis from the vessel's actual current trajectory (not the
    # original plan) and nudge toward it if it's drifted. Same idea Apollo
    # used -- a planned correction burn, not a single all-or-nothing shot.
    frame = parent.non_rotating_reference_frame
    r_now = vessel.position(frame)
    v_now = vessel.velocity(frame)
    normal_now = _norm(_cross(r_now, v_now))
    r_hat_now = _norm(r_now)
    periapsis_hat_now = _norm(_rotate_about_axis(r_hat_now, normal_now, -vessel.orbit.true_anomaly))
    arrival_dir = tuple(-c for c in periapsis_hat_now)
    target_angle = _angle_of(arrival_dir)

    halfway_ut = sc.ut + vessel.orbit.time_to_apoapsis / 2.0
    if halfway_ut - sc.ut > 30:
        job.message = f"coasting to {moon.name} (correction point ahead)"
        sc.warp_to(halfway_ut - 10)
        while sc.ut < halfway_ut:
            job.check_abort()
            job.sleep(0.2)

        job.message = f"checking course toward {moon.name}"
        mu = parent.gravitational_parameter
        moon_angle_now = _angle_of(moon.position(frame))
        moon_rate = 2 * math.pi / moon.orbit.period
        r_peri = parent.equatorial_radius + vessel.orbit.periapsis_altitude

        angle_needed = (target_angle - moon_angle_now) % (2 * math.pi)
        ideal_arrival_ut = sc.ut + angle_needed / moon_rate
        ideal_transfer_time = ideal_arrival_ut - sc.ut
        # Solve for the semi-major axis whose remaining time-to-apoapsis
        # from here matches ideal_transfer_time, treating "now" as
        # approximately a fresh periapsis-side reference -- reasonable
        # since the correction point was deliberately chosen as the
        # halfway mark, so "half the new period remaining" is a sound
        # approximation here the same way it was for the original burn.
        a_target = (mu * (2 * max(ideal_transfer_time, 1.0)) ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)
        r_apo_target = 2 * a_target - r_peri
        current_apo = parent.equatorial_radius + vessel.orbit.apoapsis_altitude
        drift = abs(r_apo_target - current_apo)
        if drift > moon.sphere_of_influence * 0.1 and r_apo_target > r_peri:
            job.message = f"mid-course correction toward {moon.name}"
            node = maneuver.adjust_other_apsis_now(client, vessel, r_apo_target - parent.equatorial_radius)
            maneuver.execute_node(client, vessel, job, node)

    job.message = f"coasting to {moon.name}"
    # Warp in bounded chunks, watching actual live distance to the moon,
    # instead of one single warp_to based on the Kerbin-orbit apoapsis
    # time. That estimate is only a proxy for when the real encounter
    # happens -- the actual closest approach can occur meaningfully
    # earlier or later -- and a single big warp can fly straight through
    # SOI entry, closest approach (including a surface impact), and exit
    # before the code ever gets a chance to check anything. Confirmed
    # live: this destroyed a real test vessel. Stop warping well outside
    # the moon's SOI and fall back to fine real-time polling for the
    # final approach, so the safety check below has an actual chance to
    # run before arrival, not just before an already-passed encounter.
    frame = parent.non_rotating_reference_frame

    def _distance_to_moon():
        vp = vessel.position(frame)
        mp = moon.position(frame)
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(vp, mp)))

    safety_radius = moon.sphere_of_influence * 3
    while vessel.orbit.body != moon and _distance_to_moon() > safety_radius:
        job.check_abort()
        remaining = vessel.orbit.time_to_apoapsis
        if remaining > 60:
            sc.warp_to(sc.ut + min(remaining - 30, 300))
        else:
            job.sleep(1)

    while vessel.orbit.body != moon:
        job.check_abort()
        job.sleep(0.2)

    # Arrival: safety check, capture if the approach is still hyperbolic,
    # shape periapsis, circularize, optional plane change. Identical to what
    # a planet arrival needs, so it lives in arrival.py and is shared with
    # planet_transfer.py -- every live-testing fix in that sequence applies
    # to both.
    arrival.capture_and_circularize(
        client, vessel, job, moon, target_periapsis_m, target_inclination_deg,
    )

    job.message = f"arrived at {moon.name}"
