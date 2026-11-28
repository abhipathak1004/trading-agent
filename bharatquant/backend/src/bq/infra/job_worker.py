"""Bounded execution of durable ``JobRun`` rows.

The scheduler only enqueues occurrences. This worker claims one run at a time,
dispatches it to a code-owned handler, and finishes the run with the store's
lease/fencing contract. It never invents a second queue or bypasses the lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import timedelta

from bq.infra.jobs import JobRun, JobRunConflict, JobRunStore

JobHandler = Callable[[JobRun], object]


class JobWorker:
    def __init__(
        self,
        store: JobRunStore,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str = "local-worker",
        lease_for: timedelta = timedelta(minutes=2),
    ) -> None:
        if not handlers:
            raise ValueError("At least one job handler is required.")
        if lease_for <= timedelta(0):
            raise ValueError("Job lease must be positive.")
        self.store = store
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.lease_for = lease_for

    def run_once(self) -> JobRun | None:
        run = self.store.claim_next(self.worker_id, lease_for=self.lease_for)
        if run is None:
            return None
        if run.lease_token is None:
            raise JobRunConflict("Claimed job has no lease token.")
        handler = self.handlers.get(run.job_id)
        if handler is None:
            return self.store.fail(
                run.run_id,
                self.worker_id,
                "handler_missing",
                lease_token=run.lease_token,
            )
        try:
            handler(run)
        except Exception:  # noqa: BLE001 - a handler failure is a failed run
            return self.store.fail(
                run.run_id,
                self.worker_id,
                "handler_error",
                lease_token=run.lease_token,
            )
        return self.store.succeed(
            run.run_id, self.worker_id, lease_token=run.lease_token
        )

    def run_until_idle(self, *, max_jobs: int = 20) -> int:
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs < 1:
            raise ValueError("Max jobs must be a positive integer.")
        completed = 0
        for _ in range(max_jobs):
            try:
                run = self.run_once()
            except JobRunConflict:
                break
            if run is None:
                break
            completed += 1
        return completed


class JobWorkerLoop:
    """Small asyncio process loop for the synchronous worker."""

    def __init__(
        self,
        worker: JobWorker,
        *,
        interval: timedelta = timedelta(seconds=5),
        max_jobs_per_tick: int = 20,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("Worker loop interval must be positive.")
        self.worker = worker
        self.interval = interval
        self.max_jobs_per_tick = max_jobs_per_tick
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self.last_completed: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop = None

    async def _run(self) -> None:
        stop = self._stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                self.last_completed = await asyncio.to_thread(
                    self.worker.run_until_idle,
                    max_jobs=self.max_jobs_per_tick,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry on the next tick
                self.last_completed = None
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.interval.total_seconds()
                )
            except TimeoutError:
                continue


__all__ = ["JobHandler", "JobWorker", "JobWorkerLoop"]
