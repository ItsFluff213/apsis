"""General landing autopilot: deorbit (if needed) and land near a target
in-game waypoint, using the shared suicide-burn descent guidance.

The deorbit burn is aimed rather than fired wherever the craft happens to
be -- see backend/autopilots/deorbit.py for why that is where nearly all
the landing accuracy comes from, and what the prediction does and does not
model.
"""

from backend.autopilots import deorbit, descent, maneuver

# How deep below the surface to aim periapsis. Deep enough that the craft
# is committed to coming down rather than skipping back out, shallow enough
# that the entry stays reasonably shallow and the descent burn manageable.
DEORBIT_PERIAPSIS_FRACTION = 0.05


def run_landing(client, vessel, job, target_lat, target_lon):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    body = vessel.orbit.body
    target_periapsis_m = -body.equatorial_radius * DEORBIT_PERIAPSIS_FRACTION

    if vessel.orbit.periapsis_altitude > 0:
        r_peri_target = body.equatorial_radius + target_periapsis_m

        job.message = "working out where to deorbit from"
        burn_ut, predicted_miss = deorbit.find_deorbit_burn_ut(
            client, vessel, job, target_lat, target_lon, r_peri_target,
        )

        if burn_ut is None:
            # Prediction failed (odd geometry, or an orbit that never
            # crosses the surface on the modelled ellipse). Fall back to
            # the old unaimed behaviour rather than refusing to land --
            # descent guidance still gets the craft down, just not
            # particularly near the waypoint.
            job.message = "couldn't aim the deorbit -- burning at apoapsis instead"
            node = maneuver.change_periapsis_node(
                client, vessel, target_periapsis_m=target_periapsis_m, burn_at="apoapsis",
            )
            maneuver.execute_node(client, vessel, job, node)
        else:
            wait_s = burn_ut - sc.ut
            job.message = (
                f"deorbit burn in {wait_s / 60:.1f} min "
                f"(predicted landing ~{predicted_miss / 1000:.0f} km from target)"
            )
            # Warp most of the way there, leaving room for execute_node to
            # orient and run its own lead-in.
            if wait_s > 180:
                sc.warp_to(burn_ut - 120)
            while sc.ut < burn_ut - 60:
                job.check_abort()
                job.sleep(0.5)

            node = maneuver.adjust_other_apsis_now(
                client, vessel, target_periapsis_m, lead_time=max(burn_ut - sc.ut, 1.0),
            )
            maneuver.execute_node(client, vessel, job, node)

    descent.suicide_burn_landing(client, vessel, job, target_lat=target_lat, target_lon=target_lon)
