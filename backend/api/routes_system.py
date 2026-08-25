import math

from fastapi import APIRouter

from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/system", tags=["system"])


def build_router(client):
    @router.get("")
    def system_bodies():
        try:
            bodies = client.space_center.bodies
        except NotConnected:
            return []
        result = []
        for name, body in bodies.items():
            if body.orbit is None:
                result.append({
                    "name": name, "parent": None, "angle_deg": 0,
                    "semi_major_axis_m": None, "eccentricity": 0, "radius_m": None,
                })
                continue
            orbit = body.orbit
            true_anomaly = orbit.true_anomaly
            ecc = orbit.eccentricity
            # Absolute angle in the orbital plane, not just angle-from-periapsis:
            # true_anomaly alone is relative to the periapsis direction, so using
            # it as the placement angle rotates every eccentric-periapsis body to
            # the wrong spot relative to its actual position in the game. Adding
            # argument_of_periapsis gives the real angle from the plane's
            # reference direction, which is what a top-down map needs.
            angle_deg = math.degrees(orbit.argument_of_periapsis + true_anomaly) % 360
            # Real instantaneous orbital radius (conic equation), not the
            # semi-major axis -- for any body with non-negligible eccentricity
            # (Moho, Eeloo, ...) using the SMA as a fixed circle radius visibly
            # disagrees with where the body actually is in-game.
            radius_m = orbit.semi_major_axis * (1 - ecc * ecc) / (1 + ecc * math.cos(true_anomaly))
            result.append({
                "name": name,
                "parent": orbit.body.name,
                "angle_deg": angle_deg,
                "semi_major_axis_m": orbit.semi_major_axis,
                "eccentricity": ecc,
                "radius_m": radius_m,
                "argument_of_periapsis_deg": math.degrees(orbit.argument_of_periapsis) % 360,
                # Real orbital tilt, e.g. Moho ~7 deg, Eeloo ~6.15 deg -- the
                # map used to force every body onto one flat plane regardless
                # of this, which looked tidy but wasn't what the system
                # actually looks like. Added so the 3D map can tilt each
                # body's actual position, not just draw a flat ring under it.
                "inclination_deg": math.degrees(orbit.inclination),
            })
        return result

    return router
