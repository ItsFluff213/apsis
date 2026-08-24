"""Executes a trajectory plan already computed by the external
KSP-MGA-Planner tool (see backend/interplanetary.py for the parser) --
no transfer-window search or Lambert solving happens here, only burning
the plan's own precomputed prograde/normal/radial vectors at the right
times, using the same execute_node already trusted for every other burn
in this project.

Flyby steps carry no burn -- the preceding DSM already aimed the
encounter, so a flyby step here just means "coast/warp through it,"
logging progress so the user can see which leg of a years-long transfer
is in progress.
"""

from backend.autopilots import maneuver


def run_interplanetary_transfer(client, vessel, job, steps):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    # T+0 in the plan is "now", the moment this job actually starts
    # executing -- the plan should be imported and started right away
    # (a plan computed against a much earlier UT than the actual departure
    # burn will have the wrong ejection geometry, same as launching late
    # against any transfer-window plan).
    reference_ut = sc.ut

    for step in steps:
        job.check_abort()
        if step["type"] == "flyby":
            job.message = f"coasting through flyby around {step['name'].replace('Flyby around ', '')}"
            target_ut = reference_ut + step["soi_exit_offset_s"]
            if target_ut - sc.ut > 30:
                sc.warp_to(target_ut - 30)
            while sc.ut < target_ut:
                job.check_abort()
                job.sleep(1)
            continue

        job.message = f"executing {step['name']}"
        ut = reference_ut + step["ut_offset_s"]
        node = vessel.control.add_node(
            ut, prograde=step["prograde"], normal=step["normal"], radial=step["radial"],
        )
        maneuver.execute_node(client, vessel, job, node)

    job.message = "interplanetary transfer complete"
