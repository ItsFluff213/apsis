"""Ascent autopilot: launch -> gravity turn -> automatic staging -> circularize.

Based on the standard kRPC launch-into-orbit pattern, extended to use tagged
decouplers (see backend/parts.py) for staging when available, with a
fallback of "activate next stage when current stage's engines flame out".
"""

from backend import parts
from backend.autopilots import maneuver


def _stage_has_fuel(vessel, stage_num):
    """True if the given stage's parts still hold meaningful propellant.
    available_thrust hitting 0 already implies the *active* engine is dry,
    but this double-checks against the actual resources in that stage
    (not just the engine's current draw) before we let go of it -- staging
    should only ever drop a stage once it's genuinely empty, not just
    once the currently-firing engine happens to read no thrust."""
    try:
        res = vessel.resources_in_decouple_stage(stage_num, cumulative=False)
        for name in ("LiquidFuel", "Oxidizer", "SolidFuel"):
            if name in res.names and res.amount(name) > 0.1:
                return False
        return True
    except Exception:
        return True  # can't tell -- don't block staging on an unknown


def _efficient_throttle(flight, atmosphere_depth):
    """Caps throttle (never raises it) to avoid needlessly overspeeding in
    the thick lower atmosphere, where excess speed just burns extra fuel
    fighting aerodynamic drag rather than buying orbital energy. A simple
    "altitude/10" speed guideline (a well-known practical rule of thumb
    for stock aero, not a rigorous optimal-control solution) -- fine
    above the atmosphere or once already slower than the limit."""
    if not atmosphere_depth or flight.mean_altitude >= atmosphere_depth:
        return 1.0
    speed_limit = max(flight.mean_altitude / 10.0, 100.0)
    if flight.speed <= speed_limit:
        return 1.0
    overspeed_frac = (flight.speed - speed_limit) / speed_limit
    return max(0.4, 1.0 - overspeed_frac)


def run_ascent(client, vessel, job, target_apoapsis_m, target_periapsis_m, target_inclination_deg=0.0,
               turn_start_altitude_m=10000, turn_end_altitude_m=45000):
    """turn_start_altitude_m defaults to 10km, not straight off the pad --
    staying vertical through the thick lower atmosphere (where most of the
    drag is) before starting to pitch over avoids fighting aerodynamic
    forces sideways while still low and slow; the turn happens entirely in
    thinner air instead."""
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    # A previous job (e.g. a moon-transfer's coast phase) can leave the
    # game time-warping when it ends -- confirmed live: a launch silently
    # sat on the pad at full commanded throttle while rails-warping at
    # 50x, since physics doesn't run normally under warp. Force back to
    # real time before doing anything else.
    if sc.rails_warp_factor != 0 or sc.physics_warp_factor != 0:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
        job.sleep(1.0)

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
    atmosphere_depth = body.atmosphere_depth
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

            control.throttle = _efficient_throttle(flight, atmosphere_depth)

            # Auto-staging: only once the current stage has neither thrust
            # nor any meaningful propellant left in it -- not just once
            # the currently-firing engine happens to read no thrust, but
            # confirmed against the stage's actual remaining resources
            # too (see _stage_has_fuel). Prefer a tagged decoupler for the
            # current stage number if one exists.
            if (vessel.available_thrust < 0.1 and not _stage_has_fuel(vessel, control.current_stage)
                    and control.current_stage not in fired_stages):
                stage_num = control.current_stage

                # Cut throttle for the actual separation instant -- full
                # throttle through the exact moment of decoupling maximizes
                # any plume impingement / collision torque against the
                # departing stage. Deliberately NOT touching the autopilot's
                # target here (first version of this fix read live
                # flight.pitch/flight.heading to "hold current attitude",
                # which caused a hard ~180 degree flip: heading is only
                # meaningful when not pointed near-vertical, and a rocket is
                # still near pitch=90 for its first stage or two -- reading
                # heading right there can return a near-arbitrary value,
                # and commanding the autopilot to chase that produced
                # exactly the flip-then-wobble-then-wasted-dv reported
                # live). The already-well-defined turn-angle target stays
                # in effect throughout; only the throttle is touched.
                control.throttle = 0.0

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
                control.activate_next_stage()
                fired_stages.add(stage_num)
                job.message = f"staged (stage {stage_num})"

                # Wait for the autopilot to actually confirm it's still on
                # target (ap.error small) before committing back to full
                # thrust, rather than a fixed delay -- a fixed pause can
                # restore full throttle while still meaningfully off-target
                # (e.g. from the CoM/moment-of-inertia shift at separation),
                # which just burns hard in a wrong direction and wastes dv.
                # Capped so a genuinely stuck autopilot doesn't stall the
                # ascent forever.
                settle_elapsed = 0.0
                while ap.error > 5 and settle_elapsed < 3.0:
                    job.check_abort()
                    job.sleep(0.1)
                    settle_elapsed += 0.1
                control.throttle = _efficient_throttle(flight, atmosphere_depth)

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
