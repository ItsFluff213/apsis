import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ws")

router = APIRouter()


def build_router(registry, jobs):
    @router.websocket("/ws/telemetry")
    async def telemetry_stream(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                vessels = await asyncio.to_thread(registry.list_vessels)
                for v in vessels:
                    job = jobs.get(v["id"])
                    v["autopilot"] = job.to_dict() if job else None
                await websocket.send_json({"krpc_connected": registry._client.is_connected, "vessels": vessels})
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("telemetry stream error: %s", exc)

    return router
