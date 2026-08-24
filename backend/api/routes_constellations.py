import json
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db

router = APIRouter(prefix="/api/constellations", tags=["constellations"])


class CreateConstellationRequest(BaseModel):
    name: str
    body: str
    kind: str  # "communications" or "custom"
    altitude_m: float | None = None
    inclination_deg: float = 0.0


class AddMemberRequest(BaseModel):
    vessel_id: str


class UpdateOrbitRequest(BaseModel):
    altitude_m: float | None = None
    inclination_deg: float | None = None


class SyncPullRequest(BaseModel):
    source_url: str  # e.g. "http://192.168.1.23:8000" -- a teammate's own dashboard, base URL only


def build_router(client, registry, jobs):
    @router.get("")
    def list_constellations():
        return db.list_constellations()

    @router.post("")
    def create_constellation(req: CreateConstellationRequest):
        try:
            cid = db.create_constellation(req.name, req.body, req.kind, req.altitude_m, req.inclination_deg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return db.get_constellation(cid)

    @router.patch("/{constellation_id}/orbit")
    def update_orbit(constellation_id: int, req: UpdateOrbitRequest):
        try:
            db.update_constellation_orbit(constellation_id, req.altitude_m, req.inclination_deg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return db.get_constellation(constellation_id)

    @router.delete("/{constellation_id}")
    def delete_constellation(constellation_id: int):
        db.delete_constellation(constellation_id)
        return {"ok": True}

    @router.post("/{constellation_id}/members")
    def add_member(constellation_id: int, req: AddMemberRequest):
        if db.get_constellation(constellation_id) is None:
            raise HTTPException(status_code=404, detail="unknown constellation id")
        db.add_constellation_member(constellation_id, req.vessel_id)
        return {"ok": True}

    @router.delete("/{constellation_id}/members/{vessel_id}")
    def remove_member(constellation_id: int, vessel_id: str):
        db.remove_constellation_member(vessel_id)
        return {"ok": True}

    @router.post("/sync/pull")
    def sync_pull(req: SyncPullRequest):
        """Pulls every constellation from a teammate's own dashboard (a
        separate backend+kRPC instance, e.g. each of you running your own
        copy against a shared multiplayer save) and merges them into this
        one's active profile. Existing local members are never removed by
        this -- see db.sync_merge_constellation for the merge rules."""
        url = req.source_url.rstrip("/") + "/api/constellations"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                remote_constellations = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise HTTPException(status_code=502, detail=f"couldn't reach {req.source_url}: {exc}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail=f"unexpected response from {req.source_url}: {exc}")

        merged = []
        for c in remote_constellations:
            cid = db.sync_merge_constellation(
                c["name"], c["body"], c["kind"], c["altitude_m"], c["inclination_deg"], c["members"],
            )
            merged.append(db.get_constellation(cid))
        return {"merged": merged}

    @router.post("/{constellation_id}/deploy/{vessel_id}")
    def deploy(constellation_id: int, vessel_id: str):
        from backend.autopilots import satellite

        if db.get_constellation(constellation_id) is None:
            raise HTTPException(status_code=404, detail="unknown constellation id")
        vessel = registry.get_vessel_object(vessel_id)
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")

        def run(job):
            satellite.run_deploy_satellite(client, registry, vessel, job, constellation_id)

        job = jobs.start(vessel_id, "deploy-satellite", run, {"constellation_id": constellation_id})
        return job.to_dict()

    return router
