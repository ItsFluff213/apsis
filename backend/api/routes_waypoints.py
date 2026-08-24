from fastapi import APIRouter, HTTPException

from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/waypoints", tags=["waypoints"])


def build_router(client):
    @router.get("")
    def list_waypoints():
        try:
            sc = client.space_center
            return [
                {
                    "name": wp.name,
                    "body": wp.body.name,
                    "latitude": wp.latitude,
                    "longitude": wp.longitude,
                }
                for wp in sc.waypoint_manager.waypoints
            ]
        except NotConnected:
            return []
        except Exception:
            # kRPC is connected but this call isn't valid right now (e.g.
            # "Procedure not available in game scene 'EditorVAB'" while
            # you're in the VAB rather than flight/tracking station).
            return []

    return router
