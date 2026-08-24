import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ws")

router = APIRouter()

TICK_SECONDS = 0.5


def build_router(registry, jobs):
    @router.websocket("/ws/telemetry")
    async def telemetry_stream(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                tick_started = time.monotonic()

                vessels = await asyncio.to_thread(registry.list_vessels)
                for vessel in vessels:
                    job = jobs.get(vessel["id"])
                    vessel["autopilot"] = job.to_dict() if job else None

                await websocket.send_json({
                    "krpc_connected": registry.is_connected,
                    "vessels": vessels,
                })

                # Sleep for whatever is left of the tick rather than a flat
                # TICK_SECONDS after the work. Gathering telemetry for a
                # busy save can take longer than the tick itself, and
                # always sleeping the full interval on top of that made the
                # dashboard progressively laggier the more craft were in
                # flight. If a tick overruns, the next one starts
                # immediately instead of compounding the delay.
                elapsed = time.monotonic() - tick_started
                await asyncio.sleep(max(0.0, TICK_SECONDS - elapsed))
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("telemetry stream error: %s", exc)

    return router
