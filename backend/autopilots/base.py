"""Common autopilot job runner: background thread, status, abort.

Only one autopilot job can run per vessel at a time -- starting a new one
for the same vessel id replaces (aborts) the old one.
"""

import logging
import threading
import time
import traceback

logger = logging.getLogger("autopilot")


class AbortRequested(Exception):
    """Raised inside a job's run function to unwind cleanly on abort()."""


class AutopilotJob:
    def __init__(self, vessel_id: str, kind: str, target_fn, params: dict):
        self.vessel_id = vessel_id
        self.kind = kind
        self.params = params
        self.status = "pending"  # pending, running, done, error, aborted
        self.message = ""
        self._abort_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target_fn,), daemon=True)

    def start(self):
        self.status = "running"
        self._thread.start()

    def _run(self, target_fn):
        try:
            target_fn(self)
            if self.status == "running":
                self.status = "done"
                # Preserve whatever specific final message the autopilot
                # itself set (e.g. "landed", "burnup", "deployed to
                # constellation") -- this used to always get stomped with
                # a generic "completed", which silently hid real outcomes
                # like "did nothing, no fuel left" behind a success-sounding
                # status.
                if not self.message:
                    self.message = "completed"
        except AbortRequested:
            self.status = "aborted"
            self.message = "aborted by user"
        except Exception as exc:
            logger.error("Autopilot job %s (%s) failed: %s", self.kind, self.vessel_id, exc)
            logger.debug(traceback.format_exc())
            self.status = "error"
            self.message = str(exc)

    def check_abort(self):
        if self._abort_event.is_set():
            raise AbortRequested()

    def sleep(self, seconds):
        """Sleep in small increments so abort() is responsive."""
        end = time.time() + seconds
        while time.time() < end:
            self.check_abort()
            time.sleep(min(0.1, end - time.time()))

    def abort(self):
        self._abort_event.set()

    def to_dict(self):
        return {
            "vessel_id": self.vessel_id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "params": self.params,
        }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, AutopilotJob] = {}
        self._lock = threading.Lock()

    def start(self, vessel_id: str, kind: str, target_fn, params: dict) -> AutopilotJob:
        with self._lock:
            existing = self._jobs.get(vessel_id)
            if existing is not None and existing.status == "running":
                existing.abort()
            job = AutopilotJob(vessel_id, kind, target_fn, params)
            self._jobs[vessel_id] = job
            job.start()
            return job

    def get(self, vessel_id: str):
        return self._jobs.get(vessel_id)

    def abort(self, vessel_id: str) -> bool:
        job = self._jobs.get(vessel_id)
        if job is None or job.status != "running":
            return False
        job.abort()
        return True
