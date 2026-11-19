"""Load test: several stations uploading at once, one shared rate limit.

Measures, per station, how long until its first result and until its last
show finishes. With fair scheduling, a station that uploads one short show
gets it back quickly even while another station has hours queued.

    python -m cuelab.loadtest --workers 8 --rate 40
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time

from .ratelimit import SharedRateLimiter
from .recognizer import SimulatedRecognizer
from .simulate import make_show
from .store import JobStore
from .worker import run_worker

# station -> list of show durations (seconds)
DEFAULT_MIX = {
    "big-station": [4 * 3600, 4 * 3600],  # one station drops 8 hours of audio
    "kdlt": [3600],
    "kwbx": [2 * 3600],
    "hobbyist-1": [1800],
    "hobbyist-2": [1800],
}


def run(
    *,
    workers: int = 8,
    rate: float = 40.0,
    latency_s: float = 0.01,
    transient_rate: float = 0.05,
    step_s: float = 30.0,
    mix: dict | None = None,
    db_path: str | None = None,
) -> dict:
    mix = mix or DEFAULT_MIX
    tmp = None
    if db_path is None:
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "load.db")
    store = JobStore(db_path, max_attempts=6)
    limiter = SharedRateLimiter(db_path, "provider", rate, capacity=rate)
    rec = SimulatedRecognizer(store.sim_manifest, latency_s=latency_s, transient_rate=transient_rate)

    show_station = {}
    seed = 0
    # The big upload is queued FIRST: the worst case for a naive FIFO queue.
    for station, durations in mix.items():
        for d in durations:
            seed += 1
            sid = store.enqueue_show(station, f"{station} show {seed}", d, step_s=step_s, sim_manifest=make_show(d, seed))
            show_station[sid] = station

    first_result: dict[str, float] = {}
    finished: dict[str, float] = {}
    lock = threading.Lock()
    t0 = time.time()

    def watch(stop: threading.Event):
        while not stop.is_set():
            for sid, station in show_station.items():
                row = store.show(sid)
                p = store.progress(sid)
                with lock:
                    if p["finished"] and station not in first_result:
                        first_result[station] = time.time() - t0
                    if row["status"] in ("done", "needs_review"):
                        finished.setdefault(sid, time.time() - t0)
            time.sleep(0.02)

    stop = threading.Event()
    watcher = threading.Thread(target=watch, args=(stop,), daemon=True)
    watcher.start()
    threads = [
        threading.Thread(
            target=run_worker,
            args=(store, rec, limiter, f"w{i}"),
            kwargs={"exit_when_idle": True, "base_backoff_s": 0.02, "idle_sleep_s": 0.01},
        )
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.1)
    stop.set()
    watcher.join()
    elapsed = time.time() - t0

    calls = store.query("SELECT outcome, COUNT(*) n FROM call_log GROUP BY outcome")
    per_station = {}
    for station in mix:
        sids = [s for s, st in show_station.items() if st == station]
        per_station[station] = {
            "audio_hours": round(sum(mix[station]) / 3600, 2),
            "first_result_s": round(first_result.get(station, float("nan")), 2),
            "all_shows_done_s": round(max(finished.get(s, float("nan")) for s in sids), 2),
            "metered_hours": round(store.usage_hours(station), 2),
        }
    total_segments = store.query("SELECT COUNT(*) FROM segments")[0][0]
    return {
        "workers": workers,
        "rate_limit_per_s": rate,
        "elapsed_s": round(elapsed, 2),
        "segments": total_segments,
        "calls": {r["outcome"]: r["n"] for r in calls},
        "observed_call_rate_per_s": round(sum(r["n"] for r in calls) / elapsed, 1),
        "statuses": {r["status"]: r["n"] for r in store.query("SELECT status, COUNT(*) n FROM shows GROUP BY status")},
        "stations": per_station,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--rate", type=float, default=40.0)
    p.add_argument("--latency", type=float, default=0.01)
    p.add_argument("--transient-rate", type=float, default=0.05)
    a = p.parse_args()
    print(json.dumps(run(workers=a.workers, rate=a.rate, latency_s=a.latency, transient_rate=a.transient_rate), indent=2))


if __name__ == "__main__":
    main()
