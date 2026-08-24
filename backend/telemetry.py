"""Reads live flight telemetry for a vessel via kRPC.

Every attribute read here is a network round trip to the game. The naive
version of this function re-fetched `vessel.orbit` for each orbital value,
re-fetched `orbit.body` twice, and built a fresh `vessel.flight()` proxy on
every call -- so a single vessel cost roughly twenty round trips, and the
dashboard polls twice a second for every vessel it can see. With a handful
of craft in flight that was the dominant source of dashboard lag.

Two changes, both about round trips rather than the values themselves:

  * Fetch each proxy object (`orbit`, `body`, `flight`) exactly once per
    call and read every field off the local handle.
  * Cache the `flight()` proxy per vessel+body, since constructing it is
    itself a round trip and it stays valid as long as the vessel is in the
    same sphere of influence.

The bigger win available is kRPC's streaming API, which pushes values
instead of being polled and would take this to near zero round trips per
tick. That means managing stream lifecycles across a vessel list that
changes as craft are created, staged and destroyed, which is a larger
change than this pass -- noted here so it isn't mistaken for an oversight.
"""

import threading

RAD_TO_DEG = 57.29577951308232

# (vessel object id, body name) -> flight proxy. Keyed on the body too
# because a flight object is bound to one reference frame, so it must be
# rebuilt when a vessel changes sphere of influence.
_flight_cache = {}
_cache_lock = threading.Lock()


def _cached_flight(vessel, body):
    """The vessel's flight proxy in its current body's reference frame,
    reusing a previously built one where possible."""
    key = (getattr(vessel, "_object_id", id(vessel)), body.name)
    with _cache_lock:
        flight = _flight_cache.get(key)
    if flight is not None:
        return flight

    flight = vessel.flight(body.reference_frame)
    with _cache_lock:
        # Bound so a long session cycling through many craft (debris,
        # spent stages) can't grow this without limit.
        if len(_flight_cache) > 256:
            _flight_cache.clear()
        _flight_cache[key] = flight
    return flight


def forget_vessel(vessel):
    """Drop any cached proxies for a vessel that has gone away."""
    object_id = getattr(vessel, "_object_id", id(vessel))
    with _cache_lock:
        for key in [k for k in _flight_cache if k[0] == object_id]:
            del _flight_cache[key]


def get_telemetry(vessel):
    orbit = vessel.orbit
    body = orbit.body
    flight = _cached_flight(vessel, body)

    return {
        "altitude": flight.mean_altitude,
        "surface_altitude": flight.surface_altitude,
        "speed": flight.speed,
        "vertical_speed": flight.vertical_speed,
        "horizontal_speed": flight.horizontal_speed,
        "apoapsis_altitude": orbit.apoapsis_altitude,
        "periapsis_altitude": orbit.periapsis_altitude,
        "inclination_deg": orbit.inclination * RAD_TO_DEG,
        "true_anomaly_deg": orbit.true_anomaly * RAD_TO_DEG,
        "eccentricity": orbit.eccentricity,
        "body": body.name,
        "latitude": flight.latitude,
        "longitude": flight.longitude,
        "situation": vessel.situation.name,
        "stage": vessel.control.current_stage,
    }
