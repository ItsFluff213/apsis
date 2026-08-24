"""Ascent autopilot: launch -> gravity turn -> automatic staging -> circularize.

Based on the standard kRPC launch-into-orbit pattern, extended to use tagged
decouplers (see backend/parts.py) for staging when available, with a
fallback of "activate next stage when current stage's engines flame out".
"""

from backend import parts
from backend.autopilots import maneuver


def run_ascent(client, vessel, job, target_apoapsis_m, target_periapsis_m, target_inclination_deg=0.0,
               turn_start_altitude_m=250, turn_end_altitude_m=45000):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    ap = vessel.auto_pilot
    control = vessel.control
    control.sas = False
    control.rcs = False
    control.throttle = 1.0

    ap.engaged = True
    ap.target_pitch_and_heading(90, 90)
    job.message = "launching"

    body = vessel.orbit.body
    flight = vessel.flight(body.reference_frame)
    turn_angle = 0

    decouplers_by_stage = parts.get_decouplers_by_stage(vessel)
    fired_stages = set()

    try:
        # --- Ascent + gravity turn + auto-staging ---
        while True:
            job.check_abort()
            altitude = flight.mean_altitude
            apoapsis = vessel.orbit.apoapsis_altitude

            if turn_start_altitude_m < altitude < turn_end_altitude_m:
                frac = (altitude - turn_start_altitude_m) / (turn_end_altitude_m - turn_start_altitude_m)
                new_turn_angle = frac * 90
                if abs(new_turn_angle - turn_angle) > 0.5:
                    turn_angle = new_turn_angle
                    ap.target_pitch_and_heading(90 - turn_angle, 90 - target_inclination_deg)
            elif altitude >= turn_end_altitude_m:
                ap.target_pitch_and_heading(0, 90 - target_inclination_deg)

            # Auto-staging: if current stage has no engines with remaining
            # thrust/fuel, decouple/activate the next stage. Prefer a
            # tagged decoupler for the current stage number if one exists.
            if vessel.available_thrust < 0.1 and control.current_stage not in fired_stages:
                stage_num = control.current_stage
                tagged = decouplers_by_stage.get(stage_num)
                if tagged:
                    for d in tagged:
                        try:
                            if d.decoupler and not d.decoupler.decoupled:
                                d.decoupler.decouple()
                        except Exception:
                            # kRPC can throw a null-reference error reading
                            # .decoupler on some parts (observed on a real
                            # craft). activate_next_stage() below still
                            # fires the stage's actual staging action
                            # regardless.
                            pass
                names_before = {v.name for v in sc.vessels}
                control.activate_next_stage()
                fired_stages.add(stage_num)
                job.message = f"staged (stage {stage_num})"

                # If a piece actually separated (a new vessel genuinely
                # appeared) and it still has engines/fuel of its own (e.g.
                # a spent booster that's going to fly a return-to-KSC
                # autopilot job), actively burn clear of it now rather than
                # coasting right next to it. available_thrust alone can't
                # tell "something separated with thrust" apart from "this
                # activate_next_stage() call was just the first ignition of
                # our own engine" -- confirmed live: an empty stage ahead of
                # the real engine had the debris-clear burn (throttle 0.6)
                # misfire right at its ignition, since the engine lighting
                # up also makes available_thrust go from 0 to nonzero.
                job.sleep(0.3)
                separated = bool({v.name for v in sc.vessels} - names_before)
                if separated and vessel.available_thrust > 0.1:
                    maneuver.burn_away_from_debris(client, vessel, job)
                    # burn_away_from_debris always zeroes throttle in its
                    # own cleanup (correct for its other callers, which
                    # either land or move on to a separate burn right
                    # after) -- but here the ascent itself is still
                    # supposed to be under full thrust, so it must be
                    # explicitly restored. Confirmed live: without this,
                    # the engine silently stayed at 0% for the rest of the
                    # "ascent" and the rocket fell back to the pad.
                    control.throttle = 1.0
                    ap.target_pitch_and_heading(90 - turn_angle, 90 - target_inclination_deg)

            if apoapsis >= target_apoapsis_m:
                job.message = "target apoapsis reached, coasting to space"
                break

            job.sleep(0.25)

        control.throttle = 0.25
        while vessel.orbit.apoapsis_altitude < target_apoapsis_m:
            job.check_abort()
            job.sleep(0.1)
    finally:
        control.throttle = 0.0

    # Coast out of the atmosphere if still inside it.
    atmosphere_depth = body.atmosphere_depth
    if atmosphere_depth and flight.mean_altitude < atmosphere_depth:
        job.message = "coasting through atmosphere"
        while flight.mean_altitude < atmosphere_depth:
            job.check_abort()
            job.sleep(1)

    job.message = "planning circularization burn"
    node = maneuver.change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis")
    maneuver.execute_node(client, vessel, job, node)

    control.sas = True
    job.message = "orbit achieved"
