"""General landing autopilot: deorbit (if needed) and land near a target
in-game waypoint, using the shared suicide-burn descent guidance.
"""

from backend.autopilots import descent, maneuver


def run_landing(client, vessel, job, target_lat, target_lon):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    body = vessel.orbit.body

    if vessel.orbit.periapsis_altitude > 0:
        job.message = "deorbiting"
        node = maneuver.change_periapsis_node(client, vessel, target_periapsis_m=-body.equatorial_radius * 0.05,
                                               burn_at="apoapsis")
        maneuver.execute_node(client, vessel, job, node)

    descent.suicide_burn_landing(client, vessel, job, target_lat=target_lat, target_lon=target_lon)
