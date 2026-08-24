"""Vector and two-body orbital math shared by the transfer autopilots.

Pure functions only -- nothing here touches kRPC or game state, so it can
be unit-tested directly (see tests/test_orbital.py). The transfer planners
in backend/autopilots/ read live positions out of the game and then do all
their actual geometry through this module.

Convention note: kRPC's non-rotating reference frames put the orbital plane
roughly in x/z, with y as the out-of-plane axis. `angle_of` follows that,
projecting onto x/z. Every planar simplification in this project's transfer
math uses the same projection, so they compose consistently.
"""

import math


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def magnitude(a):
    return math.sqrt(sum(c * c for c in a))


def norm(a):
    m = magnitude(a) or 1.0
    return tuple(c / m for c in a)


def rotate_about_axis(vec, axis, angle):
    """Rodrigues' rotation formula: rotate `vec` about `axis` by `angle`
    radians (right-handed)."""
    axis = norm(axis)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    cross_term = cross(axis, vec)
    dot_term = dot(axis, vec) * (1 - cos_a)
    return tuple(vec[i] * cos_a + cross_term[i] * sin_a + axis[i] * dot_term for i in range(3))


def angle_of(position):
    """Angle (rad) of a position vector projected onto the x/z plane."""
    x, _, z = position
    return math.atan2(z, x)


def wrap_angle(angle):
    """Normalize to [0, 2*pi)."""
    return angle % (2 * math.pi)


def signed_angle_difference(target, current):
    """Shortest signed angle (rad) to get from `current` to `target`, in
    (-pi, pi]."""
    return (target - current + math.pi) % (2 * math.pi) - math.pi


# --- Two-body relations -------------------------------------------------

def vis_viva_speed(mu, r, a):
    """Orbital speed at radius r on an orbit of semi-major axis a."""
    return math.sqrt(mu * (2.0 / r - 1.0 / a))


def circular_speed(mu, r):
    return math.sqrt(mu / r)


def escape_speed(mu, r):
    return math.sqrt(2.0 * mu / r)


def period_for_sma(mu, a):
    """Kepler's third law."""
    return 2 * math.pi * math.sqrt(a ** 3 / mu)


def sma_for_period(mu, period):
    """Inverse of period_for_sma."""
    return (mu * period ** 2 / (4 * math.pi ** 2)) ** (1.0 / 3.0)


def hohmann_transfer(mu, r1, r2):
    """Classic two-impulse Hohmann transfer between coplanar circular
    orbits of radii r1 and r2 about a body of gravitational parameter mu.

    Returns (transfer_sma, time_of_flight_s, dv_depart, dv_arrive), where
    the two delta-v figures are the speed changes needed *in the
    heliocentric (or parent-centric) frame* -- for a planet-to-planet
    transfer these are not the burns the vessel actually performs, since
    the vessel departs from inside a planet's gravity well. See
    `ejection_speed` for turning dv_depart into a real burn.
    """
    a_transfer = (r1 + r2) / 2.0
    tof = period_for_sma(mu, a_transfer) / 2.0
    v1_circ = circular_speed(mu, r1)
    v2_circ = circular_speed(mu, r2)
    v_peri = vis_viva_speed(mu, r1, a_transfer)
    v_apo = vis_viva_speed(mu, r2, a_transfer)
    return a_transfer, tof, v_peri - v1_circ, v2_circ - v_apo


def hohmann_phase_angle(mu, r1, r2):
    """The angle (rad) the destination must lead the origin by at the
    moment of departure, so that both arrive at the transfer orbit's far
    apsis together.

    Positive means the target should be ahead of the origin (the normal
    case going outward); negative means behind (going inward, where the
    faster inner target laps around to meet the vessel).
    """
    _, tof, _, _ = hohmann_transfer(mu, r1, r2)
    # How far the destination travels during the flight...
    target_sweep = tof * (2 * math.pi / period_for_sma(mu, r2))
    # ...and the vessel arrives exactly half a transfer orbit (pi) around.
    return math.pi - target_sweep


def ejection_speed(mu_body, r_park, v_infinity):
    """Speed needed at radius `r_park` inside a body's gravity well to
    leave it with hyperbolic excess speed `v_infinity`.

    Energy bookkeeping: the orbit's specific energy is v_inf^2/2 once free
    of the body, so at radius r it must be v^2/2 - mu/r = v_inf^2/2.
    This is why an interplanetary departure is so much cheaper from low
    orbit than high -- the Oberth effect falls straight out of the 2*mu/r
    term.
    """
    return math.sqrt(v_infinity ** 2 + 2.0 * mu_body / r_park)


def ejection_angle(mu_body, r_park, v_infinity):
    """Angle (rad) between the departure burn point and the direction the
    vessel finally escapes along, measured around the body.

    On the escape hyperbola the asymptote sits at true anomaly
    acos(-1/e) from periapsis, so the burn must happen that far *before*
    the outgoing direction. Getting this wrong is the classic way to
    depart with a perfect heliocentric delta-v and still miss the target
    planet entirely -- the speed is right but it points the wrong way.
    """
    v_eject = ejection_speed(mu_body, r_park, v_infinity)
    # Eccentricity of the escape hyperbola from its periapsis state.
    eccentricity = (r_park * v_eject ** 2 / mu_body) - 1.0
    if eccentricity <= 1.0:
        # Not actually an escape trajectory -- caller asked for a v_infinity
        # of ~0. Degenerate, but pi/2 is the sensible limit (a parabolic
        # escape leaves perpendicular to the periapsis radius).
        return math.pi / 2.0
    return math.acos(-1.0 / eccentricity)


# --- Kepler time-of-flight ----------------------------------------------
# Needed to answer "how long until the vessel gets from here to there on
# this orbit", which is what targeted deorbiting depends on: a deorbit burn
# is aimed by choosing *where* in the orbit to fire it, and that requires
# knowing how long the fall to the ground will take so the body's rotation
# underneath can be accounted for.

def true_anomaly_at_radius(a, e, r):
    """True anomaly (rad, in [0, pi]) at which an orbit of semi-major axis
    `a` and eccentricity `e` passes through radius `r`.

    Returns the outbound (post-periapsis) solution; the inbound one is its
    negative, since the conic is symmetric about the apsis line. Raises
    ValueError if the orbit never reaches that radius at all.
    """
    if e < 0:
        raise ValueError("eccentricity cannot be negative")
    p = a * (1 - e * e)  # semi-latus rectum
    if abs(e) < 1e-12:
        if abs(r - a) > 1e-6 * max(a, 1.0):
            raise ValueError("circular orbit never reaches that radius")
        return 0.0
    cos_nu = (p / r - 1.0) / e
    if cos_nu > 1.0 or cos_nu < -1.0:
        raise ValueError(f"orbit does not reach radius {r}")
    return math.acos(cos_nu)


def eccentric_from_true_anomaly(e, nu):
    """Eccentric anomaly from true anomaly (elliptical orbits only)."""
    return math.atan2(math.sqrt(max(1 - e * e, 0.0)) * math.sin(nu), e + math.cos(nu))


def mean_from_eccentric_anomaly(e, eccentric):
    """Kepler's equation, in the easy direction."""
    return eccentric - e * math.sin(eccentric)


def time_from_periapsis(mu, a, e, nu):
    """Time (s) since periapsis passage at true anomaly `nu`, for an
    elliptical orbit. Always in [0, period)."""
    eccentric = eccentric_from_true_anomaly(e, nu)
    mean = mean_from_eccentric_anomaly(e, eccentric)
    n = math.sqrt(mu / a ** 3)  # mean motion
    return (mean / n) % period_for_sma(mu, a)


def time_between_true_anomalies(mu, a, e, nu_from, nu_to):
    """Flight time (s) going forward along the orbit from `nu_from` to
    `nu_to`. Wraps through periapsis if needed, so the result is always
    non-negative."""
    t_from = time_from_periapsis(mu, a, e, nu_from)
    t_to = time_from_periapsis(mu, a, e, nu_to)
    return (t_to - t_from) % period_for_sma(mu, a)


def sphere_of_influence(a, mass_body, mass_parent):
    """Radius of a body's sphere of influence. kRPC exposes this directly
    for real bodies; this exists for tests and sanity checks."""
    return a * (mass_body / mass_parent) ** 0.4
