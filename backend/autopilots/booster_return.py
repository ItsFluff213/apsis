"""Booster return-to-KSC autopilot: boostback burn to cancel downrange
velocity and head back toward the pad, then the shared suicide-burn
descent for landing.

Meant to be run on a spent stage right after it separates during ascent
(kRPC lists a separated stage as its own vessel, so it can be targeted
independently).

Reentry orientation is handled now: the shared descent guidance holds the
craft retrograde through the heating phase (see
descent.hold_retrograde_through_reentry), so a tagged `heatshield.*` part
faces the airflow without anyone having to point it by hand first. That
used to be an explicit scope limitation documented here.
"""

from backend import geo
from backend.autopilots import descent, maneuver


def _wait_for_clear_separation(client, vessel, job, min_distance_m=50, max_wait_s=20):
    """Backstop for whatever separated this vessel: if the continuing stage
    didn't (or couldn't) burn away first, don't start the boostback burn
    while still within collision range of it. Real fix lives in ascent.py
    (the continuing stage burns away right after staging); this is just a
    safety net in case this job gets run some other way."""
    job.message = "waiting for clear separation"
    vessel.control.throttle = 0.0
    elapsed = 0.0
    while elapsed < max_wait_s:
        job.check_abort()
        dist = maneuver.closest_other_vessel_distance(client, vessel)
        if dist is None or dist >= min_distance_m:
            return
        job.sleep(0.2)
        elapsed += 0.2
    # Timed out still close to something -- proceed anyway rather than
    # wait forever, but this is a real residual risk, not a guarantee.


def run_booster_return(client, vessel, job, boostback_pitch_deg=15, horizontal_speed_threshold=20,
                        boostback_timeout_s=60):
    sc = client.space_center

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)

    control = vessel.control
    ap = vessel.auto_pilot
    control.sas = False
    control.rcs = True
    ap.engaged = True

    _wait_for_clear_separation(client, vessel, job)

    job.message = "boostback burn"
    control.throttle = 1.0
    elapsed = 0.0
    try:
        while elapsed < boostback_timeout_s:
            job.check_abort()
            flight = vessel.flight(vessel.orbit.body.reference_frame)
            if flight.horizontal_speed < horizontal_speed_threshold:
                break
            if vessel.available_thrust < 0.1:
                job.message = "boostback burn: out of thrust, proceeding to descent"
                break
            heading = geo.bearing_to(flight.latitude, flight.longitude, geo.KSC_PAD_LATITUDE, geo.KSC_PAD_LONGITUDE)
            ap.target_pitch_and_heading(boostback_pitch_deg, heading)
            job.sleep(0.1)
            elapsed += 0.1
    finally:
        control.throttle = 0.0

    job.message = "coasting toward landing site"
    descent.suicide_burn_landing(
        client, vessel, job,
        target_lat=geo.KSC_PAD_LATITUDE, target_lon=geo.KSC_PAD_LONGITUDE,
    )
