"""Tests for plane-change node placement in backend/autopilots/maneuver.py.

Worth testing precisely because getting it wrong is not a crash -- it is a
burn that costs several times what it should, or silently reshapes the
orbit while still rotating the plane correctly. Both have happened for
real: a 90 degree change around Mun done on a circular orbit cost 4x what
the same change costs done at apoapsis on the elliptical capture orbit
(cost, fixed first); and computing the burn from only the scalar vis-viva
speed -- which implicitly assumes zero radial velocity -- silently
corrupted the orbit's shape whenever the burn happened away from an apsis,
which is exactly what a cost-optimized node choice sometimes requires
(shape, fixed second, confirmed live on a real Mun arrival: right
inclination, periapsis and apoapsis both wrong).

FakeOrbit below is a real Keplerian orbit -- not a flat stub -- specifically
so these tests can place the burn away from periapsis/apoapsis and check
that shape is still preserved there. A fake that only ever burns at an
apsis cannot catch this class of bug; it's exactly what let the original
formula ship.
"""

import math

import pytest

from backend import orbital
from backend.autopilots import maneuver

MU_MUN = 6.5138398e10
MUN_RADIUS = 200_000.0
REFERENCE_UT = 1000.0


class FakeOrbit:
    """A real 3D Keplerian orbit, built from classical elements with the
    ascending node fixed along +X (i.e. longitude of ascending node = 0) --
    a real special case, not a simplification of the physics. Everything
    below is standard orbital mechanics (perifocal decomposition, the
    r(nu)/v_r(nu)/v_t(nu) relations), not anything specific to this
    project, so an independent check of the numbers is straightforward.
    """

    def __init__(self, mu, semi_major_axis, eccentricity, inclination_rad=0.0, argument_of_periapsis=0.0):
        self.body = type("Body", (), {
            "gravitational_parameter": mu,
            "non_rotating_reference_frame": "fake_frame",
        })()
        self.semi_major_axis = semi_major_axis
        self.eccentricity = eccentricity
        self.inclination = inclination_rad
        self.argument_of_periapsis = argument_of_periapsis
        self.period = orbital.period_for_sma(mu, semi_major_axis)

        # Ascending node along +X: a zero-inclination orbit's normal is +Y,
        # and tilting the plane by `inclination` about the line of nodes
        # (+X, fixed here) tips that normal toward +Z.
        self._normal = orbital.rotate_about_axis((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), inclination_rad)
        self._periapsis_hat = orbital.rotate_about_axis((1.0, 0.0, 0.0), self._normal, argument_of_periapsis)
        self._perp_hat = orbital.cross(self._normal, self._periapsis_hat)
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
        """Analytic velocity, for the test's own verification math only.

        Real kRPC has no vector velocity_at at all -- confirmed live, see
        maneuver.velocity_at's docstring -- so this is deliberately NOT
        named to match anything production calls. Production goes through
        maneuver.velocity_at (a position_at finite difference) like it will
        against the real game; this exact version exists so the
        verification below can check the result against ground truth
        rather than checking the approximation against itself.
        """
        nu = self._true_anomaly_at_ut(ut)
        r = self.radius_at_true_anomaly(nu)
        mu = self.body.gravitational_parameter
        v_radial = (mu / self._h) * self.eccentricity * math.sin(nu)
        v_tangential = self._h / r
        radial_hat = orbital.norm(self.position_at(ut, frame))
        tangential_hat = orbital.cross(self._normal, radial_hat)
        return tuple(
            v_radial * rh + v_tangential * th for rh, th in zip(radial_hat, tangential_hat)
        )

    def _true_anomaly_at_ut(self, ut):
        # Inverse of ut_at_true_anomaly by search -- fine for test-sized
        # tolerances, and keeps this fake from needing its own Kepler
        # solver on top of the one already tested in backend/orbital.py.
        target = ut - REFERENCE_UT
        lo, hi = -math.pi + 1e-9, math.pi - 1e-9
        for _ in range(80):
            mid = (lo + hi) / 2
            t = orbital.time_from_periapsis(self.body.gravitational_parameter, self.semi_major_axis,
                                            self.eccentricity, mid)
            # time_from_periapsis wraps to [0, period); unwrap near 0/period
            # by comparing against target modulo period for a stable bisection.
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


def node_cost(node):
    return math.sqrt(node["prograde"] ** 2 + node["normal"] ** 2 + node["radial"] ** 2)


def make(periapsis_alt, apoapsis_alt, argp=0.0, inclination_deg=0.0):
    a = (2 * MUN_RADIUS + periapsis_alt + apoapsis_alt) / 2
    e = (apoapsis_alt - periapsis_alt) / (2 * MUN_RADIUS + apoapsis_alt + periapsis_alt)
    orbit = FakeOrbit(MU_MUN, a, e, inclination_rad=math.radians(inclination_deg), argument_of_periapsis=argp)
    return FakeClient(), FakeVessel(orbit)


def orbit_after_burn(vessel, node):
    """Apply the node's delta-v at its burn time and return the resulting
    (semi_major_axis, eccentricity, inclination_deg) -- the three things a
    plane-change-only maneuver must leave alone apart from inclination."""
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

    r = orbital.magnitude(position)
    v = orbital.magnitude(new_velocity)
    specific_energy = v ** 2 / 2 - mu / r
    new_a = -mu / (2 * specific_energy)

    h_vec = orbital.cross(position, new_velocity)
    h = orbital.magnitude(h_vec)
    new_e = math.sqrt(max(0.0, 1 - h ** 2 / (new_a * mu)))

    new_normal = orbital.norm(h_vec)
    new_inclination_deg = math.degrees(math.acos(max(-1.0, min(1.0, new_normal[1]))))

    return new_a, new_e, new_inclination_deg


class TestPlaneChangePreservesOrbitShape:
    """The actual regression: a plane change must leave semi-major axis and
    eccentricity alone, at ANY burn point -- not just at an apsis, which is
    the one case the old (wrong) formula happened to get right."""

    def test_shape_preserved_when_burning_off_apsis(self):
        """The specific failure mode: an eccentric orbit whose node does not
        fall at periapsis or apoapsis. This is the case that shipped wrong."""
        client, vessel = make(periapsis_alt=100_000, apoapsis_alt=1_500_000, argp=math.radians(50))
        original_a = vessel.orbit.semi_major_axis
        original_e = vessel.orbit.eccentricity

        node = maneuver.change_inclination_node(client, vessel, 90.0)
        new_a, new_e, new_incl = orbit_after_burn(vessel, node)

        assert new_a == pytest.approx(original_a, rel=1e-6)
        assert new_e == pytest.approx(original_e, abs=1e-6)
        assert new_incl == pytest.approx(90.0, abs=1e-6)

    def test_shape_preserved_across_several_eccentric_configurations(self):
        for argp_deg in (0, 33, 90, 145, 200, 300):
            client, vessel = make(periapsis_alt=80_000, apoapsis_alt=2_000_000, argp=math.radians(argp_deg))
            original_a = vessel.orbit.semi_major_axis
            original_e = vessel.orbit.eccentricity

            node = maneuver.change_inclination_node(client, vessel, 45.0)
            new_a, new_e, new_incl = orbit_after_burn(vessel, node)

            assert new_a == pytest.approx(original_a, rel=1e-5), f"argp={argp_deg}"
            assert new_e == pytest.approx(original_e, abs=1e-5), f"argp={argp_deg}"
            assert new_incl == pytest.approx(45.0, abs=1e-4), f"argp={argp_deg}"

    def test_shape_preserved_on_near_circular_orbit_too(self):
        """The case that always worked -- must keep working."""
        client, vessel = make(periapsis_alt=50_000, apoapsis_alt=50_000)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        new_a, new_e, new_incl = orbit_after_burn(vessel, node)
        assert new_a == pytest.approx(vessel.orbit.semi_major_axis, rel=1e-6)
        assert new_e == pytest.approx(0.0, abs=1e-6)
        assert new_incl == pytest.approx(90.0, abs=1e-6)


class TestPlaneChangeCost:
    def test_polar_change_on_circular_50km_orbit_is_expensive(self):
        client, vessel = make(50_000, 50_000)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        assert node_cost(node) == pytest.approx(722, abs=15)

    def test_polar_change_on_capture_orbit_is_far_cheaper(self):
        # argp = pi puts periapsis opposite the reference direction, so the
        # ascending node (true anomaly -argp) lands at apoapsis.
        client, vessel = make(50_000, 1_215_000, argp=math.pi)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        assert node_cost(node) == pytest.approx(166, abs=15)

    def test_cheaper_ordering_saves_hundreds_of_m_s(self):
        client_c, vessel_c = make(50_000, 50_000)
        circular = node_cost(maneuver.change_inclination_node(client_c, vessel_c, 90.0))
        client_e, vessel_e = make(50_000, 1_215_000, argp=math.pi)
        elliptical = node_cost(maneuver.change_inclination_node(client_e, vessel_e, 90.0))
        assert circular - elliptical > 500


class TestNodeSelection:
    def test_picks_the_higher_of_the_two_nodes(self):
        client_a, vessel_a = make(50_000, 1_215_000, argp=math.pi)
        client_b, vessel_b = make(50_000, 1_215_000, argp=0.0)
        cost_a = node_cost(maneuver.change_inclination_node(client_a, vessel_a, 90.0))
        cost_b = node_cost(maneuver.change_inclination_node(client_b, vessel_b, 90.0))
        assert cost_a == pytest.approx(cost_b, rel=0.05)

    def test_circular_orbit_nodes_are_equivalent(self):
        client_a, vessel_a = make(80_000, 80_000, argp=0.0)
        client_b, vessel_b = make(80_000, 80_000, argp=math.pi)
        cost_a = node_cost(maneuver.change_inclination_node(client_a, vessel_a, 45.0))
        cost_b = node_cost(maneuver.change_inclination_node(client_b, vessel_b, 45.0))
        assert cost_a == pytest.approx(cost_b, rel=1e-6)

    def test_burn_time_is_at_or_after_now(self):
        client, vessel = make(50_000, 1_215_000, argp=math.pi)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        assert node["ut"] >= client.space_center.ut - 1e-6


class TestPlaneChangeDoesNotExceedEscapeSpeed:
    """The original bug this function was written to fix, still guarded:
    an incorrect plane-change formula can add speed instead of only
    rotating it, and for a large angle that can exceed local escape
    velocity -- confirmed live once, flinging a satellite out of a moon's
    SOI instead of re-tilting its orbit."""

    @pytest.mark.parametrize("angle_deg", [5.0, 30.0, 60.0, 90.0, 120.0])
    def test_resulting_speed_matches_vis_viva_at_burn_radius(self, angle_deg):
        client, vessel = make(50_000, 50_000)
        node = maneuver.change_inclination_node(client, vessel, angle_deg)
        position = vessel.orbit.position_at(node["ut"], vessel.orbit.body.non_rotating_reference_frame)
        velocity = vessel.orbit.exact_velocity_at(node["ut"], vessel.orbit.body.non_rotating_reference_frame)
        prograde_hat = orbital.norm(velocity)
        normal_hat = orbital.norm(orbital.cross(position, velocity))
        radial_hat = orbital.cross(normal_hat, prograde_hat)
        new_velocity = tuple(
            v + node["prograde"] * p + node["normal"] * n + node["radial"] * r
            for v, p, n, r in zip(velocity, prograde_hat, normal_hat, radial_hat)
        )
        # rel=1e-6, not 1e-9: production's basis vectors come from a
        # centered finite difference (maneuver.velocity_at), not the exact
        # analytic velocity used here as ground truth -- a tiny, genuine
        # numerical discretization difference, not a bug. The original
        # wrong-formula bug this test guards against was off by tens of
        # percent, not parts per billion, so this tolerance still catches
        # it while not failing on floating-point-level noise.
        assert orbital.magnitude(new_velocity) == pytest.approx(orbital.magnitude(velocity), rel=1e-6)

    def test_ninety_degree_change_does_not_exceed_escape_speed(self):
        """The resulting SPEED must stay below escape velocity -- checking
        the delta-v magnitude against it directly doesn't make sense (a
        rotation's delta-v naturally approaches 2x the orbital speed for a
        90 degree turn; that was never the actual bug). The bug this
        guards is a formula that ADDS speed instead of only rotating it."""
        client, vessel = make(50_000, 50_000)
        r = MUN_RADIUS + 50_000
        escape = orbital.escape_speed(MU_MUN, r)
        node = maneuver.change_inclination_node(client, vessel, 90.0)

        position = vessel.orbit.position_at(node["ut"], vessel.orbit.body.non_rotating_reference_frame)
        velocity = vessel.orbit.exact_velocity_at(node["ut"], vessel.orbit.body.non_rotating_reference_frame)
        prograde_hat = orbital.norm(velocity)
        normal_hat = orbital.norm(orbital.cross(position, velocity))
        radial_hat = orbital.cross(normal_hat, prograde_hat)
        new_velocity = tuple(
            v + node["prograde"] * p + node["normal"] * n + node["radial"] * r_
            for v, p, n, r_ in zip(velocity, prograde_hat, normal_hat, radial_hat)
        )
        assert orbital.magnitude(new_velocity) < escape
