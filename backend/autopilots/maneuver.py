"""Maneuver node helpers: create nodes for common orbital changes and
execute any node (orient, warp, burn). Shared by every autopilot in this
package so the node-execution logic lives in one place; auto-staging during
a burn is delegated to backend/autopilots/staging.py, which the ascent climb
uses too.
"""

import math
import time

from backend import orbital
from backend.autopilots import staging

# How far ahead to sample the second position for the finite-difference
# velocity below. Small enough that the approximation error is negligible
# at orbital distances/speeds, large enough not to lose precision to
# floating-point cancellation when subtracting two close positions.
_VELOCITY_FD_DT = 0.05


def velocity_at(orbit, ut, frame):
    """Velocity vector at a future time, on an orbit kRPC hasn't reached
    yet.

    kRPC's Orbit class has `position_at(ut, frame)` but no vector
    `velocity_at` -- confirmed live: code in this project assumed one
    existed (built by analogy with `position_at`, never checked against
    the real API) and crashed the first time it actually ran, with
    `'Orbit' object has no attribute 'velocity_at'`. The scalar
    `orbital_speed_at(ut)` does exist, but a scalar has no direction to
    give the caller.

    Reconstructed from `position_at`, which does exist, via a CENTERED
    finite difference -- not a forward one. That distinction is not
    pedantic here: a forward difference's error is first-order in dt
    (scales with local acceleration, mu/r^2), which at a low orbit around
    something as dense as Mun is large enough to matter -- confirmed by a
    test built around ground-truth analytic velocity, which the forward
    version failed at the 1e-9 relative tolerance a burn planner actually
    needs. Centering the sample around `ut` cancels that leading error
    term, making the result accurate to O(dt^2) instead of O(dt) for the
    same step size -- a large accuracy gain for a one-line change.
    """
    r1 = orbit.position_at(ut - _VELOCITY_FD_DT, frame)
    r2 = orbit.position_at(ut + _VELOCITY_FD_DT, frame)
    return tuple((b - a) / (2 * _VELOCITY_FD_DT) for a, b in zip(r1, r2))


def vis_viva_speed(mu, r, a):
    return math.sqrt(mu * (2.0 / r - 1.0 / a))


def estimate_burn_time(vessel, delta_v):
    """Tsiolkovsky-based estimate of how long a burn of this delta-v takes
    at the vessel's current mass/thrust/Isp."""
    isp = vessel.specific_impulse or 250
    g0 = 9.80665
    m0 = vessel.mass
    max_thrust = vessel.available_thrust or 1
    m1 = m0 / math.exp(delta_v / (isp * g0))
    flow_rate = max_thrust / (isp * g0)
    if flow_rate <= 0:
        return 1.0
    return (m0 - m1) / flow_rate


def circularize_node(client, vessel, at="apoapsis"):
    """Adds a node that circularizes the orbit at the next apoapsis or
    periapsis and returns it."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if at == "apoapsis":
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, r)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis"):
    """Adds a node (burned at the next apoapsis or periapsis) that changes
    the periapsis altitude to the given value. A negative target altitude
    is fine -- it's used for deorbit burns aimed at impact/atmosphere."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if burn_at == "apoapsis":
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_periapsis_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def adjust_other_apsis_now(client, vessel, target_altitude_m, lead_time=1.0):
    """Burns prograde right at the vessel's current position to set the
    *other* apsis (whichever one the vessel isn't currently at) to a given
    altitude -- the general version of "burn now" rather than waiting for
    an actual apsis passage. The vis-viva math doesn't care which apsis is
    which; this only cleanly hits the target if fired reasonably close to
    an actual apsis of the current orbit, since elsewhere the current
    radius isn't really "the other apsis" of the resulting orbit either.
    Two uses: an emergency collision-avoidance correction (raise periapsis
    now, imprecisely, rather than arrive at the real apoapsis after
    already flying too close to impact), and a mid-course correction
    partway through a coast (nudge the far apsis toward a freshly
    recomputed target instead of trusting the original burn to have been
    exact)."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    r = math.sqrt(sum(c * c for c in vessel.position(body.reference_frame)))
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_altitude_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(sc.ut + lead_time, prograde=v2 - v1)


def change_apoapsis_node(client, vessel, target_apoapsis_m, burn_at="periapsis"):
    """Adds a node (burned at the next periapsis or apoapsis) that changes
    the apoapsis altitude to the given value, leaving the other apsis
    where it is. Mirror of change_periapsis_node."""
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    if burn_at == "periapsis":
        ut = sc.ut + vessel.orbit.time_to_periapsis
        r = body.equatorial_radius + vessel.orbit.periapsis_altitude
    else:
        ut = sc.ut + vessel.orbit.time_to_apoapsis
        r = body.equatorial_radius + vessel.orbit.apoapsis_altitude
    a1 = vessel.orbit.semi_major_axis
    a2 = (r + body.equatorial_radius + target_apoapsis_m) / 2.0
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a2)
    return vessel.control.add_node(ut, prograde=v2 - v1)


def verify_and_trim_apsides(client, vessel, job, target_periapsis_m=None, target_apoapsis_m=None,
                             tolerance_frac=0.05, max_corrections=4):
    """Check the actual resulting orbit against target apsides after a burn,
    and fire immediate corrective burns if either is off by more than
    tolerance_frac (minimum 500m, so small orbits don't get chased down to
    the metre).

    change_periapsis_node/change_apoapsis_node schedule a burn timed as an
    instantaneous impulse at a specific apsis. That assumption quietly
    breaks down for a *large* burn on a low-TWR craft -- confirmed live: a
    periapsis-raise burn needing ~850 m/s took 48 real seconds and required
    staging mid-burn, which invalidated execute_node's up-front burn-time
    estimate (used only to centre the burn around the planned apsis). The
    actual burn ended up running mostly *after* the intended apoapsis point
    instead of straddling it, and the result was not a small rounding error:
    apoapsis nearly tripled (80km -> 206km) while periapsis was barely
    touched, on a craft that was supposed to already be in a safe orbit.
    That target was never reached, so the ascent autopilot reported "orbit
    achieved" over an orbit that decayed and crashed the vessel minutes
    later.

    Rather than trying to predict burn duration perfectly (which would need
    a full multi-stage delta-v simulator to handle mid-burn staging), this
    checks what orbit actually resulted and corrects it directly. A trim
    burn is always small relative to the original -- most of the delta-v
    already landed -- so it isn't expected to need its own staging event or
    similarly distort a third apsis. Bounded by max_corrections so a
    persistently-wrong result triggers a visible failure instead of an
    infinite correction loop.
    """
    def _needs_fix(current, target):
        return target is not None and abs(target - current) > max(500.0, abs(target) * tolerance_frac)

    burns_fired = 0
    while burns_fired < max_corrections:
        # Which apsis is "now" (a quick adjust_other_apsis_now burn can hit
        # the far one immediately) versus which is "half an orbit away"
        # (needs a properly timed node, burned at the apsis we're actually
        # near, same as the initial burn would have used) depends on where
        # the vessel currently sits -- re-read it every pass since each
        # correction burn moves the vessel.
        near_apoapsis = vessel.orbit.time_to_apoapsis < vessel.orbit.time_to_periapsis
        peri, apo = vessel.orbit.periapsis_altitude, vessel.orbit.apoapsis_altitude

        if _needs_fix(peri, target_periapsis_m):
            job.message = f"trimming periapsis ({peri / 1000:.1f} km -> {target_periapsis_m / 1000:.1f} km)"
            if near_apoapsis:
                factory = lambda: adjust_other_apsis_now(client, vessel, target_periapsis_m)  # noqa: E731
            else:
                factory = lambda: change_periapsis_node(client, vessel, target_periapsis_m, burn_at="apoapsis")  # noqa: E731
            execute_node_retrying(client, vessel, job, factory)
            burns_fired += 1
            continue  # position has changed -- re-derive near_apoapsis before the apoapsis check

        if _needs_fix(apo, target_apoapsis_m):
            job.message = f"trimming apoapsis ({apo / 1000:.1f} km -> {target_apoapsis_m / 1000:.1f} km)"
            if not near_apoapsis:
                factory = lambda: adjust_other_apsis_now(client, vessel, target_apoapsis_m)  # noqa: E731
            else:
                factory = lambda: change_apoapsis_node(client, vessel, target_apoapsis_m, burn_at="periapsis")  # noqa: E731
            execute_node_retrying(client, vessel, job, factory)
            burns_fired += 1
            continue

        return True  # neither apsis needs fixing -- converged

    peri, apo = vessel.orbit.periapsis_altitude, vessel.orbit.apoapsis_altitude
    return not (_needs_fix(peri, target_periapsis_m) or _needs_fix(apo, target_apoapsis_m))


def change_inclination_node(client, vessel, target_inclination_deg):
    """Adds a node at the next ascending or descending node (whichever is
    cheaper -- see below) that changes the orbit's inclination to the
    target, leaving the orbit's shape (semi-major axis and eccentricity,
    hence periapsis/apoapsis) unchanged.

    Built from the actual position and velocity vectors at the burn time,
    not from the scalar vis-viva speed at that radius. That distinction
    matters, and got this function wrong for a real flight before it was
    caught: at any point that isn't a periapsis or apoapsis, velocity has
    both a tangential (prograde) component AND a radial one. An earlier
    version computed only the scalar speed `v = vis_viva_speed(...)` and
    split it into `prograde_dv = v*(cos(di)-1)` / `normal_dv = v*sin(di)`,
    which implicitly assumes the ENTIRE velocity is prograde at the burn
    point -- true only exactly at an apsis, or anywhere on a circular
    orbit. That assumption was harmless everywhere this function had been
    called before (fresh, near-circular parking orbits), but this project
    also now calls it deliberately off-apsis, on a still-eccentric orbit,
    to rotate the plane before circularizing (cheaper that way -- see
    below). Confirmed live: on a real Mun arrival, that combination
    silently corrupted the orbit's shape while still rotating the plane
    correctly, leaving a vessel in a 320km-ish orbit instead of the
    requested 100km, with the *right* inclination and the *wrong*
    everything else -- a hard bug to notice by watching, since the ship
    was clearly headed into a clean-looking circular orbit right up until
    the final numbers didn't match, and it took a look at eccentricity
    (accidentally almost 0 -- misleadingly "clean") and periapsis together
    to see the plane change was where the meters had gone.

    The fix: read the vessel's actual velocity and position at the burn
    time, and rotate the FULL velocity vector by delta_inclination about
    the axis defined by the position vector. At a true node, the position
    vector lies exactly along the line of nodes -- the intersection of the
    old and new orbital planes -- so rotating velocity about that axis by
    the desired angle changes the plane by exactly that angle while
    leaving both the vector's magnitude (so: energy, so: semi-major axis)
    and the magnitude of position-cross-velocity (so: angular momentum, so:
    eccentricity) untouched, for any point on the orbit, not just apsides.
    This is the general, always-correct version of what the old formula
    only approximated at an apsis.

    A plane change is priced by how fast you are travelling when you make
    it -- the cost scales directly with orbital speed -- so on an
    eccentric orbit it matters enormously *where* it happens. Both the
    ascending and descending node are valid points to rotate the plane
    about, and they sit half an orbit apart, so one of them is always the
    higher (slower, cheaper) of the two. This picks that one.

    Concretely, for a 90 degree change around Mun: on the elliptical
    capture orbit, at apoapsis, roughly 166 m/s. On a circular 50km orbit,
    722 m/s. Same maneuver, four times the fuel, purely from when it is
    done. On a circular orbit both nodes are equivalent and this reduces to
    the old behaviour."""
    sc = client.space_center
    orbit = vessel.orbit
    delta_inclination = math.radians(target_inclination_deg) - orbit.inclination

    ta_ascending = (-orbit.argument_of_periapsis) % (2 * math.pi)
    ta_descending = (ta_ascending + math.pi) % (2 * math.pi)

    # Whichever node is further out is the slower one, and therefore the
    # cheaper place to rotate the plane.
    candidates = []
    for true_anomaly in (ta_ascending, ta_descending):
        radius = orbit.radius_at_true_anomaly(true_anomaly)
        candidates.append((radius, true_anomaly))
    _, true_anomaly = max(candidates)

    ut = orbit.ut_at_true_anomaly(true_anomaly)
    if ut < sc.ut:
        ut += orbit.period

    frame = orbit.body.non_rotating_reference_frame
    position = orbit.position_at(ut, frame)
    velocity = velocity_at(orbit, ut, frame)

    # Rotate about the line of nodes pointing toward the ASCENDING node
    # specifically -- not the raw position vector at the burn point.
    # Confirmed live: those are the same vector only when the burn happens
    # to land at the ascending node. At the descending node, position
    # points the opposite way, and rotating by the identical signed
    # delta_inclination about that reversed axis silently inverts the
    # result -- the first plane change this session picked the ascending
    # node and worked perfectly; the next one picked the descending node
    # on a different orbit and the inclination moved the *wrong* direction
    # while the burn also visibly distorted the orbit's shape. Reproduced
    # numerically: rotating about `position` gives the right answer at one
    # node and a badly wrong one at the other; rotating about the
    # body-relative-y-axis line of nodes (fixed for the orbit, independent
    # of which node the burn is actually at) gives the right answer at
    # both.
    normal_now = orbital.norm(orbital.cross(position, velocity))
    line_of_nodes_raw = orbital.cross((0.0, 1.0, 0.0), normal_now)
    if orbital.magnitude(line_of_nodes_raw) < 1e-9:
        # Degenerate case: current orbit is already (near-)equatorial, so
        # its normal is (near-)parallel to the pole axis and there is no
        # well-defined line of nodes -- every point is equally valid as a
        # reference, which is exactly what makes `position` itself safe to
        # use directly here (this is the one case the old code had right).
        rotation_axis = position
    else:
        rotation_axis = orbital.norm(line_of_nodes_raw)
    rotated_velocity = orbital.rotate_about_axis(velocity, rotation_axis, delta_inclination)
    delta_v_vector = tuple(r - v for r, v in zip(rotated_velocity, velocity))

    # add_node's prograde/normal/radial axes are defined at the node's own
    # UT, not "now" -- built from the same position/velocity already read
    # above so they line up with the burn point exactly, not the vessel's
    # current (different) position.
    prograde_hat = orbital.norm(velocity)
    normal_hat = orbital.norm(orbital.cross(position, velocity))
    # In-plane, perpendicular to prograde -- kRPC's node "radial" axis.
    radial_hat = orbital.norm(orbital.cross(normal_hat, prograde_hat))

    prograde_dv = orbital.dot(delta_v_vector, prograde_hat)
    normal_dv = orbital.dot(delta_v_vector, normal_hat)
    radial_dv = orbital.dot(delta_v_vector, radial_hat)

    return vessel.control.add_node(ut, prograde=prograde_dv, normal=normal_dv, radial=radial_dv)


def _plane_normal(position, velocity):
    """The orbit's normal, in the sign convention kRPC itself uses for
    `orbit.inclination` / `orbit.longitude_of_ascending_node` -- confirmed
    live against a dozen real vessels of varying inclination (0.1 to 158
    degrees): `cross(position, velocity)` (the textbook specific angular
    momentum vector, and what change_inclination_node above uses for its
    own *internal*, self-consistent local frame) reads exactly opposite to
    kRPC's own inclination on every one of them. `cross(velocity, position)`
    matches exactly, both for inclination and, with the node/LAN formulas
    below, for longitude of ascending node too. Get this backwards and
    every angle in change_orbital_plane_node comes out either right or its
    180-degree-flipped twin, depending on which way it happens to be
    checked -- exactly the kind of error that looks correct in a quick
    self-test and wrong against the real game."""
    return orbital.norm(orbital.cross(velocity, position))


def _lan_of(normal):
    """Longitude of the ascending node implied by an orbit normal, in the
    same calibrated convention as _plane_normal -- inverse of the
    construction in change_orbital_plane_node's target_normal."""
    node = orbital.norm(orbital.cross(normal, (0.0, 1.0, 0.0)))
    return math.degrees(math.atan2(orbital.dot(node, (0.0, 0.0, 1.0)), orbital.dot(node, (1.0, 0.0, 0.0)))) % 360.0


def change_orbital_plane_node(client, vessel, target_inclination_deg, target_lan_deg):
    """Adds a node that rotates the orbital plane to a target inclination
    AND a target longitude of ascending node together, in one burn --
    change_inclination_node above only ever changes inclination while
    implicitly preserving whatever LAN the orbit already has (it rotates
    about the orbit's OWN current line of nodes, which by construction
    doesn't move the node). Setting a specific LAN needs a different burn
    point: not either of the current orbit's own nodes, but wherever the
    orbit crosses the line where the CURRENT and TARGET planes intersect --
    the "mutual node" of the two planes, not a node of either one alone.

    Verified against a from-scratch Keplerian simulator (not just this
    project's math) before being written here: build the true target
    normal vector from (inclination, LAN), take the axis and angle that
    rotate the current normal directly onto it
    (axis = current x target, angle = angle between them), find the true
    anomaly where the orbit crosses that axis (there are two, half an orbit
    apart -- pick the farther/cheaper one, same reasoning as
    change_inclination_node), and rotate the velocity there by that same
    axis and angle. Checked round-trip against a dozen (start, target)
    combinations spanning near-equatorial to near-polar and both windings
    of LAN: every one landed on the exact target inclination and LAN while
    leaving semi-major axis and eccentricity untouched.
    """
    sc = client.space_center
    orbit = vessel.orbit
    frame = orbit.body.non_rotating_reference_frame

    ut_peri = orbit.ut_at_true_anomaly(0.0)
    peri_position = orbit.position_at(ut_peri, frame)
    peri_velocity = velocity_at(orbit, ut_peri, frame)
    current_normal = _plane_normal(peri_position, peri_velocity)
    peri_hat = orbital.norm(peri_position)
    perp_hat = orbital.norm(orbital.cross(peri_hat, current_normal))

    target_i = math.radians(target_inclination_deg)
    target_normal_at_lan0 = (0.0, math.cos(target_i), -math.sin(target_i))
    target_normal = orbital.rotate_about_axis(
        target_normal_at_lan0, (0.0, 1.0, 0.0), -math.radians(target_lan_deg),
    )

    cross_normals = orbital.cross(current_normal, target_normal)
    if orbital.magnitude(cross_normals) < 1e-9:
        # Already on (or exactly opposite) the target plane -- e.g. a plane
        # match called right after an ascent already put it there. Nothing
        # to rotate about; fall back to the ordinary inclination-only path,
        # which reduces to a same-plane no-op-sized correction if needed.
        return change_inclination_node(client, vessel, target_inclination_deg)
    axis = orbital.norm(cross_normals)
    angle = math.acos(max(-1.0, min(1.0, orbital.dot(current_normal, target_normal))))

    true_anomaly_1 = math.atan2(orbital.dot(axis, perp_hat), orbital.dot(axis, peri_hat))
    true_anomaly_2 = true_anomaly_1 + math.pi
    candidates = [
        (orbit.radius_at_true_anomaly(nu), nu) for nu in (true_anomaly_1, true_anomaly_2)
    ]
    _, true_anomaly = max(candidates)

    ut = orbit.ut_at_true_anomaly(true_anomaly)
    if ut < sc.ut:
        ut += orbit.period

    position = orbit.position_at(ut, frame)
    velocity = velocity_at(orbit, ut, frame)
    rotated_velocity = orbital.rotate_about_axis(velocity, axis, angle)
    delta_v_vector = tuple(r - v for r, v in zip(rotated_velocity, velocity))

    prograde_hat = orbital.norm(velocity)
    normal_hat = orbital.norm(orbital.cross(position, velocity))
    radial_hat = orbital.norm(orbital.cross(normal_hat, prograde_hat))

    prograde_dv = orbital.dot(delta_v_vector, prograde_hat)
    normal_dv = orbital.dot(delta_v_vector, normal_hat)
    radial_dv = orbital.dot(delta_v_vector, radial_hat)

    return vessel.control.add_node(ut, prograde=prograde_dv, normal=normal_dv, radial=radial_dv)


def change_argument_of_periapsis_node(client, vessel, target_argp_deg):
    """Adds a node that rotates where periapsis sits along the orbit to a
    target argument of periapsis, leaving semi-major axis, eccentricity,
    inclination, and longitude of ascending node all untouched.

    A different maneuver from change_orbital_plane_node above -- that one
    rotates the plane itself (the orbit's normal vector); this one rotates
    the eccentricity vector *within* a plane that doesn't move. The current
    orbit and an orbit identical in every way except argument of periapsis
    are two distinct ellipses in the same plane, and (for any two ellipses
    sharing a and e) they intersect at exactly two points: the ones
    equidistant, in true anomaly, from both periapsis directions -- i.e.
    true anomaly = (target_argp - current_argp) / 2 from the current
    periapsis (and its antipode, half an orbit later). At that point both
    ellipses agree on position, so a single burn there that matches the
    *other* ellipse's velocity switches from one to the other with nothing
    else disturbed.

    Verified against a from-scratch Keplerian simulator before being
    written here: for several (start, target) argument-of-periapsis pairs,
    computing position on the current ellipse and velocity on the target
    ellipse at that shared point landed exactly on the target argument of
    periapsis while leaving a, e, inclination, and LAN unchanged, and the
    two ellipses' positions at that point matched to numerical noise --
    confirming it's a genuine intersection, not an approximation.
    """
    sc = client.space_center
    orbit = vessel.orbit
    frame = orbit.body.non_rotating_reference_frame
    mu = orbit.body.gravitational_parameter
    a = orbit.semi_major_axis
    e = orbit.eccentricity

    delta_argp = math.radians(target_argp_deg) - orbit.argument_of_periapsis

    ut_peri = orbit.ut_at_true_anomaly(0.0)
    peri_position = orbit.position_at(ut_peri, frame)
    peri_velocity = velocity_at(orbit, ut_peri, frame)
    normal = _plane_normal(peri_position, peri_velocity)
    peri_hat = orbital.norm(peri_position)

    true_anomaly_1 = delta_argp / 2.0
    true_anomaly_2 = true_anomaly_1 + math.pi
    candidates = [(orbit.radius_at_true_anomaly(nu), nu) for nu in (true_anomaly_1, true_anomaly_2)]
    _, true_anomaly = max(candidates)

    ut = orbit.ut_at_true_anomaly(true_anomaly)
    if ut < sc.ut:
        ut += orbit.period

    position = orbit.position_at(ut, frame)
    velocity = velocity_at(orbit, ut, frame)

    # Velocity for the TARGET ellipse at this same physical point -- the
    # vis-viva radial/tangential decomposition evaluated at the true
    # anomaly that point corresponds to *relative to the target periapsis*
    # (which, by the symmetric choice of burn point above, is just
    # true_anomaly - delta_argp).
    new_true_anomaly = true_anomaly - delta_argp
    h = math.sqrt(mu * a * (1 - e ** 2))
    v_r = (mu / h) * e * math.sin(new_true_anomaly)
    v_t = h / orbital.magnitude(position)
    radial_hat = orbital.norm(position)
    tangential_hat = orbital.norm(orbital.cross(radial_hat, normal))
    new_velocity = tuple(v_r * rh + v_t * th for rh, th in zip(radial_hat, tangential_hat))

    delta_v_vector = tuple(nv - v for nv, v in zip(new_velocity, velocity))

    prograde_hat = orbital.norm(velocity)
    normal_hat = orbital.norm(orbital.cross(position, velocity))
    radial_dv_hat = orbital.norm(orbital.cross(normal_hat, prograde_hat))

    prograde_dv = orbital.dot(delta_v_vector, prograde_hat)
    normal_dv = orbital.dot(delta_v_vector, normal_hat)
    radial_dv = orbital.dot(delta_v_vector, radial_dv_hat)

    return vessel.control.add_node(ut, prograde=prograde_dv, normal=normal_dv, radial=radial_dv)


def verify_and_trim_inclination(client, vessel, job, target_inclination_deg, tolerance_deg=1.0,
                                 target_lan_deg=None):
    """Check the actual resulting inclination (and, if given, longitude of
    ascending node) against target, and fire a plane-change correction if
    either is off by more than tolerance_deg.

    Same reasoning as verify_and_trim_apsides, for a different failure mode:
    heading control during ascent is only as good as the autopilot's
    attitude authority for the *entire* burn. Confirmed live: a manual SAS
    toggle mid-ascent left the rocket thrusting at full throttle with no
    attitude control for a stretch, and the resulting orbit came out at 57
    degrees against a targeted 90 -- a large, silent miss that "orbit
    achieved" would otherwise have reported as a success. Checking the
    actual inclination and correcting it catches that regardless of what
    corrupted the heading in the first place.
    """
    current = math.degrees(vessel.orbit.inclination)
    current_lan = math.degrees(vessel.orbit.longitude_of_ascending_node) % 360.0
    lan_ok = target_lan_deg is None or abs(((current_lan - target_lan_deg + 180) % 360) - 180) <= tolerance_deg
    if abs(current - target_inclination_deg) <= tolerance_deg and lan_ok:
        return True

    if target_lan_deg is None:
        job.message = (
            f"inclination is {current:.1f} deg, not the targeted {target_inclination_deg:.1f} -- correcting"
        )
        execute_node_retrying(
            client, vessel, job,
            lambda: change_inclination_node(client, vessel, target_inclination_deg),
        )
    else:
        job.message = (
            f"plane is ({current:.1f} deg, LAN {current_lan:.1f} deg), not the targeted "
            f"({target_inclination_deg:.1f} deg, LAN {target_lan_deg:.1f} deg) -- correcting"
        )
        execute_node_retrying(
            client, vessel, job,
            lambda: change_orbital_plane_node(client, vessel, target_inclination_deg, target_lan_deg),
        )

    current = math.degrees(vessel.orbit.inclination)
    current_lan = math.degrees(vessel.orbit.longitude_of_ascending_node) % 360.0
    lan_ok = target_lan_deg is None or abs(((current_lan - target_lan_deg + 180) % 360) - 180) <= tolerance_deg
    return abs(current - target_inclination_deg) <= tolerance_deg and lan_ok


def verify_and_trim_argument_of_periapsis(client, vessel, job, target_argp_deg, tolerance_deg=1.0):
    """Check the actual resulting argument of periapsis against target, and
    fire a correction if it's off by more than tolerance_deg. Same
    verify-the-real-result pattern as the apsides/inclination trims above.

    Meaningless on a near-circular orbit -- periapsis direction is only
    well-defined when there's a real ellipse to have one on, so this is
    only useful (and only meant to be called) when a distinct periapsis
    was actually requested."""
    current = math.degrees(vessel.orbit.argument_of_periapsis) % 360.0
    if abs(((current - target_argp_deg + 180) % 360) - 180) <= tolerance_deg:
        return True

    job.message = (
        f"argument of periapsis is {current:.1f} deg, not the targeted {target_argp_deg:.1f} -- correcting"
    )
    execute_node_retrying(
        client, vessel, job,
        lambda: change_argument_of_periapsis_node(client, vessel, target_argp_deg),
    )

    current = math.degrees(vessel.orbit.argument_of_periapsis) % 360.0
    return abs(((current - target_argp_deg + 180) % 360) - 180) <= tolerance_deg


def phasing_node(client, vessel, angle_to_close_deg, num_orbits=1, burn_at="apoapsis"):
    """Adds a node that puts the vessel on a temporary phasing orbit: its
    period is adjusted so that after `num_orbits` loops back to the burn
    point, the vessel has drifted `angle_to_close_deg` relative to where
    it would be on the current (target) orbit. Positive angle means "get
    there sooner" (a lower, faster phasing orbit); negative means "arrive
    later" (higher, slower). Caller is responsible for checking the
    resulting orbit stays above a safe altitude (a large angle over few
    orbits can demand a very low or very high phasing orbit) and for
    circularizing back once the phasing orbit completes.

    Returns (node, phase_period_s, phase_periapsis_altitude_m) so the
    caller can sanity-check safety before committing to the burn.
    """
    sc = client.space_center
    orbit = vessel.orbit
    body = orbit.body
    mu = body.gravitational_parameter
    target_period = orbit.period
    angle_frac = angle_to_close_deg / 360.0
    phase_period = target_period * (1 - angle_frac / num_orbits)
    phase_period = max(phase_period, target_period * 0.05)

    a_phase = (mu * phase_period ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)

    if burn_at == "apoapsis":
        ut = sc.ut + orbit.time_to_apoapsis
        r = body.equatorial_radius + orbit.apoapsis_altitude
    else:
        ut = sc.ut + orbit.time_to_periapsis
        r = body.equatorial_radius + orbit.periapsis_altitude

    a1 = orbit.semi_major_axis
    v1 = vis_viva_speed(mu, r, a1)
    v2 = vis_viva_speed(mu, r, a_phase)
    node = vessel.control.add_node(ut, prograde=v2 - v1)

    phase_periapsis_altitude = 2 * a_phase - r - body.equatorial_radius
    return node, phase_period, phase_periapsis_altitude


def closest_other_vessel_distance(client, vessel):
    """Distance (m) to the nearest other vessel kRPC currently knows about,
    or None if there isn't one."""
    body_frame = vessel.orbit.body.reference_frame
    my_pos = vessel.position(body_frame)
    closest = None
    for other in client.space_center.vessels:
        if other == vessel:
            continue
        other_pos = other.position(body_frame)
        dist = sum((a - b) ** 2 for a, b in zip(my_pos, other_pos)) ** 0.5
        if closest is None or dist < closest:
            closest = dist
    return closest


def burn_away_from_debris(client, vessel, job, min_distance_m=50, max_burn_s=6, throttle=0.6):
    """Briefly burns roughly prograde (holding current attitude) right
    after a stage separates, so the continuing vessel actively opens
    distance from the piece it just dropped instead of coasting right next
    to it. Real rockets do this too -- burning straight back toward the
    launch site immediately after staging (as the dropped booster's return
    autopilot does) is otherwise liable to fly right back into whatever it
    just separated from, since both pieces start out at nearly the same
    position and velocity."""
    control = vessel.control
    ap = vessel.auto_pilot
    was_engaged = ap.engaged
    ap.engaged = True

    job.message = "burning clear of separated stage"
    # Hold whatever attitude the vessel was AT THE MOMENT this burn started,
    # not whatever it happens to be each tick -- a separation event can
    # leave the vessel briefly wobbling, and continuously re-targeting the
    # live (still-settling) pitch/heading was chasing that wobble instead
    # of damping it, visibly worsening it right during this burn. Confirmed
    # live: a real tumble (AoA -21 deg, 14.8 deg autopilot error) that
    # coincided exactly with this phase, which then recovered as soon as
    # the main ascent's fixed steering target took back over afterward.
    flight = vessel.flight(vessel.orbit.body.reference_frame)
    target_pitch, target_heading = flight.pitch, flight.heading
    ap.target_pitch_and_heading(target_pitch, target_heading)
    control.throttle = throttle
    elapsed = 0.0
    try:
        while elapsed < max_burn_s:
            job.check_abort()
            dist = closest_other_vessel_distance(client, vessel)
            if dist is not None and dist >= min_distance_m:
                break
            job.sleep(0.1)
            elapsed += 0.1
    finally:
        control.throttle = 0.0
        ap.engaged = was_engaged


def execute_node(client, vessel, job, node, lead_time=15):
    """Orients the vessel to a node, warps to it, and burns it out."""
    sc = client.space_center
    ap = vessel.auto_pilot
    control = vessel.control
    body = vessel.orbit.body

    def _decaying_now():
        """True if the orbit is losing real energy to the atmosphere right
        now (periapsis AND current altitude both inside it) -- checked in
        every wait loop below so a decay spiral fails loud instead of
        silently grinding the orbit away. Confirmed live, twice: a node
        timed for a future apoapsis, created while periapsis was already
        inside the atmosphere, just sits there while the craft loses more
        periapsis/apoapsis on every pass -- and sc.warp_to() makes it worse
        by silently doing nothing at this altitude/speed instead of erroring,
        so nothing was ever surfacing that the wait itself was the danger."""
        atm = body.atmosphere_depth
        if not atm:
            return False
        return (vessel.orbit.periapsis_altitude < atm
                and vessel.flight(body.reference_frame).mean_altitude < atm)

    burn_time = estimate_burn_time(vessel, node.delta_v)

    # RCS on for the whole burn: reaction wheels alone can lose attitude
    # lock while a powerful engine is firing (any thrust/COM offset
    # produces a disturbance torque with nothing to counter it), which
    # silently wastes the burn -- full throttle, but pointed increasingly
    # off the node's actual direction, so remaining_delta_v barely drops.
    # Observed for real on a satellite's phasing burn: throttle stuck at
    # 1.0 for 150+ seconds with almost no progress on a burn that should
    # have taken ~15s at that thrust-to-mass ratio.
    was_rcs = control.rcs
    control.rcs = True

    ap.reference_frame = node.reference_frame
    ap.target_direction = (0, 1, 0)
    ap.engaged = True
    job.message = "orienting for burn"
    # Wall-clock deadline, not a count of job.sleep(0.2) calls -- confirmed
    # live: when kRPC/the game is slow (heavy load, or physics bogged down
    # low in the atmosphere), a single "0.2s" sleep can take several times
    # that in real time, so a loop-count timeout silently stretches out far
    # past its intended 60s while the orbit keeps decaying underneath it.
    orient_deadline = time.time() + 60
    while ap.error > 2 and time.time() < orient_deadline:
        job.check_abort()
        if _decaying_now():
            raise RuntimeError(
                "orbit is decaying through the atmosphere while still orienting for the burn -- "
                "aborting instead of continuing to wait"
            )
        job.sleep(0.2)

    burn_ut = sc.ut + node.time_to - (burn_time / 2.0)
    if burn_ut - sc.ut > lead_time:
        if _decaying_now():
            raise RuntimeError(
                f"burn is {burn_ut - sc.ut:.0f}s away but the orbit is decaying through the "
                "atmosphere right now -- refusing to wait/warp for it"
            )
        job.message = "warping to burn"
        sc.warp_to(burn_ut - lead_time)

    while node.time_to - (burn_time / 2.0) > 0:
        job.check_abort()
        if _decaying_now():
            raise RuntimeError(
                "orbit is decaying through the atmosphere while waiting for the burn window -- "
                "aborting instead of continuing to wait"
            )
        job.sleep(0.1)

    job.message = "executing burn"
    # Proportional throttle, not a crude full-throttle-then-fixed-trickle
    # step: taper continuously as remaining delta-v approaches zero so the
    # final burn stops as close to exactly on target as the tick rate
    # allows, instead of overshooting past a coarse threshold.
    taper_window = 5.0  # start easing off within this many m/s of done
    min_throttle = 0.02
    last_dv = node.remaining_delta_v
    stager = staging.Stager(vessel)
    try:
        while node.remaining_delta_v > 0.02 and node.remaining_delta_v <= last_dv + 0.1:
            job.check_abort()
            # Confirmed live: a burn needing more delta-v than the current
            # stage can supply just runs it dry -- available_thrust hits 0
            # and remaining_delta_v gets stuck at a fixed value forever
            # (which the while-condition above can't tell apart from a
            # completed burn). Stage and keep burning instead of stalling.
            # verify_empty=False: mid-burn, the engine was already firing,
            # so zero thrust can only mean genuinely dry.
            if stager.stage_if_dry(
                job, verify_empty=False,
                settle=staging.settle_briefly(),  # let the new engine spool up
                label="staged mid-burn",
            ):
                continue
            last_dv = node.remaining_delta_v
            control.throttle = min(1.0, max(min_throttle, last_dv / taper_window))
            job.sleep(0.02)
    finally:
        # Guaranteed even on abort/error -- a burn loop that only cuts
        # throttle after the while-loop exits normally leaves the engine
        # running at full throttle if check_abort() raises mid-burn (this
        # happened for real: aborting a job mid-burn left the vessel
        # accelerating uncontrolled since the cleanup line was never
        # reached).
        control.throttle = 0.0
        ap.engaged = False
        control.rcs = was_rcs
        try:
            node.remove()
        except Exception:
            pass


def execute_node_retrying(client, vessel, job, node_factory, max_attempts=3):
    """Like execute_node, but takes a node FACTORY (a zero-argument callable
    that creates and returns a fresh node) instead of an already-created
    node, and retries with a newly recomputed node if the node disappears
    out from under the burn.

    Confirmed live, twice in one session, on two different jobs: a maneuver
    node vanished mid-execution -- kRPC raised "Maneuver node has been
    removed" from inside execute_node's own wait loop -- aborting the whole
    plane change with the orbit left exactly where it started. The cause
    wasn't pinned down (KSP's own Backspace hotkey deletes the active node,
    which is easy to hit by accident while watching a burn approach in map
    view; it could also be something else), but regardless of cause, giving
    up on the entire maneuver over a node that can simply be recreated is
    the wrong failure mode. This retries with a fresh node a bounded number
    of times before actually giving up.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        node = node_factory()
        try:
            execute_node(client, vessel, job, node)
            return
        except Exception as exc:
            if "removed" not in str(exc).lower():
                raise
            last_error = exc
            if attempt == max_attempts:
                break
            job.message = (
                f"maneuver node vanished mid-burn -- recreating and retrying "
                f"(attempt {attempt + 1}/{max_attempts})"
            )
            job.sleep(1.0)

    raise RuntimeError(
        f"the maneuver node kept disappearing mid-burn after {max_attempts} attempts, so this maneuver "
        "couldn't complete. If you're watching in map view, avoid pressing Backspace or deleting the node "
        "while a burn is in progress -- that's KSP's own hotkey for removing the active node."
    ) from last_error
