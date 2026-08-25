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

    current_apoapsis = vessel.orbit.apoapsis_altitude

    # Two symmetric cases, not one -- which apsis needs adjusting first
    # depends on which side of the target the current orbit is on. Burning
    # to fix the FAR apsis first, then circularizing at the apsis that
    # results, is cheaper and simpler than trying to hit both altitudes in
    # a single burn.
    if current_apoapsis > target_altitude_m:
        job.message = f"lowering periapsis to {target_altitude_m / 1000:.0f} km"
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

    job.message = f"circularized at {target_altitude_m / 1000:.0f} km"
