"""Tests for backend/orbital.py against known-good Kerbol system numbers.

Everything here is pure math -- no KSP, no kRPC, no game running. The
reference values are the stock system's published orbital elements and the
transfer figures the KSP community has independently derived for them
(e.g. the ~44.4 degree Kerbin->Duna departure phase angle that every
transfer-window chart agrees on). If a refactor of the transfer math ever
breaks one of these, it broke the physics, not just the code.

Run: python -m pytest tests/ -q
"""

import math

import pytest

from backend import orbital

# --- Stock Kerbol system constants (from the KSP wiki / game data) ------
MU_SUN = 1.1723328e18
MU_KERBIN = 3.5316000e12
MU_MUN = 6.5138398e10

KERBIN_SMA = 13_599_840_256.0
DUNA_SMA = 20_726_155_264.0
EVE_SMA = 9_832_684_544.0
KERBIN_RADIUS = 600_000.0
MUN_SMA = 12_000_000.0

KERBIN_YEAR_S = 9_203_545.0  # 426.08 six-hour Kerbin days
MUN_PERIOD_S = 138_984.0


class TestVectorMath:
    def test_cross_is_right_handed(self):
        assert orbital.cross((1, 0, 0), (0, 1, 0)) == pytest.approx((0, 0, 1))

    def test_norm_gives_unit_length(self):
        assert orbital.magnitude(orbital.norm((3.0, 4.0, 0.0))) == pytest.approx(1.0)

    def test_rotate_about_axis_quarter_turn(self):
        rotated = orbital.rotate_about_axis((1, 0, 0), (0, 0, 1), math.pi / 2)
        assert rotated == pytest.approx((0, 1, 0), abs=1e-12)

    def test_rotate_about_axis_preserves_length(self):
        vec = (1.0, -2.0, 3.5)
        rotated = orbital.rotate_about_axis(vec, (0.3, 1.0, -0.7), 1.234)
        assert orbital.magnitude(rotated) == pytest.approx(orbital.magnitude(vec))

    def test_angle_of_projects_onto_xz(self):
        # y is the out-of-plane axis and must be ignored entirely.
        assert orbital.angle_of((1.0, 999.0, 0.0)) == pytest.approx(0.0)
        assert orbital.angle_of((0.0, -50.0, 1.0)) == pytest.approx(math.pi / 2)

    def test_signed_angle_difference_takes_short_way_round(self):
        # 350 deg -> 10 deg is +20, not -340.
        diff = orbital.signed_angle_difference(math.radians(10), math.radians(350))
        assert math.degrees(diff) == pytest.approx(20.0)


class TestTwoBodyBasics:
    def test_kerbin_year_matches_stock(self):
        period = orbital.period_for_sma(MU_SUN, KERBIN_SMA)
        assert period == pytest.approx(KERBIN_YEAR_S, rel=1e-3)

    def test_mun_period_matches_stock(self):
        period = orbital.period_for_sma(MU_KERBIN, MUN_SMA)
        assert period == pytest.approx(MUN_PERIOD_S, rel=1e-3)

    def test_sma_for_period_inverts_period_for_sma(self):
        period = orbital.period_for_sma(MU_SUN, DUNA_SMA)
        assert orbital.sma_for_period(MU_SUN, period) == pytest.approx(DUNA_SMA)

    def test_low_kerbin_orbit_speed(self):
        # 100km LKO circular speed is ~2246 m/s in stock KSP.
        speed = orbital.circular_speed(MU_KERBIN, KERBIN_RADIUS + 100_000)
        assert speed == pytest.approx(2246, abs=5)

    def test_escape_is_sqrt2_times_circular(self):
        r = KERBIN_RADIUS + 100_000
        assert orbital.escape_speed(MU_KERBIN, r) == pytest.approx(
            math.sqrt(2) * orbital.circular_speed(MU_KERBIN, r)
        )

    def test_vis_viva_at_circular_orbit_matches_circular_speed(self):
        r = KERBIN_RADIUS + 250_000
        assert orbital.vis_viva_speed(MU_KERBIN, r, r) == pytest.approx(
            orbital.circular_speed(MU_KERBIN, r)
        )


class TestHohmannKerbinToDuna:
    """The canonical outward transfer. Every number here is cross-checkable
    against published KSP transfer-window charts."""

    def test_transfer_semi_major_axis(self):
        a, _, _, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, DUNA_SMA)
        assert a == pytest.approx((KERBIN_SMA + DUNA_SMA) / 2)

    def test_time_of_flight_about_300_kerbin_days(self):
        _, tof, _, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, DUNA_SMA)
        assert tof / 21600 == pytest.approx(302, abs=5)  # 21600s = one 6h Kerbin day

    def test_departure_phase_angle_matches_community_value(self):
        """~44.4 degrees ahead -- the single most-quoted number for this
        transfer, and the one a wrong sign convention would flip."""
        phase = math.degrees(orbital.hohmann_phase_angle(MU_SUN, KERBIN_SMA, DUNA_SMA))
        assert phase == pytest.approx(44.4, abs=0.5)

    def test_ejection_burn_from_100km_lko(self):
        """~1050-1060 m/s is the accepted figure for Kerbin->Duna from a
        100km parking orbit. This is the whole point of doing the ejection
        properly rather than just applying the heliocentric delta-v: the
        heliocentric number is ~918 m/s, and burning that from LKO would
        fall well short."""
        _, _, dv_helio, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, DUNA_SMA)
        r_park = KERBIN_RADIUS + 100_000
        burn = orbital.ejection_speed(MU_KERBIN, r_park, dv_helio) - orbital.circular_speed(MU_KERBIN, r_park)
        assert burn == pytest.approx(1055, abs=25)
        assert dv_helio == pytest.approx(918, abs=10)

    def test_ejection_angle_is_obtuse(self):
        """The burn happens well before the escape direction -- around
        150 degrees around the parking orbit for a transfer this size."""
        _, _, dv_helio, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, DUNA_SMA)
        angle = math.degrees(orbital.ejection_angle(MU_KERBIN, KERBIN_RADIUS + 100_000, dv_helio))
        assert 120 < angle < 170


class TestHohmannInward:
    """Kerbin -> Eve. Going inward flips several signs, which is exactly
    where a transfer planner written only against an outward case breaks."""

    def test_departure_delta_v_is_retrograde(self):
        _, _, dv_helio, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, EVE_SMA)
        assert dv_helio < 0, "slowing down is what drops you toward the Sun"

    def test_phase_angle_is_negative(self):
        """Eve must be *behind* Kerbin at departure -- it moves faster and
        catches up to the rendezvous point."""
        phase = math.degrees(orbital.hohmann_phase_angle(MU_SUN, KERBIN_SMA, EVE_SMA))
        assert phase < 0

    def test_time_of_flight_is_positive_and_shorter_than_duna(self):
        _, tof_eve, _, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, EVE_SMA)
        _, tof_duna, _, _ = orbital.hohmann_transfer(MU_SUN, KERBIN_SMA, DUNA_SMA)
        assert 0 < tof_eve < tof_duna


class TestEjectionEdgeCases:
    def test_oberth_saving_is_larger_from_low_orbit(self):
        """The Oberth effect, stated correctly.

        Note what it does NOT say: escaping from a *high* circular orbit
        genuinely needs less additional delta-v than from a low one, simply
        because you already paid to climb most of the way out of the well.

        What Oberth says is that buying hyperbolic excess speed deep in the
        well is disproportionately cheap. Compare each orbit's real
        ejection burn against the naive "escape first, then add v_inf in
        free space" cost: the shortfall is the Oberth saving, and it is
        bigger the deeper you burn. If the 2*mu/r term in ejection_speed
        were ever dropped, both savings would collapse to zero.
        """
        v_inf = 1000.0

        def saving(r):
            escape_burn = orbital.escape_speed(MU_KERBIN, r) - orbital.circular_speed(MU_KERBIN, r)
            naive = escape_burn + v_inf
            actual = orbital.ejection_speed(MU_KERBIN, r, v_inf) - orbital.circular_speed(MU_KERBIN, r)
            return naive - actual

        low_saving = saving(KERBIN_RADIUS + 100_000)
        high_saving = saving(KERBIN_RADIUS + 2_000_000)
        assert low_saving > high_saving > 0

    def test_zero_excess_velocity_gives_escape_speed(self):
        r = KERBIN_RADIUS + 100_000
        assert orbital.ejection_speed(MU_KERBIN, r, 0.0) == pytest.approx(orbital.escape_speed(MU_KERBIN, r))

    def test_zero_excess_velocity_angle_does_not_blow_up(self):
        """A parabolic escape has e == 1 exactly, where acos(-1/e) is a
        boundary case. Must return a sane angle rather than raising."""
        angle = orbital.ejection_angle(MU_KERBIN, KERBIN_RADIUS + 100_000, 0.0)
        assert 0 < angle <= math.pi


class TestKeplerTimeOfFlight:
    def test_half_period_from_periapsis_to_apoapsis(self):
        a, e = KERBIN_RADIUS + 300_000, 0.3
        tof = orbital.time_between_true_anomalies(MU_KERBIN, a, e, 0.0, math.pi)
        assert tof == pytest.approx(orbital.period_for_sma(MU_KERBIN, a) / 2)

    def test_full_lap_is_one_period(self):
        a, e = KERBIN_RADIUS + 300_000, 0.3
        period = orbital.period_for_sma(MU_KERBIN, a)
        # Going from nu round to nu again wraps to a full period, not zero.
        tof = orbital.time_between_true_anomalies(MU_KERBIN, a, e, 1.0, 1.0 - 1e-9)
        assert tof == pytest.approx(period, rel=1e-6)

    def test_time_of_flight_is_never_negative(self):
        a, e = KERBIN_RADIUS + 300_000, 0.4
        # Backwards in true anomaly must wrap forward through periapsis.
        tof = orbital.time_between_true_anomalies(MU_KERBIN, a, e, 3.0, 1.0)
        assert tof > 0

    def test_ellipse_spends_longer_near_apoapsis(self):
        """Kepler's second law: equal areas in equal times, so the same
        angular sweep takes longer out at apoapsis than down at periapsis.
        A sign error in the eccentric-anomaly conversion inverts this."""
        a, e = KERBIN_RADIUS + 500_000, 0.5
        near_peri = orbital.time_between_true_anomalies(MU_KERBIN, a, e, -0.4, 0.4)
        near_apo = orbital.time_between_true_anomalies(MU_KERBIN, a, e, math.pi - 0.4, math.pi + 0.4)
        assert near_apo > near_peri

    def test_circular_orbit_sweeps_uniformly(self):
        a = KERBIN_RADIUS + 100_000
        period = orbital.period_for_sma(MU_KERBIN, a)
        quarter = orbital.time_between_true_anomalies(MU_KERBIN, a, 0.0, 0.0, math.pi / 2)
        assert quarter == pytest.approx(period / 4)

    def test_true_anomaly_at_radius_recovers_apsides(self):
        a, e = KERBIN_RADIUS + 400_000, 0.25
        r_peri, r_apo = a * (1 - e), a * (1 + e)
        assert orbital.true_anomaly_at_radius(a, e, r_peri) == pytest.approx(0.0, abs=1e-6)
        assert orbital.true_anomaly_at_radius(a, e, r_apo) == pytest.approx(math.pi, abs=1e-6)

    def test_true_anomaly_at_unreachable_radius_raises(self):
        a, e = KERBIN_RADIUS + 400_000, 0.25
        with pytest.raises(ValueError):
            orbital.true_anomaly_at_radius(a, e, a * (1 + e) * 2)

    def test_deorbit_geometry_impact_before_periapsis(self):
        """The case targeted deorbiting actually relies on: periapsis is
        driven below the surface, so the vessel meets the ground at some
        true anomaly short of it."""
        a, e = 3_000_000.0, 0.85
        assert a * (1 - e) < KERBIN_RADIUS, "test setup: periapsis must be underground"
        nu_impact = orbital.true_anomaly_at_radius(a, e, KERBIN_RADIUS)
        assert 0 < nu_impact < math.pi


class TestSphereOfInfluence:
    def test_mun_soi_matches_stock(self):
        """Stock Mun SOI is 2,429,559 m."""
        mass_mun = MU_MUN / 6.67430e-11
        mass_kerbin = MU_KERBIN / 6.67430e-11
        soi = orbital.sphere_of_influence(MUN_SMA, mass_mun, mass_kerbin)
        assert soi == pytest.approx(2_429_559, rel=0.01)
