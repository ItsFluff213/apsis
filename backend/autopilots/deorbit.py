"""Targeted deorbit: pick *where* in the orbit to fire the retrograde burn
so the craft falls near a chosen surface target.

The previous behaviour was to burn at apoapsis, full stop -- whatever point
in the orbit that happened to be. The descent guidance then only steered
toward the target during the final low-speed hover, from about 15 m up, by
which time essentially nothing about the landing site can still be changed.
So the craft reliably landed *somewhere*, and only accidentally near the
waypoint.

The leverage is all in the burn timing. A deorbit burn fixes the impact
point hundreds of kilometres in advance, at a cost of zero extra delta-v --
it is the same burn either way, just fired a few minutes earlier or later.
This module works out which few minutes.

How the prediction works, for a burn at radius r_burn that drops periapsis
to r_peri (deliberately below the surface):

  * That burn point becomes the new orbit's apoapsis, so the fall is the
    arc from apoapsis down to wherever the ellipse crosses the surface.
  * Kepler gives both how far around that arc goes and how long it takes
    (see backend/orbital.py, unit-tested in tests/test_orbital.py).
  * The body spins underneath during the fall, so the landing site drifts
    west of where the inertial geometry alone would put it. On Kerbin a
    typical descent takes long enough for this to be worth hundreds of
    kilometres -- ignoring it is the single biggest error in a naive
    prediction.

Deliberately still an approximation: it is a vacuum two-body fall, so
atmospheric drag (which shortens the trajectory) and terrain height are
not modelled. Expect it to place a landing in the right region rather than
on a dime -- which is a large improvement on not aiming at all, and short
of what a real precision-landing solver does.
"""

import math

from backend import geo, orbital

# How finely to search one orbit for the best burn point. 240 samples over
# a ~30 minute low orbit is roughly one every 7 seconds of orbit time --
# well inside the accuracy the rest of the model can justify.
SEARCH_SAMPLES = 240


def _predict_impact(client, vessel, body, burn_ut, r_peri_target):
    """Latitude/longitude where the craft would come down if the deorbit
    burn happened at `burn_ut`. Returns (lat, lon, fall_time_s) or None if
    the geometry doesn't produce an impact."""
    sc = client.space_center
    frame = body.non_rotating_reference_frame
    mu = body.gravitational_parameter

    position = vessel.orbit.position_at(burn_ut, frame)
    r_burn = orbital.magnitude(position)
    if r_burn <= r_peri_target:
        return None  # already lower than the periapsis we'd be aiming for

    # The post-burn ellipse: burn point is its apoapsis, target periapsis
    # is underground.
    a2 = (r_burn + r_peri_target) / 2.0
    e2 = (r_burn - r_peri_target) / (r_burn + r_peri_target)

    try:
        nu_impact = orbital.true_anomaly_at_radius(a2, e2, body.equatorial_radius)
    except ValueError:
        return None

    # Falling from apoapsis (nu = pi) toward periapsis, the surface is met
    # on the inbound side, at -nu_impact.
    nu_arrive = 2 * math.pi - nu_impact
    fall_time = orbital.time_between_true_anomalies(mu, a2, e2, math.pi, nu_arrive)
    sweep = nu_arrive - math.pi  # angle travelled around the body during the fall

    # Rotate the burn position forward along the direction of motion by
    # that sweep to get the inertial impact direction.
    velocity = vessel.orbit.velocity_at(burn_ut, frame)
    normal = orbital.cross(position, velocity)
    impact_direction = orbital.rotate_about_axis(orbital.norm(position), normal, sweep)
    impact_position = tuple(c * body.equatorial_radius for c in impact_direction)

    latitude = body.latitude_at_position(impact_position, frame)
    # longitude_at_position against the non-rotating frame gives the
    # longitude as if the body were frozen at its current orientation, so
    # the whole rotation from now until impact still has to come off.
    longitude_frozen = body.longitude_at_position(impact_position, frame)
    if body.rotational_period:
        elapsed = (burn_ut - sc.ut) + fall_time
        rotated_deg = 360.0 * elapsed / body.rotational_period
        longitude = (longitude_frozen - rotated_deg + 180) % 360 - 180
    else:
        longitude = longitude_frozen

    return latitude, longitude, fall_time


def find_deorbit_burn_ut(client, vessel, job, target_lat, target_lon, r_peri_target):
    """Search one full orbit for the burn time whose predicted impact lands
    closest to the target. Returns (burn_ut, predicted_miss_m) or
    (None, None) if nothing could be predicted."""
    sc = client.space_center
    body = vessel.orbit.body
    period = vessel.orbit.period

    best_ut = None
    best_miss = None
    # Start slightly ahead of now: the burn needs time to be set up and
    # oriented for, so a "best" answer four seconds from now is useless.
    lead = max(60.0, period * 0.02)

    for i in range(SEARCH_SAMPLES):
        job.check_abort()
        burn_ut = sc.ut + lead + (period * i / SEARCH_SAMPLES)
        prediction = _predict_impact(client, vessel, body, burn_ut, r_peri_target)
        if prediction is None:
            continue
        latitude, longitude, _ = prediction
        miss = geo.surface_distance(body, latitude, longitude, target_lat, target_lon)
        if best_miss is None or miss < best_miss:
            best_miss = miss
            best_ut = burn_ut

    return best_ut, best_miss
