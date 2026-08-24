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
                result.append({"name": name, "parent": None, "angle_deg": 0, "semi_major_axis_m": None})
                continue
            result.append({
                "name": name,
                "parent": body.orbit.body.name,
                "angle_deg": math.degrees(body.orbit.true_anomaly),
                "semi_major_axis_m": body.orbit.semi_major_axis,
            })
        return result

    return router
