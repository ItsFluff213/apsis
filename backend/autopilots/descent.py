"""Shared suicide-burn descent guidance.

Used by both the general waypoint-landing autopilot and the
booster-return-to-KSC autopilot for the actual touchdown. The main burn
points true retrograde (using kRPC's `flight.retrograde` direction vector
in the body's reference frame, fed to `auto_pilot.target_direction`) so the
full thrust vector cancels the actual velocity vector -- vertical and
horizontal together -- rather than pointing mostly straight up and only
weakly leaning toward the target, which wastes propellant on shallow,
mostly-horizontal reentries (confirmed by a real test: a booster returning
from near-orbital velocity ran the tank dry before killing its speed under
the old pitch-90-plus-a-15-degree-lean approach). Fine steering toward a
target waypoint is only applied in the final low-speed hover phase, where
it doesn't cost meaningful delta-v; the main deceleration burn is pure
retrograde. This is still an approximate guided descent, not a precision
solver -- expect to land near a target, not exactly on it.
"""

import math

from backend import geo, parts

# Stock parachutes rip off above roughly 250 m/s at sea-level pressure.
# Deploying below this is safe; the check is against dynamic pressure where
# available, since the same speed is harmless in thin air high up.
CHUTE_SAFE_SPEED_MS = 250.0
CHUTE_SAFE_DYNAMIC_PRESSURE = 12_000.0  # Pa

# Below this descent rate under canopy there is nothing for a landing burn
# to improve on -- the chutes have already done the job.
CHUTE_TERMINAL_DESCENT_MS = 12.0


def _flight(vessel):
    return vessel.flight(vessel.orbit.body.reference_frame)


def _max_deceleration(vessel):
    thrust = vessel.available_thrust
    mass = vessel.mass
    g = vessel.orbit.body.surface_gravity
    if mass <= 0 or thrust <= 0:
        return 0.1
    return max((thrust / mass) - g, 0.1)


def estimate_available_delta_v(vessel):
    """Tsiolkovsky delta-v from current (wet) mass down to dry mass, at
    the vessel's current Isp. Doesn't account for staging during the burn
    -- if the craft drops empty tanks partway through, real available
    delta-v is higher than this estimates."""
    isp = vessel.specific_impulse or 0
    if isp <= 0:
        return 0.0
    g0 = 9.80665
    wet, dry = vessel.mass, vessel.dry_mass
    if wet <= dry or dry <= 0:
        return 0.0
    return isp * g0 * math.log(wet / dry)


def check_landing_feasible(vessel, margin=1.3):
    """Rough go/no-go: is there plausibly enough delta-v left to cancel
    the current descent? `margin` accounts for gravity losses during the
    burn and imperfect guidance -- not exact, but far better than finding
    out by running dry mid-descent. Returns (feasible, available_dv,
    required_dv)."""
    flight = _flight(vessel)
    required = flight.speed * margin
    available = estimate_available_delta_v(vessel)
    return available >= required, available, required


def _commit_to_burnup(vessel, job, max_burn_s=5):
    """When a safe landing isn't feasible, don't waste remaining fuel
    attempting one and then crash uncontrolled anyway -- use whatever's
    left for a brief retrograde burn to push the periapsis as deep as
    possible (maximizing reentry heating/impact severity for a clean,
    controlled disposal), then stop. No point saving fuel for a landing
    that was never going to happen."""
    control = vessel.control
    ap = vessel.auto_pilot
    if vessel.available_thrust < 0.1:
        return False
    ap.engaged = True
    control.throttle = 1.0
    elapsed = 0.0
    try:
        while elapsed < max_burn_s and vessel.available_thrust > 0.1:
            job.check_abort()
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            ap.target_direction = flight.retrograde
            job.sleep(0.1)
            elapsed += 0.1
    finally:
        control.throttle = 0.0
        ap.engaged = False
    return True


def _chutes_are_safe_to_deploy(vessel, flight):
    """Whether deploying now would survive. Prefers dynamic pressure (the
    thing that actually rips a canopy) over raw speed, since 250 m/s in the
    upper atmosphere is harmless while the same speed low down is not."""
    try:
        if flight.dynamic_pressure > CHUTE_SAFE_DYNAMIC_PRESSURE:
            return False
        return True
    except Exception:
        return flight.speed <= CHUTE_SAFE_SPEED_MS


def deploy_parachutes(vessel, job, flight):
    """Deploy any parachutes that aren't already out. Returns True if this
    call deployed at least one."""
    deployed_any = False
    for part in parts.get_parachutes(vessel):
        try:
            chute = part.parachute
            if chute is None or chute.deployed:
                continue
            chute.deploy()
            deployed_any = True
        except Exception:
            # Same kRPC null-reference quirk seen on decouplers -- skip the
            # part rather than abandoning the whole deployment.
            continue
    if deployed_any:
        job.message = "parachutes deployed"
    return deployed_any


def hold_retrograde_through_reentry(client, vessel, job, body):
    """Keep the craft pointed retrograde while it is fast and deep enough
    for heating to matter, so a heatshield actually faces the airflow.

    This used to be an explicit scope gap -- the docs told you to point a
    heatshield manually before commanding a return. It matters even without
    a shield: a capsule tumbling through reentry presents an unpredictable
    cross-section, which wrecks the descent guidance's assumptions as much
    as it wrecks the craft.

    Returns once the craft has slowed to where the landing guidance can
    take over, or immediately on an airless body (nothing to reenter).
    """
    if not body.atmosphere_depth:
        return

    flight = _flight(vessel)
    if flight.mean_altitude > body.atmosphere_depth or flight.speed < CHUTE_SAFE_SPEED_MS:
        return  # not reentering, or already slow enough to skip the phase

    ap = vessel.auto_pilot
    control = vessel.control
    control.throttle = 0.0
    control.sas = False
    ap.reference_frame = body.reference_frame
    ap.engaged = True

    has_shield = parts.get_heatshield(vessel) is not None
    job.message = "reentry -- holding retrograde" + (" (heatshield forward)" if has_shield else "")

    while True:
        job.check_abort()
        flight = _flight(vessel)
        if flight.mean_altitude >= body.atmosphere_depth:
            break  # skipped back out of the atmosphere
        if flight.speed <= CHUTE_SAFE_SPEED_MS:
            break  # through the worst of it
        if flight.bedrock_altitude < 1000:
            break  # out of time -- let the landing guidance have it
        ap.target_direction = flight.retrograde
        job.sleep(0.2)


def _ride_chutes_down(client, vessel, job, body, retro_assist_speed=CHUTE_TERMINAL_DESCENT_MS):
    """Wait out a parachute descent to touchdown.

    Mostly this just watches. The one active part is the fallback at the
    bottom: if the craft is still coming down harder than chutes alone can
    handle (too heavy for its canopy area, or thin atmosphere like Duna's
    where chutes help but don't fully arrest), a short retrograde burn just
    above the ground takes the last of it off. That combination -- chutes
    for the bulk, engine for the final margin -- is far cheaper than
    powering the whole descent, and is how most real capsules with
    retro-rockets work.
    """
    control = vessel.control
    ap = vessel.auto_pilot
    ap.reference_frame = body.reference_frame
    ap.engaged = True
    job.message = "descending under parachutes"

    try:
        while True:
            job.check_abort()
            flight = _flight(vessel)
            descent_rate = -flight.vertical_speed

            # bedrock_altitude for the touchdown check too: over land this is
            # the same as being on the ground, and a splashdown is still
            # caught by the situation check right below regardless of how
            # deep the actual seabed is under the water.
            if flight.bedrock_altitude <= 0.5 and abs(flight.vertical_speed) < 0.5:
                break
            if vessel.situation.name in ("landed", "splashed"):
                break

            # Keep pointed retrograde so the craft stays stable under
            # canopy rather than swinging.
            try:
                ap.target_direction = flight.retrograde
            except Exception:
                pass

            # Retro-assist only in the last stretch, and only if actually
            # needed -- burning higher up just fights the canopy.
            if (flight.bedrock_altitude < 200 and descent_rate > retro_assist_speed
                    and vessel.available_thrust > 0.1):
                job.message = "parachute descent -- retro-assist for touchdown"
                control.throttle = min(1.0, (descent_rate - retro_assist_speed) / 10.0)
            else:
                control.throttle = 0.0

            job.sleep(0.2)
    finally:
        control.throttle = 0.0
        ap.engaged = False
        control.sas = True

    job.message = "landed under parachutes"


def _parachute_descent(client, vessel, job, body):
    """Full chute-only descent for a craft with no usable propellant: wait
    until the air is thick enough to deploy safely, then ride it down."""
    ap = vessel.auto_pilot
    ap.reference_frame = body.reference_frame
    ap.engaged = True
    vessel.control.throttle = 0.0

    job.message = "waiting for safe parachute deployment speed"
    while True:
        job.check_abort()
        flight = _flight(vessel)
        if vessel.situation.name in ("landed", "splashed"):
            job.message = "landed"
            return
        if _chutes_are_safe_to_deploy(vessel, flight) and flight.mean_altitude < body.atmosphere_depth:
            break
        try:
            ap.target_direction = flight.retrograde
        except Exception:
            pass
        job.sleep(0.5)

    deploy_parachutes(vessel, job, _flight(vessel))
    _ride_chutes_down(client, vessel, job, body)


def suicide_burn_landing(client, vessel, job, target_lat=None, target_lon=None, final_hover_altitude=15,
                         use_parachutes=True):
    control = vessel.control
    ap = vessel.auto_pilot
    body = vessel.orbit.body

    # Survive the heating first, if there is any -- no point planning a
    # touchdown for a craft that arrives as debris.
    hold_retrograde_through_reentry(client, vessel, job, body)

    # Chutes change the feasibility question completely: a craft with no
    # propellant left is doomed under power but perfectly fine under
    # canopy, so check for them before declaring a landing impossible.
    has_chutes = use_parachutes and bool(parts.get_parachutes(vessel))

    feasible, available_dv, required_dv = check_landing_feasible(vessel)
    if not feasible and has_chutes and body.atmosphere_depth:
        job.message = "not enough delta-v to land under power -- descending on parachutes"
        _parachute_descent(client, vessel, job, body)
        return
    if not feasible:
        job.message = (
            f"not enough delta-v for a safe landing ({available_dv:.0f} m/s available, "
            f"~{required_dv:.0f} m/s needed) -- committing to a controlled burnup instead "
            f"of a doomed landing attempt"
        )
        did_burn = _commit_to_burnup(vessel, job)
        job.message = "burnup" if did_burn else "no fuel left -- nothing could be done, left on its uncontrolled trajectory"
        return

    control.sas = False
    control.rcs = True
    ap.reference_frame = body.reference_frame
    ap.engaged = True

    def heading_toward_target(flight):
        if target_lat is None:
            return flight.heading  # no target: hold current heading
        return geo.bearing_to(flight.latitude, flight.longitude, target_lat, target_lon)

    try:
        # --- Coast, pointed retrograde, until it's time to burn ---
        job.message = "coasting toward touchdown"
        while True:
            job.check_abort()
            flight = _flight(vessel)
            # bedrock_altitude, not surface_altitude: the latter is defined
            # as height above the surface *or sea level, whichever is
            # closer* -- so anywhere the terrain dips below the sea-level
            # datum (not just open ocean; canyons, crater floors, coastal
            # shallows all count) it silently switches from terrain-relative
            # to water-relative mid-descent, handing this throttle
            # calculation a discontinuous jump in its input. bedrock_altitude
            # is always true distance to solid ground, continuous throughout.
            altitude = flight.bedrock_altitude
            vertical_speed = -flight.vertical_speed  # positive = descending

            # Chutes, if the craft has them and the air is thick enough for
            # them to bite. Deploying during the coast (rather than after a
            # burn) is the whole point: every m/s the canopy sheds is a m/s
            # the engine doesn't have to pay for.
            if has_chutes and body.atmosphere_depth and _chutes_are_safe_to_deploy(vessel, flight):
                if deploy_parachutes(vessel, job, flight):
                    control.throttle = 0.0
                    _ride_chutes_down(client, vessel, job, body)
                    return

            if altitude <= final_hover_altitude:
                break
            if vertical_speed > 0:
                max_accel = _max_deceleration(vessel)
                stopping_distance = (vertical_speed ** 2) / (2 * max_accel)
                if stopping_distance >= altitude - final_hover_altitude:
                    break

            ap.target_direction = flight.retrograde
            job.sleep(0.2)

        # --- Suicide burn: true retrograde, cancelling the full velocity
        # vector (vertical and horizontal together) -- not just vertical
        # speed with a token lean, which burns propellant far less
        # efficiently. ---
        job.message = "executing suicide burn"
        while True:
            job.check_abort()
            flight = _flight(vessel)
            altitude = flight.bedrock_altitude  # see the coast-phase comment above
            if altitude <= final_hover_altitude:
                break

            max_accel = _max_deceleration(vessel)
            stopping_distance = (flight.speed ** 2) / (2 * max_accel)

            ap.target_direction = flight.retrograde
            throttle = min(max(stopping_distance / max(altitude - final_hover_altitude, 1), 0.05), 1.0)
            control.throttle = throttle
            job.sleep(0.05)

        # --- Final hover-down to touchdown: low speed by now, so fine
        # steering toward the target is cheap here even though it wasn't
        # during the main burn. ---
        job.message = "final descent"
        while True:
            job.check_abort()
            flight = _flight(vessel)
            ap.target_pitch_and_heading(90, heading_toward_target(flight))
            # bedrock_altitude never reads ~0 on water (it's distance to the
            # seabed), so the situation check is what actually catches a
            # splashdown -- the altitude+speed check is what catches
            # touchdown on land.
            if (flight.bedrock_altitude <= 0.3 and abs(flight.vertical_speed) < 0.3) \
                    or vessel.situation.name in ("landed", "splashed"):
                break
            target_descent_rate = -max(min(flight.bedrock_altitude / 5.0, 3.0), 0.5)
            error = target_descent_rate - flight.vertical_speed
            control.throttle = min(max(0.5 + error * 0.15, 0.0), 1.0)
            job.sleep(0.05)
    finally:
        # Guaranteed even on abort/error -- see the comment in
        # maneuver.execute_node about why this can't just live after the
        # loops.
        control.throttle = 0.0
        ap.engaged = False
        control.sas = True

    job.message = "landed"
