"""Shared arrival sequence: turn "we're inside the target body's SOI" into
a clean circular orbit around it.

Identical whether the vessel just crossed into Mun's SOI from Kerbin or
into Duna's from solar orbit, so both the moon and planet transfer
autopilots call this rather than each carrying their own copy. Every step
below exists because a real test flight failed without it -- the comments
say which.
"""

from backend.autopilots import maneuver


def capture_and_circularize(client, vessel, job, body, target_periapsis_m, target_inclination_deg=None):
    """Run the full arrival sequence at `body`, which the vessel must
    already be orbiting (i.e. vessel.orbit.body == body).

    target_inclination_deg is relative to `body`, applied as a separate
    plane-change burn once in a stable circular orbit -- a transfer only
    ever burns prograde and cannot change planes on the way in.
    """
    # --- Safety first: is the raw arrival trajectory going to hit? ---
    # A transfer controls WHEN the vessel arrives relative to the body, not
    # the exact closest-approach distance (that needs real 3D targeting
    # this patched-conic model doesn't do). A clean arrival can still line
    # up a near-direct hit on the surface -- we want a capture around it,
    # never straight into it. periapsis_altitude is well-defined even for a
    # hyperbolic flyby (unlike apoapsis), so it can be checked the moment
    # SOI entry is detected, well before actually reaching that periapsis.
    min_safe_periapsis_m = max(target_periapsis_m * 0.5, body.equatorial_radius * 0.05)
    if vessel.orbit.periapsis_altitude < min_safe_periapsis_m:
        job.message = f"correcting course -- raw arrival would pass too close to {body.name}"
        node = maneuver.adjust_other_apsis_now(client, vessel, min_safe_periapsis_m * 1.5)
        maneuver.execute_node(client, vessel, job, node)

    # --- Capture, if the arrival is still an escape trajectory ---
    # A Hohmann-style transfer aims for a capture but doesn't precisely
    # target a bound arrival -- entering the SOI is often still a HYPERBOLIC
    # flyby (eccentricity >= 1). A hyperbolic orbit has no real apoapsis
    # (kRPC reports a negative apoapsis_altitude and a meaningless
    # time_to_apoapsis), so the shaping burn below would be undefined.
    # Confirmed live: skipping this silently produced a huge, wrong burn
    # that flung a real vessel out of Kerbin's SOI entirely into solar
    # orbit. Burn at the flyby's periapsis (always well-defined, hyperbolic
    # or not) to pull apoapsis down inside the SOI first.
    if vessel.orbit.eccentricity >= 1:
        job.message = f"capturing at {body.name} (arrival was a flyby, not a capture)"
        capture_apoapsis_m = max(target_periapsis_m * 4, body.sphere_of_influence * 0.5)
        node = maneuver.change_apoapsis_node(client, vessel, capture_apoapsis_m, burn_at="periapsis")
        maneuver.execute_node(client, vessel, job, node)

    # --- Shape, then circularize: two burns, not one ---
    # The capture orbit's own periapsis (wherever the arrival trajectory
    # happened to put it) is not target_periapsis_m. Calling
    # change_apoapsis_node(target_periapsis_m, burn_at="periapsis") here
    # would treat target_periapsis_m as a new APOAPSIS while leaving the
    # arrival periapsis untouched -- nonsense whenever the arrival
    # periapsis isn't already near the target, and confirmed live as a
    # huge wrong burn instead of a clean capture orbit. Correct sequence:
    # pin periapsis to the target by burning at the far, slow, cheap
    # capture apoapsis, then circularize at that new periapsis.
    job.message = f"shaping periapsis at {body.name}"
    node = maneuver.change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis")
    maneuver.execute_node(client, vessel, job, node)

    # --- Plane change BEFORE circularizing, not after ---
    # A plane change costs in proportion to how fast you are going, and the
    # craft is never slower than it is now, way out at the capture orbit's
    # apoapsis. Doing it here instead of on the final circular orbit is the
    # difference between roughly 166 m/s and 722 m/s for a polar orbit
    # around Mun -- 556 m/s of pure waste, which is more spare fuel than a
    # typical Mun craft has after capture, so the expensive ordering would
    # simply run the tank dry partway through the burn.
    #
    # Circularizing afterwards is safe: the plane change rotates the
    # velocity vector without changing its magnitude, so the orbit's shape
    # is untouched and its periapsis is still where the next burn expects.
    if target_inclination_deg is not None:
        job.message = f"adjusting inclination around {body.name} (cheap out here, before circularizing)"
        node = maneuver.change_inclination_node(client, vessel, target_inclination_deg)
        maneuver.execute_node(client, vessel, job, node)

    job.message = f"circularizing at {body.name}"
    node = maneuver.circularize_node(client, vessel, at="periapsis")
    maneuver.execute_node(client, vessel, job, node)
