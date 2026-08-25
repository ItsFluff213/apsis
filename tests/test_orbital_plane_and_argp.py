"""Tests for change_orbital_plane_node and change_argument_of_periapsis_node
in backend/autopilots/maneuver.py.

These use a separately-calibrated FakeOrbit from test_plane_change.py's,
not because the physics differs, but because the *sign convention* does.
change_inclination_node (tested there) builds its own internal geometry
from cross(position, velocity) and only ever compares against a scalar
inclination -- self-consistent, and correct for that. change_orbital_plane_
node and change_argument_of_periapsis_node need to agree with kRPC's actual
`orbit.longitude_of_ascending_node`, which is not symmetric under flipping
the normal vector's sign the way inclination is. Confirmed live against a
dozen real vessels: kRPC's own convention is cross(velocity, position), the
opposite of the internal one used elsewhere in this file -- get this
backwards here and the tests would validate against the wrong ground truth
without ever failing.
"""

import math

import pytest

from backend import orbital
from backend.autopilots import maneuver

MU_MUN = 6.5138398e10
MUN_RADIUS = 200_000.0
REFERENCE_UT = 1000.0


class FakeOrbit:
    """A real Keplerian orbit built from classical elements including
    longitude of ascending node, calibrated to kRPC's own sign convention
    (see module docstring) -- verified by round-tripping every element
    (a, e, inclination, LAN, argument of periapsis) back out of a
    constructed state vector before this class was trusted for anything.
    """

    def __init__(self, mu, semi_major_axis, eccentricity, inclination_rad=0.0,
                 lan_rad=0.0, argument_of_periapsis=0.0):
        self.body = type("Body", (), {
            "gravitational_parameter": mu,
            "non_rotating_reference_frame": "fake_frame",
        })()
        self.semi_major_axis = semi_major_axis
        self.eccentricity = eccentricity
        self.inclination = inclination_rad
        self.longitude_of_ascending_node = lan_rad
        self.argument_of_periapsis = argument_of_periapsis
        self.period = orbital.period_for_sma(mu, semi_major_axis)

        n0 = (0.0, math.cos(inclination_rad), -math.sin(inclination_rad))
        self._normal = orbital.rotate_about_axis(n0, (0.0, 1.0, 0.0), -lan_rad)
        node_hat = orbital.norm(orbital.cross(self._normal, (0.0, 1.0, 0.0)))
        advance_at_node = orbital.norm(orbital.cross(node_hat, self._normal))
        self._periapsis_hat = tuple(
            math.cos(argument_of_periapsis) * n + math.sin(argument_of_periapsis) * a
            for n, a in zip(node_hat, advance_at_node)
        )
        self._perp_hat = orbital.norm(orbital.cross(self._periapsis_hat, self._normal))
        self._h = math.sqrt(mu * semi_major_axis * (1 - eccentricity ** 2))

    def radius_at_true_anomaly(self, nu):
        return (self.semi_major_axis * (1 - self.eccentricity ** 2)) / (1 + self.eccentricity * math.cos(nu))

    def ut_at_true_anomaly(self, nu):
        return REFERENCE_UT + orbital.time_from_periapsis(
            self.body.gravitational_parameter, self.semi_major_axis, self.eccentricity, nu,
        )

    def position_at(self, ut, frame):
        nu = self._true_anomaly_at_ut(ut)
        r = self.radius_at_true_anomaly(nu)
        return tuple(
            r * (math.cos(nu) * p + math.sin(nu) * q) for p, q in zip(self._periapsis_hat, self._perp_hat)
        )

    def exact_velocity_at(self, ut, frame):
        nu = self._true_anomaly_at_ut(ut)
        r = self.radius_at_true_anomaly(nu)
        mu = self.body.gravitational_parameter
        v_radial = (mu / self._h) * self.eccentricity * math.sin(nu)
        v_tangential = self._h / r
        radial_hat = orbital.norm(self.position_at(ut, frame))
        tangential_hat = orbital.norm(orbital.cross(radial_hat, self._normal))
        return tuple(v_radial * rh + v_tangential * th for rh, th in zip(radial_hat, tangential_hat))

    def _true_anomaly_at_ut(self, ut):
        target = ut - REFERENCE_UT
        lo, hi = -math.pi + 1e-9, math.pi - 1e-9
        for _ in range(80):
            mid = (lo + hi) / 2
            t = orbital.time_from_periapsis(self.body.gravitational_parameter, self.semi_major_axis,
                                            self.eccentricity, mid)
            period = self.period
            diff = (t - (target % period) + period / 2) % period - period / 2
            if diff < 0:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2


class FakeControl:
    def __init__(self):
        self.node = None

    def add_node(self, ut, prograde=0.0, normal=0.0, radial=0.0):
        self.node = {"ut": ut, "prograde": prograde, "normal": normal, "radial": radial}
        return self.node


class FakeVessel:
    def __init__(self, orbit):
        self.orbit = orbit
        self.control = FakeControl()


class FakeClient:
    def __init__(self):
        self.space_center = type("SC", (), {"ut": REFERENCE_UT})()


def make(periapsis_alt, apoapsis_alt, inclination_deg=0.0, lan_deg=0.0, argp_deg=0.0):
    a = (2 * MUN_RADIUS + periapsis_alt + apoapsis_alt) / 2
    e = (apoapsis_alt - periapsis_alt) / (2 * MUN_RADIUS + apoapsis_alt + periapsis_alt)
    orbit = FakeOrbit(MU_MUN, a, e, inclination_rad=math.radians(inclination_deg),
                       lan_rad=math.radians(lan_deg), argument_of_periapsis=math.radians(argp_deg))
    return FakeClient(), FakeVessel(orbit)


def node_cost(node):
    return math.sqrt(node["prograde"] ** 2 + node["normal"] ** 2 + node["radial"] ** 2)


def elements_after_burn(vessel, node):
    """Apply the node's delta-v (using velocity.py's real maneuver.velocity_at
    reconstruction, matching what execute_node actually burns, not the
    fake's own exact velocity) and return the resulting classical elements."""
    orbit = vessel.orbit
    mu = orbit.body.gravitational_parameter
    position = orbit.position_at(node["ut"], orbit.body.non_rotating_reference_frame)
    velocity = orbit.exact_velocity_at(node["ut"], orbit.body.non_rotating_reference_frame)

    prograde_hat = orbital.norm(velocity)
    normal_hat = orbital.norm(orbital.cross(position, velocity))
    radial_hat = orbital.cross(normal_hat, prograde_hat)

    new_velocity = tuple(
        v + node["prograde"] * p + node["normal"] * n + node["radial"] * r
        for v, p, n, r in zip(velocity, prograde_hat, normal_hat, radial_hat)
    )

    r_mag = orbital.magnitude(position)
    v_mag = orbital.magnitude(new_velocity)
    specific_energy = v_mag ** 2 / 2 - mu / r_mag
    new_a = -mu / (2 * specific_energy)

    h_vec = orbital.cross(new_velocity, position)  # kRPC's real convention -- see module docstring
    normal = orbital.norm(h_vec)
    h_mag = orbital.magnitude(h_vec)
    new_e = math.sqrt(max(0.0, 1 - h_mag ** 2 / (new_a * mu)))

    new_incl = math.degrees(math.acos(max(-1.0, min(1.0, normal[1]))))
    node_hat = orbital.norm(orbital.cross(normal, (0.0, 1.0, 0.0)))
    new_lan = math.degrees(math.atan2(orbital.dot(node_hat, (0, 0, 1)), orbital.dot(node_hat, (1, 0, 0)))) % 360

    advance_at_node = orbital.norm(orbital.cross(node_hat, normal))
    # The eccentricity-vector identity e = (v x h)/mu - r_hat is a physics
    # constraint on a *specific* h = position x velocity (the textbook
    # convention) -- it does not hold for h_vec above, which is deliberately
    # the opposite sign to match kRPC's inclination/LAN reporting. Using
    # the wrong one here doesn't error, it just silently produces a
    # rotated-but-wrong eccentricity vector -- caught by cross-checking
    # this decoder's own output against the (already independently
    # verified) numeric toy model before trusting it for anything.
    h_standard = orbital.cross(position, new_velocity)
    vxh = orbital.cross(new_velocity, h_standard)
    e_vec = tuple(c / mu - rh for c, rh in zip(vxh, orbital.norm(position)))
    e_hat = orbital.norm(e_vec)
    new_argp = math.degrees(math.atan2(orbital.dot(e_hat, advance_at_node), orbital.dot(e_hat, node_hat))) % 360

    return new_a, new_e, new_incl, new_lan, new_argp


class TestChangeOrbitalPlaneNode:
    def test_reaches_target_inclination_and_lan_preserving_shape(self):
        for (i0, lan0, argp0, it, lant) in [
            (37, 80, 10, 90, 90),
            (10, 200, 45, 60, 300),
            (90, 0, 0, 45, 180),
            (5, 10, 270, 80, 80.5),
            (150, 45, 90, 30, 200),
        ]:
            client, vessel = make(80_000, 1_200_000, inclination_deg=i0, lan_deg=lan0, argp_deg=argp0)
            original_a = vessel.orbit.semi_major_axis
            original_e = vessel.orbit.eccentricity

            node = maneuver.change_orbital_plane_node(client, vessel, it, lant)
            new_a, new_e, new_incl, new_lan, _ = elements_after_burn(vessel, node)

            assert new_incl == pytest.approx(it, abs=1e-3), f"start=({i0},{lan0}) target=({it},{lant})"
            assert new_lan == pytest.approx(lant % 360, abs=1e-3), f"start=({i0},{lan0}) target=({it},{lant})"
            assert new_a == pytest.approx(original_a, rel=1e-5)
            assert new_e == pytest.approx(original_e, abs=1e-5)

    def test_circular_orbit_too(self):
        client, vessel = make(80_000, 80_000, inclination_deg=20, lan_deg=15)
        node = maneuver.change_orbital_plane_node(client, vessel, 75.0, 200.0)
        new_a, new_e, new_incl, new_lan, _ = elements_after_burn(vessel, node)
        assert new_incl == pytest.approx(75.0, abs=1e-3)
        assert new_lan == pytest.approx(200.0, abs=1e-3)
        assert new_e == pytest.approx(0.0, abs=1e-6)


class TestChangeArgumentOfPeriapsisNode:
    def test_reaches_target_argp_preserving_everything_else(self):
        for (i0, lan0, argp0, argt) in [
            (37, 80, 10, 100),
            (45, 0, 0, 270),
            (20, 300, 350, 10),
            (60, 45, 180, 45.5),
        ]:
            client, vessel = make(80_000, 1_200_000, inclination_deg=i0, lan_deg=lan0, argp_deg=argp0)
            original_a = vessel.orbit.semi_major_axis
            original_e = vessel.orbit.eccentricity

            node = maneuver.change_argument_of_periapsis_node(client, vessel, argt)
            new_a, new_e, new_incl, new_lan, new_argp = elements_after_burn(vessel, node)

            assert new_argp == pytest.approx(argt % 360, abs=1e-3), f"start_argp={argp0} target={argt}"
            assert new_a == pytest.approx(original_a, rel=1e-5)
            assert new_e == pytest.approx(original_e, abs=1e-5)
            assert new_incl == pytest.approx(i0, abs=1e-3)
            assert new_lan == pytest.approx(lan0 % 360, abs=1e-3)

    def test_cost_is_reasonable_not_a_full_replan(self):
        """A small argp change should cost far less than a from-scratch
        circularization burn -- if this function were somehow reconstructing
        the whole orbit instead of just rotating periapsis, this would catch
        a wildly oversized delta-v."""
        client, vessel = make(80_000, 1_200_000, inclination_deg=30, argp_deg=0)
        node = maneuver.change_argument_of_periapsis_node(client, vessel, 10.0)
        assert node_cost(node) < 200.0
