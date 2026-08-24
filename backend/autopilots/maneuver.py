"""Maneuver node helpers: create nodes for common orbital changes and
execute any node (orient, warp, burn). Shared by the ascent, landing, and
booster-return autopilots so the node-execution logic lives in one place.
"""

import math


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
    shape otherwise unchanged. Sign convention assumes kRPC's node-normal
    axis matches standard orbital-angular-momentum direction (positive
    normal at the ascending node increases inclination) -- this hasn't been
    independently verified against a live burn yet; check the resulting
    inclination after the first real use."""
    sc = client.space_center
    orbit = vessel.orbit
    delta_inclination = math.radians(target_inclination_deg) - orbit.inclination

    ta_an = (-orbit.argument_of_periapsis) % (2 * math.pi)
    ut = orbit.ut_at_true_anomaly(ta_an)
    if ut < sc.ut:
        ut += orbit.period

    r = orbit.radius_at_true_anomaly(ta_an)
    v = vis_viva_speed(orbit.body.gravitational_parameter, r, orbit.semi_major_axis)
    normal_dv = 2 * v * math.sin(delta_inclination / 2.0)

    return vessel.control.add_node(ut, normal=normal_dv)


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
    control.throttle = throttle
    elapsed = 0.0
    try:
        while elapsed < max_burn_s:
            job.check_abort()
            dist = closest_other_vessel_distance(client, vessel)
            if dist is not None and dist >= min_distance_m:
                break
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            ap.target_pitch_and_heading(flight.pitch, flight.heading)
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
    try:
        while node.remaining_delta_v > 0.02 and node.remaining_delta_v <= last_dv + 0.1:
            job.check_abort()
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
