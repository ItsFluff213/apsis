"""Maneuver node helpers: create nodes for common orbital changes and
execute any node (orient, warp, burn). Shared by every autopilot in this
package so the node-execution logic lives in one place; auto-staging during
a burn is delegated to backend/autopilots/staging.py, which the ascent climb
uses too.
"""

import math

from backend.autopilots import staging


def vis_viva_speed(mu, r, a):
    return math.sqrt(mu * (2.0 / r - 1.0 / a))


def estimate_burn_time(vessel, delta_v):
    """Tsiolkovsky-based estimate of how long a burn of this delta-v takes
    at the vessel's current mass/thrust/Isp."""
    isp = vessel.specific_impulse or 250
    g0 = 9.80665
    m0 = vessel.mass
    max_thrust = vessel.available_thrust or 1
    m1 = m0 / math.exp(delta_v / (isp * g0))
    flow_rate = max_thrust / (isp * g0)
    if flow_rate <= 0:
        return 1.0
    return (m0 - m1) / flow_rate


def circularize_node(client, vessel, at="apoapsis"):
    """Adds a node that circularizes the orbit at the next apoapsis or
    periapsis and returns it."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if at == "apoapsis":
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, r)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis"):
    """Adds a node (burned at the next apoapsis or periapsis) that changes
    the periapsis altitude to the given value. A negative target altitude
    is fine -- it's used for deorbit burns aimed at impact/atmosphere."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if burn_at == "apoapsis":
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_periapsis_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def adjust_other_apsis_now(client, vessel, target_altitude_m, lead_time=1.0):
    """Burns prograde right at the vessel's current position to set the
    *other* apsis (whichever one the vessel isn't currently at) to a given
    altitude -- the general version of "burn now" rather than waiting for
    an actual apsis passage. The vis-viva math doesn't care which apsis is
    which; this only cleanly hits the target if fired reasonably close to
    an actual apsis of the current orbit, since elsewhere the current
    radius isn't really "the other apsis" of the resulting orbit either.
    Two uses: an emergency collision-avoidance correction (raise periapsis
    now, imprecisely, rather than arrive at the real apoapsis after
    already flying too close to impact), and a mid-course correction
    partway through a coast (nudge the far apsis toward a freshly
    recomputed target instead of trusting the original burn to have been
    exact)."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    r = math.sqrt(sum(c * c for c in vessel.position(body.reference_frame)))
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_altitude_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(sc.ut + lead_time, prograde=v2 - v1)


def change_apoapsis_node(client, vessel, target_apoapsis_m, burn_at="periapsis"):
    """Adds a node (burned at the next periapsis or apoapsis) that changes
    the apoapsis altitude to the given value, leaving the other apsis
    where it is. Mirror of change_periapsis_node."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if burn_at == "periapsis":
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_apoapsis_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def change_inclination_node(client, vessel, target_inclination_deg):
    """Adds a node at the next ascending node (relative to the body's
    equatorial plane -- the same reference Orbit.inclination already uses)
    that changes the orbit's inclination to the target, leaving the orbit
    shape (speed, radius) otherwise unchanged.

    The delta-v is split between prograde and normal components -- not
    applied purely normal. A pure-normal burn of magnitude 2*v*sin(di/2)
    only approximates a plane rotation for *small* di: for a large change
    (confirmed live with a 90 degree change) it adds that much speed
    entirely sideways on top of the existing forward speed, making the
    resulting total speed *higher* than a correct plane change would --
    for close to a 90 degree change this pushed the resulting speed past
    local escape velocity, flinging a real test satellite out of Minmus's
    SOI entirely instead of just re-tilting its orbit. Splitting the burn
    into prograde*(cos(di)-1) and normal*sin(di) instead rotates the
    velocity vector while preserving its magnitude exactly, for any angle,
    which is what a plane-change-only maneuver actually requires.

    A plane change is priced by how fast you are travelling when you make
    it -- the cost scales directly with orbital speed -- so on an
    eccentric orbit it matters enormously *where* it happens. Both the
    ascending and descending node are valid points to rotate the plane
    about, and they sit half an orbit apart, so one of them is always the
    higher (slower, cheaper) of the two. This picks that one.

    Concretely, for a 90 degree change around Mun: on the elliptical
    capture orbit, at apoapsis, roughly 166 m/s. On a circular 50km orbit,
    722 m/s. Same maneuver, four times the fuel, purely from when it is
    done. On a circular orbit both nodes are equivalent and this reduces to
    the old behaviour."""
    sc = client.space_center
    orbit = vessel.orbit
    delta_inclination = math.radians(target_inclination_deg) - orbit.inclination

    ta_ascending = (-orbit.argument_of_periapsis) % (2 * math.pi)
    ta_descending = (ta_ascending + math.pi) % (2 * math.pi)

    # Whichever node is further out is the slower one, and therefore the
    # cheaper place to rotate the plane.
    candidates = []
    for true_anomaly in (ta_ascending, ta_descending):
        radius = orbit.radius_at_true_anomaly(true_anomaly)
        candidates.append((radius, true_anomaly))
    radius, true_anomaly = max(candidates)

    ut = orbit.ut_at_true_anomaly(true_anomaly)
    if ut < sc.ut:
        ut += orbit.period

    v = vis_viva_speed(orbit.body.gravitational_parameter, radius, orbit.semi_major_axis)
    prograde_dv = v * (math.cos(delta_inclination) - 1)
    normal_dv = v * math.sin(delta_inclination)

    return vessel.control.add_node(ut, prograde=prograde_dv, normal=normal_dv)


def phasing_node(client, vessel, angle_to_close_deg, num_orbits=1, burn_at="apoapsis"):
    """Adds a node that puts the vessel on a temporary phasing orbit: its
    period is adjusted so that after `num_orbits` loops back to the burn
    point, the vessel has drifted `angle_to_close_deg` relative to where
    it would be on the current (target) orbit. Positive angle means "get
    there sooner" (a lower, faster phasing orbit); negative means "arrive
    later" (higher, slower). Caller is responsible for checking the
    resulting orbit stays above a safe altitude (a large angle over few
    orbits can demand a very low or very high phasing orbit) and for
    circularizing back once the phasing orbit completes.

    Returns (node, phase_period_s, phase_periapsis_altitude_m) so the
    caller can sanity-check safety before committing to the burn.
    """
    sc = client.space_center
    orbit = vessel.orbit
    body = orbit.body
    mu = body.gravitational_parameter
    target_period = orbit.period
    angle_frac = angle_to_close_deg / 360.0
    phase_period = target_period * (1 - angle_frac / num_orbits)
    phase_period = max(phase_period, target_period * 0.05)

    a_phase = (mu * phase_period ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)

    if burn_at == "apoapsis":
        ut = sc.ut + orbit.time_to_apoapsis
        r = body.equatorial_radius + orbit.apoapsis_altitude
    else:
        ut = sc.ut + orbit.time_to_periapsis
        r = body.equatorial_radius + orbit.periapsis_altitude

    a1 = orbit.semi_major_axis
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a_phase)
    node = vessel.control.add_node(ut, prograde=v2 - v1)

    phase_periapsis_altitude = 2 * a_phase - r - body.equatorial_radius
    return node, phase_period, phase_periapsis_altitude


def closest_other_vessel_distance(client, vessel):
    """Distance (m) to the nearest other vessel kRPC currently knows about,
    or None if there isn't one."""
    body_frame = vessel.orbit.body.reference_frame
    my_pos = vessel.position(body_frame)
    closest = None
    for other in client.space_center.vessels:
        if other == vessel:
            continue
        other_pos = other.position(body_frame)
        dist = sum((a - b) ** 2 for a, b in zip(my_pos, other_pos)) ** 0.5
        if closest is None or dist < closest:
            closest = dist
    return closest


def burn_away_from_debris(client, vessel, job, min_distance_m=50, max_burn_s=6, throttle=0.6):
    """Briefly burns roughly prograde (holding current attitude) right
    after a stage separates, so the continuing vessel actively opens
    distance from the piece it just dropped instead of coasting right next
    to it. Real rockets do this too -- burning straight back toward the
    launch site immediately after staging (as the dropped booster's return
    autopilot does) is otherwise liable to fly right back into whatever it
    just separated from, since both pieces start out at nearly the same
    position and velocity."""
    control = vessel.control
    ap = vessel.auto_pilot
    was_engaged = ap.engaged
    ap.engaged = True

    job.message = "burning clear of separated stage"
    # Hold whatever attitude the vessel was AT THE MOMENT this burn started,
    # not whatever it happens to be each tick -- a separation event can
    # leave the vessel briefly wobbling, and continuously re-targeting the
    # live (still-settling) pitch/heading was chasing that wobble instead
    # of damping it, visibly worsening it right during this burn. Confirmed
    # live: a real tumble (AoA -21 deg, 14.8 deg autopilot error) that
    # coincided exactly with this phase, which then recovered as soon as
    # the main ascent's fixed steering target took back over afterward.
    flight = vessel.flight(vessel.orbit.body.reference_frame)
    target_pitch, target_heading = flight.pitch, flight.heading
    ap.target_pitch_and_heading(target_pitch, target_heading)
    control.throttle = throttle
    elapsed = 0.0
    try:
        while elapsed < max_burn_s:
            job.check_abort()
            dist = closest_other_vessel_distance(client, vessel)
            if dist is not None and dist >= min_distance_m:
                break
            job.sleep(0.1)
            elapsed += 0.1
    finally:
        control.throttle = 0.0
        ap.engaged = was_engaged


def execute_node(client, vessel, job, node, lead_time=15):
    """Orients the vessel to a node, warps to it, and burns it out."""
    sc = client.space_center
    ap = vessel.auto_pilot
    control = vessel.control

    burn_time = estimate_burn_time(vessel, node.delta_v)

    # RCS on for the whole burn: reaction wheels alone can lose attitude
    # lock while a powerful engine is firing (any thrust/COM offset
    # produces a disturbance torque with nothing to counter it), which
    # silently wastes the burn -- full throttle, but pointed increasingly
    # off the node's actual direction, so remaining_delta_v barely drops.
    # Observed for real on a satellite's phasing burn: throttle stuck at
    # 1.0 for 150+ seconds with almost no progress on a burn that should
    # have taken ~15s at that thrust-to-mass ratio.
    was_rcs = control.rcs
    control.rcs = True

    ap.reference_frame = node.reference_frame
    ap.target_direction = (0, 1, 0)
    ap.engaged = True
    job.message = "orienting for burn"
    orient_elapsed = 0.0
    while ap.error > 2 and orient_elapsed < 60:
        job.check_abort()
        job.sleep(0.2)
        orient_elapsed += 0.2

    burn_ut = sc.ut + node.time_to - (burn_time / 2.0)
    if burn_ut - sc.ut > lead_time:
        job.message = "warping to burn"
        sc.warp_to(burn_ut - lead_time)

    while node.time_to - (burn_time / 2.0) > 0:
        job.check_abort()
        job.sleep(0.1)

    job.message = "executing burn"
    # Proportional throttle, not a crude full-throttle-then-fixed-trickle
    # step: taper continuously as remaining delta-v approaches zero so the
    # final burn stops as close to exactly on target as the tick rate
    # allows, instead of overshooting past a coarse threshold.
    taper_window = 5.0  # start easing off within this many m/s of done
    min_throttle = 0.02
    last_dv = node.remaining_delta_v
    stager = staging.Stager(vessel)
    try:
        while node.remaining_delta_v > 0.02 and node.remaining_delta_v <= last_dv + 0.1:
            job.check_abort()
            # Confirmed live: a burn needing more delta-v than the current
            # stage can supply just runs it dry -- available_thrust hits 0
            # and remaining_delta_v gets stuck at a fixed value forever
            # (which the while-condition above can't tell apart from a
            # completed burn). Stage and keep burning instead of stalling.
            # verify_empty=False: mid-burn, the engine was already firing,
            # so zero thrust can only mean genuinely dry.
            if stager.stage_if_dry(
                job, verify_empty=False,
                settle=staging.settle_briefly(),  # let the new engine spool up
                label="staged mid-burn",
            ):
                continue
            last_dv = node.remaining_delta_v
            control.throttle = min(1.0, max(min_throttle, last_dv / taper_window))
            job.sleep(0.02)
    finally:
        # Guaranteed even on abort/error -- a burn loop that only cuts
        # throttle after the while-loop exits normally leaves the engine
        # running at full throttle if check_abort() raises mid-burn (this
        # happened for real: aborting a job mid-burn left the vessel
        # accelerating uncontrolled since the cleanup line was never
        # reached).
        control.throttle = 0.0
        ap.engaged = False
        control.rcs = was_rcs
        try:
            node.remove()
        except Exception:
            pass
