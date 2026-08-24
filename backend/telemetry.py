"""Reads live flight telemetry for a vessel via kRPC."""


def get_telemetry(vessel):
    orbit = vessel.orbit
    flight = vessel.flight(vessel.orbit.body.reference_frame)
    return {
        "altitude": flight.mean_altitude,
        "surface_altitude": flight.surface_altitude,
        "speed": flight.speed,
        "vertical_speed": flight.vertical_speed,
        "horizontal_speed": flight.horizontal_speed,
        "apoapsis_altitude": orbit.apoapsis_altitude,
        "periapsis_altitude": orbit.periapsis_altitude,
        "inclination_deg": orbit.inclination * 57.29577951308232,
        "true_anomaly_deg": orbit.true_anomaly * 57.29577951308232,
        "eccentricity": orbit.eccentricity,
        "body": orbit.body.name,
        "latitude": flight.latitude,
        "longitude": flight.longitude,
        "situation": vessel.situation.name,
        "stage": vessel.control.current_stage,
    }
