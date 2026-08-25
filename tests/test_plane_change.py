"""Tests for plane-change node placement in backend/autopilots/maneuver.py.

Worth testing precisely because getting it wrong is not a crash -- it is a
burn that costs several times what it should and silently strands the
craft when the tank runs dry mid-maneuver. A 90 degree change around Mun is
166 m/s done at the capture orbit's apoapsis and 722 m/s done on a circular
50km orbit; nothing about the failure looks like a bug until you are out of
fuel.

kRPC is stubbed rather than mocked wholesale: these fakes implement the few
orbit properties the function reads, using real conic geometry, so the test
exercises the actual arithmetic.
"""

import math

import pytest

from backend.autopilots import maneuver

MU_MUN = 6.5138398e10
MUN_RADIUS = 200_000.0


class FakeOrbit:
    def __init__(self, periapsis_r, apoapsis_r, argument_of_periapsis=0.0, inclination=0.0):
        self.semi_major_axis = (periapsis_r + apoapsis_r) / 2.0
        self.eccentricity = (apoapsis_r - periapsis_r) / (apoapsis_r + periapsis_r)
        self.argument_of_periapsis = argument_of_periapsis
        self.inclination = inclination
        self.body = type("Body", (), {"gravitational_parameter": MU_MUN})()
        self.period = 2 * math.pi * math.sqrt(self.semi_major_axis ** 3 / MU_MUN)

    def radius_at_true_anomaly(self, true_anomaly):
        p = self.semi_major_axis * (1 - self.eccentricity ** 2)
        return p / (1 + self.eccentricity * math.cos(true_anomaly))

    def ut_at_true_anomaly(self, true_anomaly):
        # Monotonic in true anomaly over one revolution -- enough for the
        # "is it in the future" check the function does.
        return 1000.0 + (true_anomaly % (2 * math.pi)) / (2 * math.pi) * self.period


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
        self.space_center = type("SC", (), {"ut": 1000.0})()


def node_cost(node):
    return math.hypot(node["prograde"], node["normal"])


def make(periapsis_alt, apoapsis_alt, argp=0.0, inclination_deg=0.0):
    orbit = FakeOrbit(
        MUN_RADIUS + periapsis_alt, MUN_RADIUS + apoapsis_alt,
        argument_of_periapsis=argp, inclination=math.radians(inclination_deg),
    )
    return FakeClient(), FakeVessel(orbit)


class TestPlaneChangeCost:
    def test_polar_change_on_circular_50km_orbit_is_expensive(self):
        """The baseline: doing it on the final circular orbit."""
        client, vessel = make(50_000, 50_000)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        assert node_cost(node) == pytest.approx(722, abs=15)

    def test_polar_change_on_capture_orbit_is_far_cheaper(self):
        """The same maneuver on the elliptical capture orbit, with the
        ascending node out at apoapsis."""
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
        """Ascending and descending nodes sit half an orbit apart, so one is
        always slower and cheaper. Whichever way round the orbit is
        oriented, the cost must come out the same -- if the function always
        took the ascending node it would be cheap for one argp and
        expensive for the opposite one."""
        client_a, vessel_a = make(50_000, 1_215_000, argp=math.pi)
        client_b, vessel_b = make(50_000, 1_215_000, argp=0.0)
        cost_a = node_cost(maneuver.change_inclination_node(client_a, vessel_a, 90.0))
        cost_b = node_cost(maneuver.change_inclination_node(client_b, vessel_b, 90.0))
        assert cost_a == pytest.approx(cost_b, rel=0.02)

    def test_circular_orbit_nodes_are_equivalent(self):
        """With no eccentricity there is nothing to choose between them, so
        behaviour must be unchanged from before this optimization."""
        client_a, vessel_a = make(80_000, 80_000, argp=0.0)
        client_b, vessel_b = make(80_000, 80_000, argp=math.pi)
        cost_a = node_cost(maneuver.change_inclination_node(client_a, vessel_a, 45.0))
        cost_b = node_cost(maneuver.change_inclination_node(client_b, vessel_b, 45.0))
        assert cost_a == pytest.approx(cost_b, rel=1e-9)

    def test_burn_time_is_in_the_future(self):
        client, vessel = make(50_000, 1_215_000, argp=math.pi)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        assert node["ut"] >= client.space_center.ut


class TestPlaneChangePreservesSpeed:
    """A plane change must rotate the velocity vector without lengthening
    it. An earlier version applied the whole delta-v as a pure normal burn,
    which for a large angle *added* speed on top of the existing forward
    motion -- confirmed live as flinging a satellite clean out of Minmus's
    SOI instead of re-tilting its orbit."""

    @pytest.mark.parametrize("angle_deg", [5.0, 30.0, 60.0, 90.0, 120.0])
    def test_resulting_speed_is_unchanged(self, angle_deg):
        client, vessel = make(50_000, 50_000)
        orbit = vessel.orbit
        v = math.sqrt(MU_MUN / (MUN_RADIUS + 50_000))
        node = maneuver.change_inclination_node(client, vessel, angle_deg)

        # Original velocity is entirely prograde; the burn adds a prograde
        # component and a normal one.
        new_prograde = v + node["prograde"]
        new_speed = math.hypot(new_prograde, node["normal"])
        assert new_speed == pytest.approx(v, rel=1e-9)

    def test_ninety_degree_change_does_not_exceed_escape_speed(self):
        """The specific failure that stranded a real satellite."""
        client, vessel = make(50_000, 50_000)
        r = MUN_RADIUS + 50_000
        v = math.sqrt(MU_MUN / r)
        escape = math.sqrt(2 * MU_MUN / r)
        node = maneuver.change_inclination_node(client, vessel, 90.0)
        new_speed = math.hypot(v + node["prograde"], node["normal"])
        assert new_speed < escape
