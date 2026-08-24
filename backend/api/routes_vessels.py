from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db
from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/vessels", tags=["vessels"])


class RenameRequest(BaseModel):
    name: str


class SetRoleRequest(BaseModel):
    category: str  # one of db.VALID_TYPES, or "" to clear
    detail: str = ""


def build_router(registry):
    @router.get("")
    def list_vessels():
        return registry.list_vessels()

    @router.post("/{vessel_id}/name")
    def rename(vessel_id: str, body: RenameRequest):
        try:
            registry.rename(vessel_id, body.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @router.post("/{vessel_id}/role")
    def set_role(vessel_id: str, body: SetRoleRequest):
        """Sets the vessel's role by writing the same tag convention onto
        its controlling part (core/cockpit) that in-game tagging uses --
        this is just a more convenient way to set the same thing, not a
        separate mechanism, so it stays in sync with tagging in-game."""
        if body.category and body.category not in db.VALID_TYPES:
            raise HTTPException(status_code=400, detail=f"invalid category, must be one of {sorted(db.VALID_TYPES)}")
        try:
            vessel = registry.get_vessel_object(vessel_id)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")
        controlling = vessel.parts.controlling
        if controlling is None:
            raise HTTPException(status_code=409, detail="vessel has no controlling part")
        controlling.tag = f"{body.category}.{body.detail}" if body.category and body.detail else body.category
        return {"ok": True}

    return router
