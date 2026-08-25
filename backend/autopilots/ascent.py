"""Ascent autopilot: launch -> gravity turn -> automatic staging -> circularize.

The steering law here is a real gravity turn, not a pitch schedule. The
difference matters for fuel, which is the whole point of the maneuver:

A pitch schedule commands pitch purely as a function of altitude -- at 20km
you are at 45 degrees whether or not the rocket is actually *flying* in that
direction. Whenever commanded attitude and the velocity vector disagree, the
craft flies at an angle of attack: part of the thrust goes into turning
instead of accelerating (steering loss), and the airframe presents its side
to the airstream (extra drag, and real aerodynamic torque fighting the
autopilot). That was the previous implementation here.

A gravity turn instead uses gravity to do the turning. Pitch over by a few
degrees once moving fast enough to have aerodynamic authority (the "pitch
kick"), then simply hold *surface prograde*. Gravity pulls the velocity
vector steadily downrange, and because thrust stays aligned with velocity
there is no steering loss and near-zero angle of attack the whole way up.
The trajectory shape comes out of the physics rather than being dictated,
which is exactly why it is the efficient one.

Two guards on top of the pure law, both of which real launch vehicles also
need:
  * The angle of attack is clamped (see MAX_AOA_DEG). Pure prograde-holding
    has no restoring force if something knocks the velocity vector around --
    a staging transient, a gust, a thrust asymmetry -- so the commanded
    attitude is allowed to differ from prograde only by a few degrees while
    there is meaningful air. Above the atmosphere the clamp is irrelevant
    and the craft simply aims at the horizon to build orbital velocity.
  * There is a floor on how far the turn can lag (see `_schedule_pitch`).
    A very low thrust-to-weight craft left purely to gravity can fail to
    turn at all and fly straight up; the schedule acts as a "you should be
    at least this far over by now" backstop, never as the primary command.

Staging is delegated to backend/autopilots/staging.py, shared with the
mid-burn staging in maneuver.execute_node.
"""

import math

from backend.autopilots import maneuver, staging

# How far off the velocity vector the autopilot may ever command while in
# meaningful atmosphere. Small enough that drag stays near its
# zero-lift minimum, large enough to actually authority-correct a
# disturbance.
MAX_AOA_DEG = 5.0

# Surface speed at which to make the initial pitch kick. Too early and the
# fins/gimbal have no authority and the rocket flops; too late and it has
# already wasted propellant climbing vertically out of the thickest air.
PITCH_KICK_SPEED_MS = 60.0
PITCH_KICK_DEG = 3.0

# Horizontal speed at which "follow prograde" takes over from "hold the
# kick angle". Below this, the velocity vector is still dominated by the
# vertical climb and reading its angle as "prograde" is really just
# reading noise around 90 degrees -- not a real turn to follow.
KICK_ESTABLISHED_MS = 5.0

G0 = 9.80665


def _efficient_throttle(vessel, flight, atmosphere_depth):
    """Caps throttle (never raises it) to avoid overspeeding low in the
    atmosphere, where excess speed buys drag rather than orbital energy.

    The right cap is terminal velocity: the speed at which drag equals
    weight. Below it, thrust is mostly buying altitude and speed; push past
    it and an increasing share is spent pushing air aside, which is pure
    loss. kRPC exposes the live drag force and the vessel's mass, so this
    can be measured off the actual craft and the actual atmosphere rather
    than guessed at.

    Replaces an older `speed <= altitude/10` rule of thumb, which is a
    stock-aero folk heuristic with no dependence on the craft at all -- a
    draggy wide payload and a slender pencil rocket got the identical
    limit. Falls back to that heuristic only if the drag reading is
    unavailable, since it is still better than no cap at all.
    """
    if not atmosphere_depth or flight.mean_altitude >= atmosphere_depth:
        return 1.0

    try:
        drag = flight.drag
        drag_magnitude = math.sqrt(sum(c * c for c in drag))
        weight = vessel.mass * vessel.orbit.body.surface_gravity
        if drag_magnitude > weight * 1.05:
            # Past terminal velocity: ease off in proportion to the
            # overshoot rather than chopping to a fixed fraction, so the
            # craft settles near the limit instead of oscillating around it.
            excess = (drag_magnitude - weight) / weight
            return max(0.4, 1.0 - excess)
        return 1.0
    except Exception:
        speed_limit = max(flight.mean_altitude / 10.0, 100.0)
        if flight.speed <= speed_limit:
            return 1.0
        overspeed_frac = (flight.speed - speed_limit) / speed_limit
        return max(0.4, 1.0 - overspeed_frac)


def _schedule_pitch(altitude, turn_start_altitude_m, turn_end_altitude_m):
    """The backstop pitch profile: where the turn should have got to by a
    given altitude, worst case. Used only as a floor under the gravity
    turn, never as the command itself -- see `_gravity_turn_pitch`.

    Square-root rather than linear: a gravity turn naturally pitches over
    quickly at first (when it is slow and gravity has the most leverage on
    the velocity vector) and then flattens out, so a linear ramp would
    demand the craft still be near-vertical well after it should have
    turned.
    """
    if altitude <= turn_start_altitude_m:
        return 90.0
    if altitude >= turn_end_altitude_m:
        return 0.0
    frac = (altitude - turn_start_altitude_m) / (turn_end_altitude_m - turn_start_altitude_m)
    return 90.0 * (1.0 - math.sqrt(frac))


def _gravity_turn_pitch(flight, kicked, altitude, turn_start_altitude_m, turn_end_altitude_m):
    """The commanded pitch for this tick: follow prograde, clamped.

    Before the pitch kick the craft holds vertical. After it, the command
    is the craft's actual flight path angle (i.e. prograde) -- that is the
    gravity turn. The schedule floor then ensures a sluggish craft cannot
    simply refuse to turn.

    Confirmed live: PITCH_KICK_DEG never actually did anything, so the
    rocket flew dead vertical for the entire ascent. At the instant `kicked`
    goes true, the craft is still moving almost straight up -- vertical
    speed just crossed PITCH_KICK_SPEED_MS, horizontal speed is ~0 -- so
    prograde_pitch computes to essentially 90 degrees too. `max(90, floor)`
    is then 90 no matter what the floor says, forever: thrust stays
    vertical, so no horizontal velocity is ever produced, so prograde never
    leaves vertical, so the command never leaves 90. A "gravity turn" that
    never applies an actual kick has nothing to turn it.

    The fix is the kick has to be a real deviation, not a value that
    happens to equal prograde. Until the craft has built up a little real
    horizontal speed, command PITCH_KICK_DEG off vertical directly -- that
    is a genuine, if small, angle of attack, which is exactly what tips the
    velocity vector off vertical in the first place. Once horizontal speed
    is past KICK_ESTABLISHED_MS, the velocity vector has enough of its own
    momentum that following it (the normal gravity-turn law below) takes
    over and continues the turn on its own.
    """
    if not kicked:
        return 90.0

    horizontal = flight.horizontal_speed
    vertical = flight.vertical_speed

    if horizontal < KICK_ESTABLISHED_MS:
        # Still essentially straight up -- hold the deliberate kick angle
        # rather than "following prograde", which is indistinguishable from
        # vertical at this point and would command no turn at all.
        return 90.0 - PITCH_KICK_DEG

    # flight.pitch is the *nose* attitude; the flight path angle is what
    # prograde actually is, derived from the velocity components. Using the
    # nose angle here would make the loop chase its own tail (command =
    # current attitude is a no-op that freezes the turn wherever it started).
    prograde_pitch = math.degrees(math.atan2(vertical, horizontal))

    floor_pitch = _schedule_pitch(altitude, turn_start_altitude_m, turn_end_altitude_m)
    # max(): the floor may only *hold the nose up* relative to prograde,
    # never push it down -- pitching below prograde would mean deliberately
    # flying at negative angle of attack, which is the loss this whole
    # function exists to avoid.
    return max(prograde_pitch, floor_pitch)


def _clamp_to_aoa(commanded_pitch, flight, in_atmosphere):
    """Keep the commanded pitch within MAX_AOA_DEG of the actual velocity
    vector while there is air to fight. Outside the atmosphere the
    constraint is meaningless (no airstream, no drag penalty) and would
    only slow down the final flattening to the horizon."""
    if not in_atmosphere:
        return commanded_pitch
    horizontal = flight.horizontal_speed
    vertical = flight.vertical_speed
    if horizontal <= 0.1 and vertical <= 0.1:
        return commanded_pitch
    prograde_pitch = math.degrees(math.atan2(vertical, horizontal))
    return max(prograde_pitch - MAX_AOA_DEG, min(prograde_pitch + MAX_AOA_DEG, commanded_pitch))


def _delta_v_capacity(vessel):
    """Tsiolkovsky delta-v from current mass to dry mass at current Isp.
    Used only to report how much the ascent spent, so different steering
    tweaks can actually be compared between launches instead of judged by
    eye."""
    isp = vessel.specific_impulse
    if not isp or isp <= 0:
        return 0.0
    wet, dry = vessel.mass, vessel.dry_mass
    if wet <= dry or dry <= 0:
        return 0.0
    return isp * G0 * math.log(wet / dry)


def run_ascent(client, vessel, job, target_apoapsis_m, target_periapsis_m, target_inclination_deg=0.0,
               target_lan_deg=None, target_argp_deg=None,
               turn_start_altitude_m=1000, turn_end_altitude_m=45000):
    """The pitch kick fires purely on PITCH_KICK_SPEED_MS -- speed, not
    altitude, is what determines whether the craft has aerodynamic and
    gimbal authority to steer, and there is no altitude floor gating it.

    An earlier version also required `altitude >= turn_start_altitude_m`
    before the kick could fire, on the reasoning that a very high-thrust
    craft might otherwise pitch over while still metres off the pad. That
    reasoning doesn't hold up: PITCH_KICK_SPEED_MS is itself the real
    gate -- a craft can't be going 60 m/s while still on the pad -- so the
    altitude check was a redundant restriction that only ever delayed the
    turn, never protected anything. With prograde-following and the AoA
    clamp, there is no angle-of-attack risk to guard against by holding
    off, so turning the moment the craft has real airspeed is both safe
    and strictly cheaper: turning low and slow is exactly when gravity has
    the most leverage to do the work for free.

    turn_start_altitude_m therefore no longer gates the kick at all -- it
    only sets where `_schedule_pitch`'s floor starts ramping down, which is
    a backstop for a sluggish climb, not the normal path.
    """
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

    # The autopilot auto-tunes its own PID gains from vessel mass/torque,
    # and confirmed live: it can still end up oscillating even with that
    # on -- heading swinging a steady +/-2 degrees back and forth roughly
    # every 2 seconds, angular velocity cycling with it. Confirmed via the
    # autopilot's own built-in diagnostics mid-flight: pitch_yaw_control_
    # oscillation was nonzero and pitch_yaw_oscillation_latched was True,
    # i.e. it had detected and locked into an oscillating state itself.
    # target_smoothing_time defaults to 0 (no smoothing of target changes
    # at all) -- giving it some smooths out the response to each steering
    # update instead of snapping straight at it and overshooting.
    ap.target_smoothing_time = 0.5

    body = vessel.orbit.body
    # target_pitch_and_heading() interprets pitch/heading against
    # ap.reference_frame's own fixed axes -- body.reference_frame's "up"
    # is the body's north pole, not local vertical at the vessel, so pitch
    # 90 there points at the pole, not up (confirmed live: dot product of
    # the resulting direction with actual local-up was 0.27, not 1.0,
    # which pitched the rocket hard off-vertical and crashed it).
    # vessel.surface_reference_frame's axes are defined at the vessel's own
    # position (y = local zenith, x = local north), which is what
    # target_pitch_and_heading actually needs.
    ap.reference_frame = vessel.surface_reference_frame

    ap.engaged = True
    ap.target_pitch_and_heading(90, 90)
    job.message = "launching"

    flight = vessel.flight(body.reference_frame)
    atmosphere_depth = body.atmosphere_depth
    target_heading = 90 - target_inclination_deg

    dv_at_liftoff = _delta_v_capacity(vessel)
    stager = staging.Stager(vessel)
    settle = staging.settle_on_attitude(ap)
    kicked = False
    last_commanded_pitch = 90.0

    try:
        # --- Ascent: gravity turn + auto-staging ---
        while True:
            job.check_abort()
            altitude = flight.mean_altitude
            apoapsis = vessel.orbit.apoapsis_altitude
            in_atmosphere = bool(atmosphere_depth) and altitude < atmosphere_depth

            # Speed alone gates the kick -- no altitude floor. See the
            # docstring above for why an altitude gate here was a pure
            # restriction with nothing behind it.
            if not kicked and flight.speed >= PITCH_KICK_SPEED_MS:
                kicked = True
                job.message = "pitch kick -- starting gravity turn"

            if altitude >= turn_end_altitude_m or not in_atmosphere:
                # Out of the air (or high enough that the turn is done):
                # aim at the horizon and put everything into orbital speed.
                commanded_pitch = 0.0
            else:
                commanded_pitch = _gravity_turn_pitch(
                    flight, kicked, altitude, turn_start_altitude_m, turn_end_altitude_m,
                )
                commanded_pitch = _clamp_to_aoa(commanded_pitch, flight, in_atmosphere)

            # Only push a new target when it has actually moved. Every
            # target change restarts the autopilot's smoothing ramp, so
            # rewriting an unchanged value 4x/sec keeps it permanently in
            # its transient response instead of settled on the target.
            if abs(commanded_pitch - last_commanded_pitch) > 0.5:
                last_commanded_pitch = commanded_pitch
                ap.target_pitch_and_heading(commanded_pitch, target_heading)

            control.throttle = _efficient_throttle(vessel, flight, atmosphere_depth)

            # Auto-staging. verify_empty=True here (unlike mid-burn): at
            # launch the top stage also reads zero thrust simply because
            # nothing has ignited yet, and confirmed live, treating that as
            # "empty" blocked ignition entirely and the rocket never left
            # the pad. Checking the stage's actual remaining propellant
            # tells the two cases apart.
            stager.stage_if_dry(job, verify_empty=True, settle=settle)

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

    # The burn above is timed as an instantaneous impulse at apoapsis, which
    # is a bad approximation for a large burn on a low-TWR craft -- confirmed
    # live: a periapsis-raise burn requiring mid-burn staging took long
    # enough that it ran mostly *after* apoapsis instead of straddling it,
    # leaving periapsis basically untouched while apoapsis nearly tripled.
    # "orbit achieved" must mean the orbit is actually safe, not just that
    # the burn loop finished -- verify the real result and trim it.
    ok = maneuver.verify_and_trim_apsides(
        client, vessel, job,
        target_periapsis_m=target_periapsis_m, target_apoapsis_m=target_apoapsis_m,
    )
    if not ok:
        raise RuntimeError(
            f"circularization burn did not converge: periapsis="
            f"{vessel.orbit.periapsis_altitude / 1000:.1f} km, apoapsis="
            f"{vessel.orbit.apoapsis_altitude / 1000:.1f} km (target {target_periapsis_m / 1000:.0f}"
            f"/{target_apoapsis_m / 1000:.0f} km)"
        )

    # Heading control during the climb only holds the requested inclination
    # if attitude control actually held for the whole burn -- confirmed
    # live: a manual SAS toggle mid-ascent left the rocket thrusting with no
    # attitude control for a stretch, and it came out at 57 degrees against
    # a targeted 90. That's not something the ascent loop above can detect
    # on its own (it just keeps commanding pitch/heading; it has no idea the
    # commands stopped reaching the vessel) -- checking the actual resulting
    # inclination here and correcting it is what catches it.
    if not maneuver.verify_and_trim_inclination(
        client, vessel, job, target_inclination_deg, target_lan_deg=target_lan_deg,
    ):
        raise RuntimeError(
            f"couldn't correct inclination/plane to {target_inclination_deg:.1f} deg"
            f"{f', LAN {target_lan_deg:.1f} deg' if target_lan_deg is not None else ''} -- "
            f"still at {math.degrees(vessel.orbit.inclination):.1f} deg, "
            f"LAN {math.degrees(vessel.orbit.longitude_of_ascending_node) % 360:.1f} deg"
        )

    # Argument of periapsis only means anything on a real ellipse -- on a
    # circular target (the common case) kRPC's own reported value is
    # dominated by numerical noise, and "correcting" it would be both
    # meaningless and a wasted burn. Only bother when a genuinely distinct
    # periapsis was actually requested.
    if target_argp_deg is not None and target_periapsis_m < target_apoapsis_m * 0.999:
        if not maneuver.verify_and_trim_argument_of_periapsis(client, vessel, job, target_argp_deg):
            raise RuntimeError(
                f"couldn't correct argument of periapsis to {target_argp_deg:.1f} deg -- "
                f"still at {math.degrees(vessel.orbit.argument_of_periapsis) % 360:.1f} deg"
            )

    control.sas = True

    # Report what the climb actually cost. Staging mid-ascent makes this an
    # underestimate (delta-v capacity jumps back up when dead mass is
    # dropped), so it is a comparison figure between similar launches of
    # the same craft, not an absolute budget.
    dv_remaining = _delta_v_capacity(vessel)
    if dv_at_liftoff > 0:
        job.message = f"orbit achieved (~{dv_at_liftoff - dv_remaining:.0f} m/s used, {dv_remaining:.0f} m/s left)"
    else:
        job.message = "orbit achieved"
