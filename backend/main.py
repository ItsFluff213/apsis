import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import db
from backend.api import (
    routes_autopilot,
    routes_constellations,
    routes_parts,
    routes_profile,
    routes_system,
    routes_vessels,
    routes_waypoints,
    ws,
)
from backend.autopilots.base import JobManager
from backend.krpc_client import KRPCClient
from backend.paths import BUNDLE_DIR
from backend.vessel_registry import VesselRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

FRONTEND_DIR = BUNDLE_DIR / "frontend"

db.init_db()
client = KRPCClient()
registry = VesselRegistry(client)
jobs = JobManager()


@asynccontextmanager
async def lifespan(app):
    # Replaces the deprecated @app.on_event("startup") hook, which current
    # FastAPI warns about and will eventually drop.
    logger.info("Starting kRPC connection watchdog (will keep retrying until KSP + kRPC server are up)...")
    client.connect_in_background()
    yield


app = FastAPI(title="KSP Autonomous Fleet Control", lifespan=lifespan)


app.include_router(routes_vessels.build_router(registry))
app.include_router(routes_autopilot.build_router(client, registry, jobs))
app.include_router(routes_parts.build_router(registry))
app.include_router(routes_waypoints.build_router(client))
app.include_router(routes_system.build_router(client))
app.include_router(routes_constellations.build_router(client, registry, jobs))
app.include_router(routes_profile.build_router())
app.include_router(ws.build_router(registry, jobs))

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import sys
    import threading
    import webbrowser

    import uvicorn

    # When double-clicked as a packaged exe there's no terminal history to
    # scroll back through and no way to know it worked besides the console
    # window it opens -- open the dashboard automatically and say so
    # plainly, rather than leaving a first-time user staring at log lines.
    if getattr(sys, "frozen", False):
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
        print("Apsis is starting -- opening the dashboard in your browser...")
        print("Leave this window open while you use the dashboard. Close it to stop Apsis.")

    # Pass the app object directly (not the "backend.main:app" import string).
    # Passing a string makes uvicorn re-import this module a second time,
    # which -- since `python -m backend.main` already executed it once as
    # __main__ -- built a second, never-connected KRPCClient/registry whose
    # router handlers silently shadowed the real ones. Passing the object
    # avoids the double import entirely.
    uvicorn.run(app, host="0.0.0.0", port=8000)
