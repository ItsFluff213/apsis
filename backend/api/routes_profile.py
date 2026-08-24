from fastapi import APIRouter
from pydantic import BaseModel

from backend import db

router = APIRouter(prefix="/api/profile", tags=["profile"])


class SetProfileRequest(BaseModel):
    name: str


def build_router():
    @router.get("")
    def get_profile():
        return {"active": db.get_active_profile(), "profiles": db.list_profiles()}

    @router.post("")
    def set_profile(body: SetProfileRequest):
        db.set_active_profile(body.name)
        return {"active": db.get_active_profile(), "profiles": db.list_profiles()}

    return router
