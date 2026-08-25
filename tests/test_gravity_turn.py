"""Tests for the ascent steering law in backend/autopilots/ascent.py.

These exist because of a confirmed live failure: the pitch kick was defined
(PITCH_KICK_DEG) but never actually applied, so the "gravity turn" flew
dead vertical for the entire ascent. The bug was invisible from reading
run_ascent() itself -- it only shows up by working through what
_gravity_turn_pitch actually returns at the moment the kick fires, which is
what these tests do, plus a small closed-loop simulation so the failure
mode (holds 90 degrees forever) can't silently come back unnoticed.
"""

import math

import pytest

from backend.autopilots import ascent


class FakeFlight:
    def __init__(self, horizontal_speed, vertical_speed):
        self.horizontal_speed = horizontal_speed
        self.vertical_speed = vertical_speed

    @property
    def speed(self):
        return math.hypot(self.horizontal_speed, self.vertical_speed)


class TestPitchKickIsActuallyApplied:
    """The regression itself: right at the moment `kicked` becomes True,
    the craft is still moving essentially straight up. The commanded pitch
    at that exact instant must differ from 90 -- if it doesn't, nothing
    will ever tip the velocity vector off vertical."""

    def test_kick_moment_commands_off_vertical(self):
        # Exactly the state at the tick where kicked flips true: vertical
        # speed just crossed the trigger, horizontal is still ~0.
        flight = FakeFlight(horizontal_speed=0.05, vertical_speed=ascent.PITCH_KICK_SPEED_MS)
        pitch = ascent._gravity_turn_pitch(flight, kicked=True, altitude=500,
                                           turn_start_altitude_m=1000, turn_end_altitude_m=45000)
        assert pitch < 90.0, "the kick must command a real deviation, not something equal to vertical"
        assert pitch == pytest.approx(90.0 - ascent.PITCH_KICK_DEG)

    def test_before_the_kick_holds_vertical(self):
        flight = FakeFlight(horizontal_speed=0.0, vertical_speed=10.0)
        pitch = ascent._gravity_turn_pitch(flight, kicked=False, altitude=500,
                                           turn_start_altitude_m=1000, turn_end_altitude_m=45000)
        assert pitch == 90.0

    def test_stays_kicked_while_horizontal_speed_is_still_negligible(self):
        """A few ticks after the kick, before real horizontal speed has
        built up -- must still be commanding the kick angle, not have
        silently reverted to 90 by reading noise as prograde."""
        flight = FakeFlight(horizontal_speed=1.5, vertical_speed=65.0)
        pitch = ascent._gravity_turn_pitch(flight, kicked=True, altitude=600,
                                           turn_start_altitude_m=1000, turn_end_altitude_m=45000)
        assert pitch == pytest.approx(90.0 - ascent.PITCH_KICK_DEG)


class TestHandoverToProgradeFollowing:
    """Once real horizontal speed exists, the law should switch to
    following it -- and do so without a large discontinuity, since a jump
    here is exactly the kind of thing that reintroduces steering loss."""

    def test_switches_to_prograde_once_established(self):
        flight = FakeFlight(horizontal_speed=ascent.KICK_ESTABLISHED_MS + 1, vertical_speed=100.0)
        pitch = ascent._gravity_turn_pitch(flight, kicked=True, altitude=2000,
                                           turn_start_altitude_m=1000, turn_end_altitude_m=45000)
        expected_prograde = math.degrees(math.atan2(100.0, ascent.KICK_ESTABLISHED_MS + 1))
        assert pitch == pytest.approx(expected_prograde, abs=0.01)

    def test_handover_has_no_large_discontinuity(self):
        """Compare the commanded pitch just below and just above the
        established-speed threshold, at a velocity ratio representative of
        an actual kick-driven ascent (see the docstring in ascent.py)."""
        just_below = ascent._gravity_turn_pitch(
            FakeFlight(ascent.KICK_ESTABLISHED_MS - 0.1, 119.0), kicked=True,
            altitude=2000, turn_start_altitude_m=1000, turn_end_altitude_m=45000,
        )
        just_above = ascent._gravity_turn_pitch(
            FakeFlight(ascent.KICK_ESTABLISHED_MS + 0.1, 119.0), kicked=True,
            altitude=2000, turn_start_altitude_m=1000, turn_end_altitude_m=45000,
        )
        assert abs(just_below - just_above) < 2.0


class TestClosedLoopDoesNotFlyDeadVertical:
    """The actual regression scenario: simulate the kick's own effect on
    the velocity vector (a small thrust deflection at PITCH_KICK_DEG builds
    horizontal speed over a few seconds) and confirm the craft is
    measurably off vertical shortly after the kick -- not still commanding
    90 degrees, which is what the unfixed code did forever."""

    def test_horizontal_speed_develops_after_the_kick(self):
        thrust_accel = 25.0  # m/s^2, representative early-ascent TWR
        g = 9.8
        horizontal, vertical = 0.0, ascent.PITCH_KICK_SPEED_MS
        dt = 0.1
        kicked = True

        for _ in range(100):  # 10 simulated seconds
            flight = FakeFlight(horizontal, vertical)
            pitch = ascent._gravity_turn_pitch(flight, kicked, altitude=2000,
                                               turn_start_altitude_m=1000, turn_end_altitude_m=45000)
            pitch_rad = math.radians(pitch)
            horizontal += thrust_accel * math.cos(pitch_rad) * dt
            vertical += (thrust_accel * math.sin(pitch_rad) - g) * dt

        assert horizontal > 5.0, (
            "no meaningful horizontal speed developed -- the craft is still "
            "flying straight up, which is the exact bug this test guards against"
        )
