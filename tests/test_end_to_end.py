"""Whole pipeline, including killing a real worker process mid-show."""
import io
import os
import signal
import subprocess
import sys
import threading
import time

from cuelab import live365
from cuelab.assemble import assemble
from cuelab.loadtest import run as run_load
from cuelab.ratelimit import SharedRateLimiter
from cuelab.recognizer import SimulatedRecognizer
from cuelab.simulate import make_show
from cuelab.store import JobStore
from cuelab.uploads import save_upload
from cuelab.worker import cleanup_on_finish, run_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_pipeline_produces_validated_live365_files_and_deletes_audio(tmp_path):
    db = str(tmp_path / "e.db")
    store = JobStore(db, on_show_finished=cleanup_on_finish)
    duration = 45 * 60
    manifest = make_show(duration, seed=3)
    up = save_upload(io.BytesIO(b"ID3" + b"\0" * 2048), str(tmp_path / "up"), "show1")
    store.enqueue_show("kdlt", "Mix", duration, show_id="show1", upload_path=up.path, sim_manifest=manifest)
    rec = SimulatedRecognizer(store.sim_manifest, transient_rate=0.2, seed=11)
    limiter = SharedRateLimiter(db, "sim", 500, 50)
    threads = [
        threading.Thread(target=run_worker, args=(store, rec, limiter, f"w{i}"),
                         kwargs={"exit_when_idle": True, "base_backoff_s": 0.001})
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.show("show1")["status"] in ("done", "needs_review")
    assert not os.path.exists(up.path)
    entries, _ = live365.fit_marker_limits(assemble(store.show_results("show1")))
    csv_text = live365.export_csv(entries, duration)
    cue_text = live365.export_cue(entries, performer="KDLT", title="Mix", filename="mix.mp3", duration_s=duration)
    assert live365.validate_csv(csv_text, duration).ok and live365.validate_cue(cue_text, duration).ok

    # Every song in the manifest that lasted long enough appears with its metadata.
    songs = {(m["title"], m["artist"], m["isrc"]) for m in manifest if m["kind"] == "music" and m["end"] - m["start"] > 90}
    found = {(e.title, e.artist, e.isrc) for e in entries if e.media_type == "music"}
    assert songs <= found
    assert all(e.album and e.label and e.isrc for e in entries if e.media_type == "music")


def test_killed_worker_process_resumes_without_redoing_finished_work(tmp_path):
    db = str(tmp_path / "crash.db")
    store = JobStore(db)
    duration = 60 * 60
    sid = store.enqueue_show("kdlt", "1h show", duration, step_s=30, sim_manifest=make_show(duration, 5))  # 120 windows

    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cuelab.worker", "--db", db, "--id", "victim", "--lease", "2", "--latency", "0.05"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while store.progress(sid)["finished"] < 20 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        proc.send_signal(signal.SIGKILL)  # no cleanup, no graceful shutdown
        proc.wait()

    done_before = {r["segment_id"] for r in store.query("SELECT segment_id FROM results")}
    stuck = store.query("SELECT id FROM segments WHERE status='leased' AND lease_owner='victim'")
    assert 20 <= len(done_before) < 120
    # Almost always it dies mid-recognition holding one lease; occasionally the
    # kill lands between two windows. Both are real crashes and both must resume.
    assert len(stuck) <= 1

    time.sleep(2.1)  # let that lease expire
    rec = SimulatedRecognizer(store.sim_manifest)
    run_worker(store, rec, None, "rescuer", exit_when_idle=True)

    assert store.show(sid)["status"] == "done"
    assert store.query("SELECT COUNT(*) FROM results")[0][0] == 120
    # The rescuer never re-recognised what the victim had already finished...
    redone = store.query(
        "SELECT COUNT(*) FROM call_log WHERE worker_id='rescuer' AND segment_id IN (%s)"
        % ",".join(str(i) for i in done_before)
    )[0][0]
    assert redone == 0
    # ...and picked up the window that was in flight when it died.
    if stuck:
        rescued = store.query("SELECT attempts FROM segments WHERE id=?", (stuck[0]["id"],))[0][0]
        assert rescued == 2


def test_load_fairness_and_rate_limit_under_pressure():
    report = run_load(
        workers=6,
        rate=150.0,
        latency_s=0.0,
        transient_rate=0.05,
        step_s=60.0,
        mix={"big": [4 * 3600, 4 * 3600], "kdlt": [1800], "kwbx": [3600], "hobby": [900]},
    )
    st = report["stations"]
    # Small shows finish long before the 8 hours of "big" audio queued ahead of them.
    # (Exact turn-taking is proven deterministically in test_fairness.py; wall-clock
    # here includes retry backoff, so the bound is deliberately loose.)
    assert st["hobby"]["all_shows_done_s"] < st["big"]["all_shows_done_s"] / 2
    assert st["kdlt"]["all_shows_done_s"] < st["big"]["all_shows_done_s"] * 0.75
    # Shared limit held (burst capacity == rate, so allow one extra second of tokens).
    calls = sum(report["calls"].values())
    assert calls <= 150.0 * (report["elapsed_s"] + 1) + 5
    assert report["statuses"].get("done", 0) + report["statuses"].get("needs_review", 0) == 5
    assert st["big"]["metered_hours"] == 8.0
