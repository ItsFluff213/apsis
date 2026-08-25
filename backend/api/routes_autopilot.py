"""Autopilot endpoints.

Every "start an autopilot" route is the same three steps -- resolve the
vessel or 404, wrap the autopilot call in a job function, hand it to the
JobManager -- so that shape lives once in `_start` rather than being
retyped per route.
"""

import math

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.autopilots import ascent, booster_return, docking, landing, moon_transfer, planet_transfer
from backend.krpc_client import NotConnected

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class AscentRequest(BaseModel):
    target_apoapsis_m: float
    target_periapsis_m: float
    target_inclination_deg: float = 0.0


class LandingRequest(BaseModel):
    target_lat: float
    target_lon: float


class MoonTransferRequest(BaseModel):
    moon_name: str
    target_periapsis_m: float
    target_inclination_deg: float | None = None


class PlanetTransferRequest(BaseModel):
    target_body_name: str
    target_periapsis_m: float
    target_inclination_deg: float | None = None
    # Departure parking orbit to establish before the ejection burn. The
    # ejection math is only valid for a circular orbit of known radius, so
    # this is settled first; omitted, the craft's current altitude is used.
    parking_altitude_m: float | None = None


class DockingRequest(BaseModel):
    target_vessel_id: str
    own_port_tag: str | None = None
    target_port_tag: str | None = None


class ResourceTransferRequest(BaseModel):
    resource_name: str
    amount: float | None = None  # None means "as much as will move"
    to_target: bool = True  # True pushes out of this craft, False pulls in


def build_router(client, registry, jobs):
    def get_vessel_or_404(vessel_id: str):
        try:
            vessel = registry.get_vessel_object(vessel_id)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if vessel is None:
            raise HTTPException(status_code=404, detail="unknown vessel id")
        return vessel

    def _start(vessel_id, kind, run_fn, params):
        """Resolve the vessel, start `run_fn(job, vessel)` as a background
        autopilot job, and return the job's status dict."""
        vessel = get_vessel_or_404(vessel_id)
        job = jobs.start(vessel_id, kind, lambda job: run_fn(job, vessel), params)
        return job.to_dict()

    @router.post("/{vessel_id}/ascent")
    def start_ascent(vessel_id: str, body: AscentRequest):
        return _start(
            vessel_id, "ascent",
            lambda job, vessel: ascent.run_ascent(
                client, vessel, job,
                target_apoapsis_m=body.target_apoapsis_m,
                target_periapsis_m=body.target_periapsis_m,
                target_inclination_deg=body.target_inclination_deg,
            ),
            body.model_dump(),
        )

    @router.post("/{vessel_id}/landing")
    def start_landing(vessel_id: str, body: LandingRequest):
        return _start(
            vessel_id, "landing",
            lambda job, vessel: landing.run_landing(
                client, vessel, job, target_lat=body.target_lat, target_lon=body.target_lon,
            ),
            body.model_dump(),
        )

    @router.post("/{vessel_id}/booster-return")
    def start_booster_return(vessel_id: str):
        return _start(
            vessel_id, "booster-return",
            lambda job, vessel: booster_return.run_booster_return(client, vessel, job),
            {},
        )

    @router.post("/{vessel_id}/moon-transfer")
    def start_moon_transfer(vessel_id: str, body: MoonTransferRequest):
        return _start(
            vessel_id, "moon-transfer",
            lambda job, vessel: moon_transfer.run_moon_transfer(
                client, vessel, job,
                moon_name=body.moon_name,
                target_periapsis_m=body.target_periapsis_m,
                target_inclination_deg=body.target_inclination_deg,
            ),
            body.model_dump(),
        )

    @router.post("/{vessel_id}/planet-transfer")
    def start_planet_transfer(vessel_id: str, body: PlanetTransferRequest):
        return _start(
            vessel_id, "planet-transfer",
            lambda job, vessel: planet_transfer.run_planet_transfer(
                client, vessel, job,
                target_body_name=body.target_body_name,
                target_periapsis_m=body.target_periapsis_m,
                target_inclination_deg=body.target_inclination_deg,
                parking_altitude_m=body.parking_altitude_m,
            ),
            body.model_dump(),
        )

    @router.post("/{vessel_id}/dock")
    def start_docking(vessel_id: str, body: DockingRequest):
        return _start(
            vessel_id, "docking",
            lambda job, vessel: docking.run_docking(
                client, registry, vessel, job,
                target_vessel_id=body.target_vessel_id,
                own_port_tag=body.own_port_tag,
                target_port_tag=body.target_port_tag,
            ),
            body.model_dump(),
        )

    @router.post("/{vessel_id}/transfer-resource")
    def start_resource_transfer(vessel_id: str, body: ResourceTransferRequest):
        return _start(
            vessel_id, "resource-transfer",
            lambda job, vessel: docking.run_resource_transfer(
                client, vessel, job,
                resource_name=body.resource_name,
                amount=body.amount,
                to_target=body.to_target,
            ),
            body.model_dump(),
        )

    @router.get("/{vessel_id}/moon-transfer/preview")
    def preview_moon_transfer(vessel_id: str, moon_name: str):
        """Read-only: computes the same closed-form direct-intercept plan
        run_moon_transfer would burn, without touching the game -- lets the
        dashboard draw the planned trajectory before committing to it."""
        vessel = get_vessel_or_404(vessel_id)
        try:
            parent = vessel.orbit.body
            moon = next((b for b in parent.satellites if b.name == moon_name), None)
            if moon is None:
                raise HTTPException(status_code=400, detail=f"{moon_name!r} is not a satellite of {parent.name}")
            plan = moon_transfer.compute_direct_transfer_plan(client, vessel, parent, moon)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        sc = client.space_center
        periapsis_angle_deg = math.degrees(math.atan2(plan["periapsis_hat"][2], plan["periapsis_hat"][0]))
        return {
            "moon_name": moon.name,
            "periapsis_angle_deg": periapsis_angle_deg,
            "r_peri_m": plan["r_peri"],
            "r_apo_m": plan["target_apoapsis_m"] + parent.equatorial_radius,
            "inclination_deg": math.degrees(vessel.orbit.inclination),
            "moon_orbital_radius_m": moon.orbit.semi_major_axis,
            "burn_in_s": plan["burn_ut"] - sc.ut,
            "arrival_in_s": plan["arrival_ut"] - sc.ut,
        }

    @router.get("/{vessel_id}/planet-transfer/preview")
    def preview_planet_transfer(vessel_id: str, target_body_name: str):
        """Read-only cost/timing preview for an interplanetary transfer, so
        the dashboard can show what the window will cost before committing.

        This is what replaced pasting a plan in from KSP-MGA-Planner: the
        numbers come from this project's own math now (see
        backend/autopilots/planet_transfer.py)."""
        vessel = get_vessel_or_404(vessel_id)
        try:
            origin, star = planet_transfer._planet_of(vessel)
            target = next((b for b in star.satellites if b.name == target_body_name), None)
            if target is None:
                raise HTTPException(
                    status_code=400, detail=f"{target_body_name!r} is not a planet orbiting {star.name}",
                )
            if target == origin:
                raise HTTPException(status_code=400, detail=f"already at {origin.name}")
            plan = planet_transfer.compute_transfer_plan(client, vessel, origin, target)
        except NotConnected as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return {
            "origin_name": origin.name,
            "target_name": target.name,
            "wait_s": plan["wait_s"],
            "transfer_time_s": plan["transfer_time_s"],
            "ejection_dv": plan["ejection_dv"],
            "v_infinity": plan["v_infinity"],
            "phase_angle_deg": math.degrees(plan["required_phase_rad"]),
            "ejection_angle_deg": math.degrees(plan["ejection_angle_rad"]),
        }

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
