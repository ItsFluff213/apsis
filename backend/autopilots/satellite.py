"""Satellite constellation deployment: transfer to the constellation's
target shell (altitude + inclination), then phase into an even slot
relative to whatever other satellites are already in that constellation --
regardless of which launch they came from.

This is the most involved autopilot in the project so far (a real transfer
+ plane change + phasing sequence, not just "burn until orbit"). Built in
full per the plan, expecting to need bugfixes against a live test the same
way ascent/landing/booster-return did.
"""

import math

from backend import constellations, db, parts
from backend.autopilots import maneuver

MIN_SAFE_PERIAPSIS_FRACTION = 0.05  # of body radius, as a crude floor when no atmosphere data fits better


def _min_safe_altitude(body):
    if body.atmosphere_depth:
        return body.atmosphere_depth + 10000
    return body.equatorial_radius * MIN_SAFE_PERIAPSIS_FRACTION


def run_deploy_satellite(client, registry, vessel, job, constellation_id):
    sc = client.space_center
    control = vessel.control
    ap = vessel.auto_pilot
    constellation = db.get_constellation(constellation_id)
    if constellation is None:
        raise ValueError(f"unknown constellation id {constellation_id}")

    body = vessel.orbit.body
    if body.name != constellation["body"]:
        raise ValueError(
            f"vessel is at {body.name}, but constellation {constellation['name']!r} targets {constellation['body']}"
        )

    target_altitude, target_inclination = constellations.target_altitude_and_inclination(constellation, body)

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    # --- 1. Transfer to the target altitude (raise/lower apoapsis, then
    # circularize once there) ---
    job.message = f"transferring to {target_altitude / 1000:.0f} km shell"
    node = maneuver.change_apoapsis_node(client, vessel, target_altitude, burn_at="periapsis")
    maneuver.execute_node(client, vessel, job, node)

    job.message = "coasting to circularization point"
    if vessel.orbit.time_to_apoapsis > 30:
        sc.warp_to(sc.ut + vessel.orbit.time_to_apoapsis - 20)
    while vessel.orbit.time_to_apoapsis > 5:
        job.check_abort()
        job.sleep(1)

    job.message = "circularizing at target shell"
    node = maneuver.change_periapsis_node(client, vessel, target_altitude, burn_at="apoapsis")
    maneuver.execute_node(client, vessel, job, node)

    # --- 2. Plane change, if the inclination is meaningfully off ---
    current_inclination_deg = math.degrees(vessel.orbit.inclination)
    if abs(current_inclination_deg - target_inclination) > 0.5:
        job.message = f"adjusting inclination to {target_inclination:.1f} deg"
        node = maneuver.change_inclination_node(client, vessel, target_inclination)
        maneuver.execute_node(client, vessel, job, node)

    # --- 3. Phase into an even slot relative to other constellation members ---
    job.message = "computing constellation slot"
    target_slot_deg = constellations.compute_target_slot_deg(client, registry, constellation, vessel.name)

    if constellation["kind"] == "communications":
        current_pos_deg = vessel.flight(body.reference_frame).longitude % 360
    else:
        current_pos_deg = math.degrees(vessel.orbit.true_anomaly) % 360

    angle_to_close = (target_slot_deg - current_pos_deg + 180) % 360 - 180  # signed, in (-180, 180]

    if abs(angle_to_close) > 2:
        job.message = f"phasing {angle_to_close:.1f} deg to constellation slot"
        min_safe_alt = _min_safe_altitude(body)
        num_orbits = 1
        phase_periapsis = -1
        node = None
        # Each call creates a real maneuver node via kRPC -- remove the
        # previous trial before creating the next, or they pile up
        # unremoved (a confirmed real bug: this used to leave multiple
        # stale nodes on the vessel, on top of a redundant extra node
        # created after the loop, and the resulting burn sent a real test
        # satellite onto an escape trajectory).
        while True:
            if node is not None:
                node.remove()
            node, phase_period_s, phase_periapsis = maneuver.phasing_node(
                client, vessel, angle_to_close, num_orbits=num_orbits, burn_at="apoapsis",
            )
            if phase_periapsis >= min_safe_alt or num_orbits >= 20:
                break
            num_orbits += 1
        maneuver.execute_node(client, vessel, job, node)

        # The burn just happened at apoapsis, so waiting num_orbits full
        # phasing-orbit periods from right now brings the vessel back to
        # that same apoapsis point, having drifted the intended angle.
        job.message = f"coasting through {num_orbits} phasing orbit(s)"
        target_end_ut = sc.ut + phase_period_s * num_orbits
        if target_end_ut - sc.ut > 30:
            sc.warp_to(target_end_ut - 30)
        while sc.ut < target_end_ut:
            job.check_abort()
            job.sleep(1)

        job.message = "recircularizing at target shell"
        node = maneuver.change_periapsis_node(client, vessel, target_altitude, burn_at="apoapsis")
        maneuver.execute_node(client, vessel, job, node)

    # --- 4. Release the satellite from its transfer stage now that it's on
    # station. The transfer stage rode along for the whole trip (unlike the
    # launch booster, which separates during ascent) -- it only comes off
    # here, once the payload is actually in its slot. Once dropped, it's
    # just another spent stage with its own engine, so it gets handed off
    # to booster_return the same way the launch booster does.
    #
    # Not every satellite has a separate transfer stage at all -- a design
    # where the satellite's own final stage carries its own engine (no
    # decoupler between "transfer" and "payload" because they're the same
    # piece) has nothing to separate here. available_thrust > 0.1 alone
    # can't tell those two cases apart (the satellite's own engine also
    # reads as available thrust), so this also requires an actual
    # decoupler to exist on the vessel before attempting anything.
    decouplers_by_stage = parts.get_decouplers_by_stage(vessel)
    if vessel.available_thrust > 0.1 and decouplers_by_stage:
        job.message = "separating transfer stage"
        stage_num = control.current_stage
        tagged = decouplers_by_stage.get(stage_num)
        names_before = {v.name for v in sc.vessels}
        if tagged:
            for d in tagged:
                try:
                    if d.decoupler and not d.decoupler.decoupled:
                        d.decoupler.decouple()
                except Exception:
                    pass
        control.activate_next_stage()
        job.sleep(0.5)
        transfer_stage = next((v for v in sc.vessels if v.name not in names_before and v != vessel), None)

        # Unlike the ascent booster case, the satellite itself must not
        # burn here -- an extra clearance burn risks disturbing the
        # carefully-phased, precise constellation orbit it just reached.
        # Separation velocity alone is enough distance for safety, and the
        # transfer stage gets deorbited (moving further away) right below.

        # Leave the satellite oriented prograde rather than however it
        # happened to end up pointing after the clearance burn -- a
        # settled, predictable attitude for whatever comes next (station
        # keeping, comms alignment, etc.), not a matter of drifting.
        job.message = "orienting prograde"
        ap.reference_frame = vessel.orbital_reference_frame
        ap.target_direction = (0, 1, 0)
        ap.engaged = True
        orient_elapsed = 0.0
        while ap.error > 2 and orient_elapsed < 30:
            job.check_abort()
            job.sleep(0.2)
            orient_elapsed += 0.2
        ap.engaged = False

        # The transfer stage is discarded debris at this point -- unlike
        # the launch booster (which returns to land back at KSC), it's
        # stranded wherever the constellation slot happens to be, so flying
        # it all the way back isn't worth the delta-v even if there were
        # any left. Just deorbit it (drop periapsis into the atmosphere, or
        # well under the surface on airless bodies) so it decays/impacts on
        # its own instead of lingering as junk in the constellation's shell.
        if transfer_stage is not None:
            job.message = "deorbiting transfer stage"
            sc.active_vessel = transfer_stage
            job.sleep(0.5)
            ts_body = transfer_stage.orbit.body
            if transfer_stage.orbit.periapsis_altitude > 0 and transfer_stage.available_thrust > 0.1:
                node = maneuver.change_periapsis_node(
                    client, transfer_stage, target_periapsis_m=-ts_body.equatorial_radius * 0.05,
                    burn_at="apoapsis",
                )
                maneuver.execute_node(client, transfer_stage, job, node)
            job.message = "transfer stage deorbited"
            sc.active_vessel = vessel

    db.add_constellation_member(constellation_id, vessel.name)
    job.message = "deployed to constellation"
