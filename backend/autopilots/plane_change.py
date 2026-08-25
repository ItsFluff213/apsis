"""Standalone plane-change: rotate an existing orbit to a target inclination
without touching its shape.

Nothing else in this project exposes "just fix the plane" as its own action
-- change_inclination_node is otherwise only ever called internally, mid
another sequence (docking's rendezvous, moon/planet arrival). This is the
same operation, offered directly, for correcting an orbit that ended up on
the wrong plane after the fact (e.g. a manual SAS toggle mid-ascent
corrupting the heading -- confirmed live: an ascent targeting a 90 degree
polar orbit landed on 57 degrees after exactly that).
"""

import math

from backend.autopilots import maneuver


def run_plane_change(client, vessel, job, target_inclination_deg):
    sc = client.space_center
    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    job.message = f"changing inclination to {target_inclination_deg:.1f} deg"
    maneuver.execute_node_retrying(
        client, vessel, job,
        lambda: maneuver.change_inclination_node(client, vessel, target_inclination_deg),
    )

    job.message = f"inclination now {math.degrees(vessel.orbit.inclination):.1f} deg (target was {target_inclination_deg:.1f})"
