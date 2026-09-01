"""In-process background job manager used by the phone MCP server."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .adb import Phone
from .game_macro import MacroRunner


@dataclass
class GameJob:
    job_id: str
    name: str
    state: str = "starting"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    actions_completed: int = 0
    result: dict[str, Any] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "actions_completed": self.actions_completed,
            "result": self.result,
        }


class GameJobManager:
    """Run at most one input-producing game plan at a time."""

    def __init__(self, phone_factory: Callable[[], Phone] = Phone):
        self.phone_factory = phone_factory
        self._jobs: dict[str, GameJob] = {}
        self._active_id: str | None = None
        self._lock = threading.Lock()

    def start(
        self, plan: dict[str, Any], *, max_seconds: float | None = None, seed: int | None = None
    ) -> dict[str, Any]:
        with self._lock:
            if self._active_id:
                active = self._jobs[self._active_id]
                if active.state in {"starting", "running", "stopping"}:
                    raise RuntimeError(f"game job {active.job_id} is already {active.state}")
            job = GameJob(job_id=uuid.uuid4().hex[:12], name=plan["name"])
            self._jobs[job.job_id] = job
            self._active_id = job.job_id
        thread = threading.Thread(
            target=self._run,
            args=(job, plan, max_seconds, seed),
            name=f"game-job-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return job.public()

    def _run(
        self,
        job: GameJob,
        plan: dict[str, Any],
        max_seconds: float | None,
        seed: int | None,
    ) -> None:
        def progress(event: dict[str, Any]) -> None:
            with self._lock:
                job.updated_at = time.time()
                if event.get("event") == "action":
                    job.actions_completed = int(event["completed"])

        with self._lock:
            job.state = "running"
            job.updated_at = time.time()
        runner = MacroRunner(
            self.phone_factory(),
            stop_event=job.stop_event,
            max_seconds=max_seconds,
            seed=seed,
            progress=progress,
        )
        result = runner.run(plan).to_dict()
        with self._lock:
            job.result = result
            job.actions_completed = result["actions_completed"]
            job.state = result["status"]
            job.updated_at = time.time()
            if self._active_id == job.job_id:
                self._active_id = None

    def status(self, job_id: str = "") -> dict[str, Any]:
        with self._lock:
            resolved = job_id or self._active_id
            if not resolved:
                if not self._jobs:
                    return {"state": "idle"}
                resolved = next(reversed(self._jobs))
            if resolved not in self._jobs:
                raise KeyError(f"unknown game job {resolved!r}")
            return self._jobs[resolved].public()

    def stop(self, job_id: str = "") -> dict[str, Any]:
        with self._lock:
            resolved = job_id or self._active_id
            if not resolved or resolved not in self._jobs:
                raise KeyError(f"unknown game job {resolved!r}")
            job = self._jobs[resolved]
            if job.state not in {"starting", "running", "stopping"}:
                return job.public()
            job.state = "stopping"
            job.updated_at = time.time()
            job.stop_event.set()
            return job.public()
