"""Standalone circularization: bring the current orbit to a circular shape
at a target altitude, regardless of whether the current orbit is above,
below, or straddling that altitude.

Nothing else in this project exposes "just fix this orbit" as its own
action. `ascent.py`'s final circularization only ever adjusts periapisis to
match an apoapsis it just climbed to -- target_apoapsis_m there is used
solely to decide when to stop climbing, and is never passed into the actual
circularization burn at all. `arrival.py`'s circularization only ever runs
immediately after a fresh capture, where the orbit is already roughly the
right size. Neither handles being asked to reshape an orbit that's already
established and possibly on the wrong side of the target altitude.

Confirmed needed live: reusing `ascent.py`'s endpoint to fix a Mun orbit
that had ended up too large (a bug in change_inclination_node, since fixed,
had corrupted the orbit shape) only ever lowered periapsis -- it could not
touch the already-too-high apoapsis, leaving a 108km x 334km ellipse
instead of the requested circular 100km orbit.
"""

from backend.autopilots import maneuver


def run_circularize(client, vessel, job, target_altitude_m):
    sc = client.space_center
    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    body = vessel.orbit.body
    atmosphere_depth = body.atmosphere_depth

    # If the periapsis is already inside the atmosphere, the orbit is
    # actively decaying right now -- every pass through periapsis bleeds
    # more energy. The normal strategy below schedules a burn at the next
    # apoapsis and waits for it, but that wait can be minutes long, and
    # kRPC's warp_to() silently does nothing while moving fast through
    # dense atmosphere (KSP blocks warp there) -- confirmed live: a node
    # scheduled 800+ seconds out sat unexecuted while periapsis and
    # apoapsis both kept draining from drag, nearly losing the vessel.
    # An immediate (if imprecise -- see adjust_other_apsis_now) burn right
    # now to get periapsis clear of the atmosphere is what actually matters
    # here; the precise reshape below can run once that's no longer racing
    # against decay.
    if atmosphere_depth and vessel.orbit.periapsis_altitude < atmosphere_depth:
        safe_periapsis_m = atmosphere_depth * 1.05
        job.message = f"periapsis is inside the atmosphere -- emergency burn to raise it above {safe_periapsis_m / 1000:.0f} km"
        node = maneuver.adjust_other_apsis_now(client, vessel, safe_periapsis_m)
        maneuver.execute_node(client, vessel, job, node)

    current_apoapsis = vessel.orbit.apoapsis_altitude

    # Two symmetric cases, not one -- which apsis needs adjusting first
    # depends on which side of the target the current orbit is on. Burning
    # to fix the FAR apsis first, then circularizing at the apsis that
    # results, is cheaper and simpler than trying to hit both altitudes in
    # a single burn.
    if current_apoapsis > target_altitude_m:
        job.message = f"adjusting periapsis to {target_altitude_m / 1000:.0f} km"
        node = maneuver.change_periapsis_node(client, vessel, target_altitude_m, burn_at="apoapsis")
        maneuver.execute_node(client, vessel, job, node)

        job.message = f"circularizing at {target_altitude_m / 1000:.0f} km"
        node = maneuver.circularize_node(client, vessel, at="periapsis")
        maneuver.execute_node(client, vessel, job, node)
    else:
        job.message = f"raising apoapsis to {target_altitude_m / 1000:.0f} km"
        node = maneuver.change_apoapsis_node(client, vessel, target_altitude_m, burn_at="periapsis")
        maneuver.execute_node(client, vessel, job, node)

        job.message = f"circularizing at {target_altitude_m / 1000:.0f} km"
        node = maneuver.circularize_node(client, vessel, at="apoapsis")
        maneuver.execute_node(client, vessel, job, node)

    # As in ascent.py: these burns are timed as instantaneous impulses, which
    # a large burn on a low-TWR craft can violate badly enough to miss the
    # target outright (see verify_and_trim_apsides). Confirm the real result
    # before declaring success.
    ok = maneuver.verify_and_trim_apsides(
        client, vessel, job,
        target_periapsis_m=target_altitude_m, target_apoapsis_m=target_altitude_m,
    )
    if not ok:
        raise RuntimeError(
            f"circularization did not converge: periapsis="
            f"{vessel.orbit.periapsis_altitude / 1000:.1f} km, apoapsis="
            f"{vessel.orbit.apoapsis_altitude / 1000:.1f} km (target {target_altitude_m / 1000:.0f} km)"
        )

    job.message = f"circularized at {target_altitude_m / 1000:.0f} km"
