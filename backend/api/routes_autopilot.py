from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import interplanetary as interplanetary_plan
from backend.autopilots import ascent, booster_return, interplanetary, landing, moon_transfer
from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class AscentRequest(BaseModel):
    target_apoapsis_m: float
    target_periapsis_m: float
    target_inclination_deg: float = 0.0


class LandingRequest(BaseModel):
    target_lat: float
    target_lon: float


class InterplanetaryRequest(BaseModel):
    plan_text: str


class MoonTransferRequest(BaseModel):
    moon_name: str
    target_periapsis_m: float
    target_inclination_deg: float | None = None


def build_router(client, registry, jobs):
    def get_vessel_or_404(vessel_id: str):
        try:
            vessel = registry.get_vessel_object(vessel_id)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")
        return vessel

    @router.post("/{vessel_id}/ascent")
    def start_ascent(vessel_id: str, body: AscentRequest):
        vessel = get_vessel_or_404(vessel_id)

        def run(job):
            ascent.run_ascent(
                client,
                vessel,
                job,
                target_apoapsis_m=body.target_apoapsis_m,
                target_periapsis_m=body.target_periapsis_m,
                target_inclination_deg=body.target_inclination_deg,
            )

        job = jobs.start(vessel_id, "ascent", run, body.model_dump())
        return job.to_dict()

    @router.post("/{vessel_id}/landing")
    def start_landing(vessel_id: str, body: LandingRequest):
        vessel = get_vessel_or_404(vessel_id)

        def run(job):
            landing.run_landing(client, vessel, job, target_lat=body.target_lat, target_lon=body.target_lon)

        job = jobs.start(vessel_id, "landing", run, body.model_dump())
        return job.to_dict()

    @router.post("/{vessel_id}/booster-return")
    def start_booster_return(vessel_id: str):
        vessel = get_vessel_or_404(vessel_id)

        def run(job):
            booster_return.run_booster_return(client, vessel, job)

        job = jobs.start(vessel_id, "booster-return", run, {})
        return job.to_dict()

    @router.post("/{vessel_id}/interplanetary")
    def start_interplanetary(vessel_id: str, body: InterplanetaryRequest):
        vessel = get_vessel_or_404(vessel_id)
        try:
            steps = interplanetary_plan.parse_plan(body.plan_text)
        except interplanetary_plan.PlanParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        def run(job):
            interplanetary.run_interplanetary_transfer(client, vessel, job, steps)

        job = jobs.start(vessel_id, "interplanetary", run, {"sequence": interplanetary_plan.parse_sequence_name(body.plan_text)})
        return job.to_dict()

    @router.post("/{vessel_id}/moon-transfer")
    def start_moon_transfer(vessel_id: str, body: MoonTransferRequest):
        vessel = get_vessel_or_404(vessel_id)

        def run(job):
            moon_transfer.run_moon_transfer(
                client, vessel, job, moon_name=body.moon_name, target_periapsis_m=body.target_periapsis_m,
                target_inclination_deg=body.target_inclination_deg,
            )

        job = jobs.start(vessel_id, "moon-transfer", run, body.model_dump())
        return job.to_dict()

    @router.post("/interplanetary/parse")
    def parse_interplanetary_plan(body: InterplanetaryRequest):
        """Preview-only: parses the pasted plan and returns the steps with
        their delta-v breakdown, without touching kRPC or starting a job --
        lets the dashboard show what it understood before committing to it."""
        try:
            steps = interplanetary_plan.parse_plan(body.plan_text)
        except interplanetary_plan.PlanParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"sequence": interplanetary_plan.parse_sequence_name(body.plan_text), "steps": steps}

    @router.get("/{vessel_id}/status")
    def status(vessel_id: str):
        job = jobs.get(vessel_id)
        if job is None:
            return {"vessel_id": vessel_id, "status": "none"}
        return job.to_dict()

    @router.post("/{vessel_id}/abort")
    def abort(vessel_id: str):
        ok = jobs.abort(vessel_id)
        return {"ok": ok}

    return router
