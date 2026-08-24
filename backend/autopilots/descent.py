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

from backend import geo


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


def suicide_burn_landing(client, vessel, job, target_lat=None, target_lon=None, final_hover_altitude=15):
    control = vessel.control
    ap = vessel.auto_pilot
    body = vessel.orbit.body

    feasible, available_dv, required_dv = check_landing_feasible(vessel)
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
            altitude = flight.surface_altitude
            vertical_speed = -flight.vertical_speed  # positive = descending

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
            altitude = flight.surface_altitude
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
            if flight.surface_altitude <= 0.3 and abs(flight.vertical_speed) < 0.3:
                break
            target_descent_rate = -max(min(flight.surface_altitude / 5.0, 3.0), 0.5)
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
