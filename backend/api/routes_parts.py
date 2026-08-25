from fastapi import APIRouter, HTTPException

from backend import parts as parts_mod
from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/vessels", tags=["parts"])


def build_router(registry):
    @router.get("/{vessel_id}/parts")
    def list_parts(vessel_id: str):
        try:
            vessel = registry.get_vessel_object(vessel_id)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")
        return parts_mod.list_parts(vessel)

    @router.get("/{vessel_id}/docking-ports")
    def list_docking_ports(vessel_id: str):
        try:
            vessel = registry.get_vessel_object(vessel_id)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")
        return parts_mod.list_docking_ports(vessel)

    return router
