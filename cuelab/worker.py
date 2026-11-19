"""Background worker.

Loop: claim the next fair segment -> wait for the shared rate limiter ->
recognise -> record the result. Transient provider errors are retried with
backoff; a result from a worker whose lease was taken over is discarded.

Run as a process (several can share one database):

    python -m cuelab.worker --db engine.db --id w1
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Optional

from .ratelimit import SharedRateLimiter
from .recognizer import Recognizer, SimulatedRecognizer, TransientError
from .store import JobStore, StaleLease
from .uploads import delete_upload

log = logging.getLogger("cuelab.worker")


def run_worker(
    store: JobStore,
    recognizer: Recognizer,
    limiter: Optional[SharedRateLimiter],
    worker_id: str,
    *,
    stop: Optional[threading.Event] = None,
    lease_s: float = 60.0,
    idle_sleep_s: float = 0.05,
    exit_when_idle: bool = False,
    base_backoff_s: float = 1.0,
) -> int:
    """Process segments until stopped. Returns the number of segments completed."""
    stop = stop or threading.Event()
    completed = 0
    while not stop.is_set():
        claim = store.claim_next(worker_id, lease_s)
        if claim is None:
            if exit_when_idle and not _has_open_work(store):
                break
            time.sleep(idle_sleep_s)
            continue
        if limiter:
            limiter.acquire()
        try:
            match = recognizer.recognize(claim.show_id, claim.start_s, claim.end_s)
        except TransientError as e:
            store.log_call(claim, "transient")
            try:
                outcome = store.fail(claim, str(e), base_backoff_s=base_backoff_s)
                log.info("segment %s attempt %s failed (%s): %s", claim.segment_id, claim.attempt, e, outcome)
            except StaleLease:
                pass
            continue
        except Exception as e:  # unexpected: never retry forever, but don't crash the worker
            store.log_call(claim, "error")
            log.exception("segment %s crashed", claim.segment_id)
            try:
                store.fail(claim, f"{type(e).__name__}: {e}", base_backoff_s=base_backoff_s)
            except StaleLease:
                pass
            continue
        store.log_call(claim, "ok")
        try:
            store.complete(claim, match.to_dict() if match else None)
            completed += 1
        except StaleLease:
            log.warning("segment %s: lease taken over, result discarded", claim.segment_id)
    return completed


def _has_open_work(store: JobStore) -> bool:
    return bool(store.query("SELECT 1 FROM segments WHERE status IN ('pending','leased') LIMIT 1"))


def cleanup_on_finish(show_id: str, status: str, upload_path: Optional[str]) -> None:
    delete_upload(upload_path)


def main() -> None:
    p = argparse.ArgumentParser(description="cue sheet engine worker (simulated recognition)")
    p.add_argument("--db", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--lease", type=float, default=60.0)
    p.add_argument("--latency", type=float, default=0.0, help="simulated recognition latency (s)")
    p.add_argument("--rate", type=float, default=50.0, help="shared recognition calls per second")
    p.add_argument("--exit-when-idle", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    store = JobStore(a.db, on_show_finished=cleanup_on_finish)
    limiter = SharedRateLimiter(a.db, "simulated", a.rate, capacity=max(1.0, a.rate))
    rec = SimulatedRecognizer(store.sim_manifest, latency_s=a.latency)
    n = run_worker(store, rec, limiter, a.id, lease_s=a.lease, exit_when_idle=a.exit_when_idle)
    log.info("worker %s exiting after %d segments", a.id, n)


if __name__ == "__main__":
    main()
