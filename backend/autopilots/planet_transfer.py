"""Transfer from a parking orbit around one planet to another planet --
window search, ejection burn, coast, and capture, all computed in-house.

This replaces the old workflow, which had no transfer math at all: the user
computed a trajectory on the external KSP-MGA-Planner website, pasted its
text output into the dashboard, and this project merely replayed the burns
it listed. Everything here is native.

Relationship to moon_transfer.py: same patched-conic philosophy, one level
up the hierarchy (Sun as the parent, planets as the bodies), and the same
structure -- find a window, burn once, correct mid-course, capture on
arrival (shared with the moon case via arrival.py). But a planet departure
adds a problem a moon departure doesn't have. Leaving Kerbin for Mun, the
vessel never really escapes Kerbin; it just raises apoapsis until Mun's
gravity catches it, so a prograde burn at any periapsis works. Leaving
Kerbin for Duna, the vessel must escape Kerbin entirely and arrive in solar
orbit travelling in a *specific direction* -- which means the burn has to
happen at a specific point in the parking orbit, not merely at a specific
time. That is the ejection angle, and getting it wrong is the classic way
to depart with a textbook-perfect delta-v and still miss the planet by tens
of millions of kilometres. See orbital.ejection_angle.

Documented scope: direct Hohmann-class transfers between planets whose
orbits are treated as coplanar and near-circular. No Lambert solver, no
multi-revolution search, no gravity-assist chaining -- if you want a
Kerbin-Eve-Eve-Jool grand tour, that is genuinely a different (and much
larger) problem than this solves, and MGA-Planner remains better at it.
For the ordinary case of "send this probe to Duna", this is a complete
solution and needs no external tool. Real orbits do have eccentricity and
a degree or two of relative inclination, which is exactly what the
mid-course correction absorbs.
"""

import math

from backend import orbital
from backend.autopilots import arrival, maneuver

# How much of the coast to complete before the mid-course correction. Late
# enough that accumulated drift is measurable, early enough that fixing it
# is still cheap.
CORRECTION_AT_FRACTION = 0.5

# Only bother correcting if the predicted arrival is off by more than this
# fraction of the target's SOI -- below it, the burn costs more than the
# error is worth.
CORRECTION_THRESHOLD_SOI_FRACTION = 0.25

WARP_CHUNK_S = 3600 * 24  # up to a game-day per warp_to call, so abort stays responsive


def _planet_of(vessel):
    """The planet the vessel is currently around, and the star it orbits.

    Handles being in orbit around a *moon* by walking up: a vessel at Mun
    departing for Duna still departs from the Kerbin system.
    """
    body = vessel.orbit.body
    if body.orbit is None:
        raise ValueError(
            f"vessel is orbiting {body.name}, which orbits nothing -- "
            f"an interplanetary transfer has to start from a planet or moon"
        )
    star = body.orbit.body
    if star.orbit is None:
        return body, star  # body orbits the star directly: it is a planet
    # body orbits something that orbits the star -- so body is a moon.
    return star, star.orbit.body


def compute_transfer_plan(client, vessel, origin, target):
    """Pure calculation, no burn: when to leave and how hard.

    Factored out so the dashboard can preview a transfer (cost, wait,
    travel time) without touching game state, the same way
    moon_transfer.compute_direct_transfer_plan is used.

    Returns a dict with departure_ut, arrival_ut, transfer_time_s,
    v_infinity, ejection_dv, ejection_angle_rad, escape_direction, and the
    heliocentric radii involved.
    """
    sc = client.space_center
    star = origin.orbit.body
    mu_star = star.gravitational_parameter
    star_frame = star.non_rotating_reference_frame

    r1 = origin.orbit.semi_major_axis
    r2 = target.orbit.semi_major_axis
    _, transfer_time, dv_heliocentric, _ = orbital.hohmann_transfer(mu_star, r1, r2)
    required_phase = orbital.hohmann_phase_angle(mu_star, r1, r2)

    # --- When is the window? ---
    origin_angle = orbital.angle_of(origin.position(star_frame))
    target_angle = orbital.angle_of(target.position(star_frame))
    current_phase = target_angle - origin_angle

    rate_origin = 2 * math.pi / origin.orbit.period
    rate_target = 2 * math.pi / target.orbit.period
    relative_rate = rate_target - rate_origin
    if abs(relative_rate) < 1e-12:
        raise ValueError(
            f"{origin.name} and {target.name} have the same orbital period -- "
            f"their relative geometry never changes, so no transfer window exists"
        )

    # Solve relative_rate * t == (required_phase - current_phase) mod 2pi
    # for the smallest t >= 0. The modulo has to be taken in the direction
    # the phase angle actually drifts, hence the sign split: an inner
    # target laps the vessel's planet (positive rate), an outer one falls
    # behind (negative rate), and using the wrong branch picks a window up
    # to a full synodic period away.
    delta = (required_phase - current_phase) % (2 * math.pi)
    wait = delta / relative_rate if relative_rate > 0 else (delta - 2 * math.pi) / relative_rate
    departure_ut = sc.ut + wait

    # --- How hard is the burn? ---
    # For a coplanar Hohmann the vessel must leave the origin's SOI moving
    # parallel to the origin's own heliocentric velocity, faster by
    # dv_heliocentric (outward) or slower by it (inward). That difference
    # is exactly the hyperbolic excess velocity -- what the vessel has left
    # once it has climbed out of the planet's gravity well.
    v_infinity = abs(dv_heliocentric)
    origin_velocity = origin.velocity(star_frame)
    prograde_hat = orbital.norm(origin_velocity)
    # Outward: escape along the planet's motion. Inward: escape backwards
    # along it, shedding heliocentric speed so the Sun pulls the vessel in.
    sign = 1.0 if dv_heliocentric >= 0 else -1.0
    escape_direction = tuple(sign * c for c in prograde_hat)

    mu_origin = origin.gravitational_parameter
    r_park = origin.equatorial_radius + vessel.orbit.periapsis_altitude
    v_eject = orbital.ejection_speed(mu_origin, r_park, v_infinity)
    v_park = orbital.vis_viva_speed(mu_origin, r_park, vessel.orbit.semi_major_axis)
    ejection_dv = v_eject - v_park
    nu_infinity = orbital.ejection_angle(mu_origin, r_park, v_infinity)

    return {
        "departure_ut": departure_ut,
        "arrival_ut": departure_ut + transfer_time,
        "transfer_time_s": transfer_time,
        "wait_s": wait,
        "v_infinity": v_infinity,
        "ejection_dv": ejection_dv,
        "ejection_angle_rad": nu_infinity,
        "escape_direction": escape_direction,
        "dv_heliocentric": dv_heliocentric,
        "required_phase_rad": required_phase,
        "r_origin": r1,
        "r_target": r2,
    }


def _build_ejection_node(client, vessel, origin, plan):
    """Create the departure node at the correct *place* in the parking
    orbit, not merely at the correct time.

    The vessel must be at the escape hyperbola's periapsis when it burns.
    The outgoing asymptote sits `ejection_angle` further around the orbit
    from that periapsis, so the burn point is that angle *behind* the
    direction the vessel needs to finally escape along.
    """
    sc = client.space_center
    frame = origin.non_rotating_reference_frame

    r_vessel = vessel.position(frame)
    v_vessel = vessel.velocity(frame)
    normal = orbital.norm(orbital.cross(r_vessel, v_vessel))

    escape_direction = sc.transform_direction(
        plan["escape_direction"], origin.orbit.body.non_rotating_reference_frame, frame,
    )
    # Rotate backwards along the direction of travel from the asymptote to
    # find periapsis.
    burn_point = orbital.rotate_about_axis(escape_direction, normal, -plan["ejection_angle_rad"])
    burn_angle = orbital.angle_of(burn_point)
    current_angle = orbital.angle_of(r_vessel)

    # angle_of measures counterclockwise about +y, but an orbit whose
    # normal points +y actually sweeps clockwise through that projection.
    # Without this the burn lands on the opposite side of the planet for
    # half of all parking orbits.
    direction = -1.0 if normal[1] > 0 else 1.0
    omega = 2 * math.pi / vessel.orbit.period
    time_to_burn_point = ((direction * (burn_angle - current_angle)) % (2 * math.pi)) / omega

    # Two knobs, as in moon_transfer: the window fixes roughly *when*, and
    # which parking-orbit lap to use fixes it finely. Pick the lap landing
    # closest to the window.
    first_burn_ut = sc.ut + time_to_burn_point
    parking_period = vessel.orbit.period
    laps = max(0, round((plan["departure_ut"] - first_burn_ut) / parking_period))
    burn_ut = first_burn_ut + laps * parking_period

    return vessel.control.add_node(burn_ut, prograde=plan["ejection_dv"])


def _warp_to(client, job, target_ut, message):
    """Warp forward in bounded chunks, checking abort between each.

    sc.warp_to() blocks server-side until it arrives. For an interplanetary
    coast (often hundreds of game-days) a single call would make the job
    un-abortable for its entire duration -- the same problem already fixed
    in moon_transfer for its much shorter waits.
    """
    sc = client.space_center
    job.message = message
    while sc.ut < target_ut - 30:
        job.check_abort()
        sc.warp_to(min(sc.ut + WARP_CHUNK_S, target_ut))
    while sc.ut < target_ut:
        job.check_abort()
        job.sleep(0.2)


def _mid_course_correction(client, vessel, job, star, target, plan):
    """Partway through the coast, re-aim using the vessel's *actual*
    trajectory rather than trusting the departure burn to have been exact.

    Necessary for the same reasons as in moon_transfer, only more so: a
    real burn takes finite time instead of being the instant impulse the
    plan assumes, the planets are not really coplanar or circular, and here
    the errors have hundreds of days to grow. Apollo did the same thing --
    a planned correction burn, not one all-or-nothing shot.
    """
    sc = client.space_center
    star_frame = star.non_rotating_reference_frame
    mu_star = star.gravitational_parameter

    correction_ut = plan["departure_ut"] + plan["transfer_time_s"] * CORRECTION_AT_FRACTION
    if correction_ut <= sc.ut:
        return
    _warp_to(client, job, correction_ut, f"coasting to {target.name} (correction point ahead)")

    job.message = f"checking course toward {target.name}"
    if vessel.orbit.body != star:
        return  # already captured somewhere, or never left -- nothing to correct

    # Where will the target be when the vessel reaches the target's orbital
    # radius, and where will the vessel be? Both reduce to an angle.
    r_target_orbit = target.orbit.semi_major_axis
    time_to_arrival = plan["arrival_ut"] - sc.ut
    if time_to_arrival <= 0:
        return

    target_angle_now = orbital.angle_of(target.position(star_frame))
    target_rate = 2 * math.pi / target.orbit.period
    target_angle_at_arrival = target_angle_now + target_rate * time_to_arrival

    # The vessel arrives at its own far apsis, which sits opposite its
    # periapsis -- a prograde-only burn never rotates the apsis line, so
    # this direction is fixed and known.
    vessel_r = vessel.position(star_frame)
    vessel_v = vessel.velocity(star_frame)
    normal = orbital.norm(orbital.cross(vessel_r, vessel_v))
    periapsis_hat = orbital.norm(
        orbital.rotate_about_axis(orbital.norm(vessel_r), normal, -vessel.orbit.true_anomaly)
    )
    arrival_angle = orbital.angle_of(tuple(-c for c in periapsis_hat))

    # Angular miss, converted to a distance at the target's orbit.
    angle_error = orbital.signed_angle_difference(target_angle_at_arrival, arrival_angle)
    miss_distance = abs(angle_error) * r_target_orbit
    if miss_distance < target.sphere_of_influence * CORRECTION_THRESHOLD_SOI_FRACTION:
        job.message = f"course to {target.name} is good, no correction needed"
        return

    # Fix it by retiming the arrival: adjust the far apsis so the vessel
    # gets there when the target does. Solve for the semi-major axis whose
    # remaining half-period equals the time until the target reaches the
    # arrival point.
    corrected_arrival_ut = sc.ut + orbital.signed_angle_difference(
        arrival_angle, target_angle_now
    ) % (2 * math.pi) / target_rate
    remaining = corrected_arrival_ut - sc.ut
    if remaining <= 0:
        return
    a_target = orbital.sma_for_period(mu_star, 2 * remaining)
    r_apo_target = 2 * a_target - (star.equatorial_radius + vessel.orbit.periapsis_altitude)
    if r_apo_target <= 0:
        return

    job.message = f"mid-course correction toward {target.name} (~{miss_distance / 1e6:.1f} Mm off)"
    node = maneuver.adjust_other_apsis_now(client, vessel, r_apo_target - star.equatorial_radius)
    maneuver.execute_node(client, vessel, job, node)


def run_planet_transfer(client, vessel, job, target_body_name, target_periapsis_m,
                        target_inclination_deg=None):
    """Fly the whole transfer: wait for the window, eject, coast, correct,
    capture, circularize.

    target_inclination_deg is relative to the DESTINATION planet -- it
    describes the final orbit there, not the departure parking orbit.
    """
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    if sc.rails_warp_factor != 0 or sc.physics_warp_factor != 0:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
        job.sleep(1.0)

    origin, star = _planet_of(vessel)
    target = next((b for b in star.satellites if b.name == target_body_name), None)
    if target is None:
        raise ValueError(f"{target_body_name!r} is not a planet orbiting {star.name}")
    if target == origin:
        raise ValueError(f"already at {target.name}")

    if vessel.orbit.body != origin:
        raise ValueError(
            f"vessel is orbiting {vessel.orbit.body.name}, not {origin.name} -- "
            f"return to a {origin.name} parking orbit before starting an interplanetary transfer"
        )

    plan = compute_transfer_plan(client, vessel, origin, target)

    # --- Wait for the window ---
    # Leave a full parking orbit of slack so the ejection point can still
    # be found and burned at once we get there.
    if plan["wait_s"] > vessel.orbit.period:
        wait_days = plan["wait_s"] / 21600
        _warp_to(
            client, job, plan["departure_ut"] - vessel.orbit.period,
            f"waiting {wait_days:.0f} days for the {target.name} transfer window",
        )
        # Geometry moved while we warped; re-derive against reality.
        plan = compute_transfer_plan(client, vessel, origin, target)

    # --- Ejection burn ---
    job.message = (
        f"ejecting for {target.name} "
        f"(~{plan['ejection_dv']:.0f} m/s, {plan['transfer_time_s'] / 21600:.0f} day cruise)"
    )
    node = _build_ejection_node(client, vessel, origin, plan)
    maneuver.execute_node(client, vessel, job, node)

    # --- Leave the origin's SOI ---
    job.message = f"climbing out of {origin.name}'s gravity well"
    while vessel.orbit.body == origin:
        job.check_abort()
        remaining = vessel.orbit.time_to_soi_change
        if remaining is not None and remaining > 60:
            sc.warp_to(sc.ut + min(remaining - 30, WARP_CHUNK_S))
        else:
            job.sleep(1)

    _mid_course_correction(client, vessel, job, star, target, plan)

    # --- Cruise ---
    # Watch for the SOI flip rather than trusting the predicted arrival
    # time. Warping past an encounter entirely is a confirmed way to
    # destroy a vessel (it happened on a Mun transfer), so this stops
    # warping well outside the target's SOI and polls finely from there.
    job.message = f"cruising to {target.name}"
    star_frame = star.non_rotating_reference_frame

    def distance_to_target():
        vp = vessel.position(star_frame)
        tp = target.position(star_frame)
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(vp, tp)))

    safety_radius = target.sphere_of_influence * 3
    while vessel.orbit.body != target:
        job.check_abort()
        if vessel.orbit.body != star:
            # Fell into some other body's SOI on the way -- an unplanned
            # encounter. Stop and report rather than silently running the
            # arrival sequence at the wrong planet.
            raise ValueError(
                f"unexpected encounter with {vessel.orbit.body.name} en route to {target.name} -- "
                f"aborting the transfer so it can be sorted out manually"
            )
        if distance_to_target() > safety_radius:
            remaining = vessel.orbit.time_to_soi_change
            if remaining is not None and remaining > 60:
                sc.warp_to(sc.ut + min(remaining - 30, WARP_CHUNK_S))
            else:
                sc.warp_to(sc.ut + 3600)
        else:
            job.sleep(0.5)

    # --- Arrival ---
    arrival.capture_and_circularize(
        client, vessel, job, target, target_periapsis_m, target_inclination_deg,
    )
    job.message = f"arrived at {target.name}"
