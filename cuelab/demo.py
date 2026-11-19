"""End-to-end demo: upload -> queue -> workers -> tracklist -> validated Live365 export.

    python -m cuelab.demo --out examples
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading

from . import live365
from .assemble import assemble
from .ratelimit import SharedRateLimiter
from .recognizer import SimulatedRecognizer
from .simulate import make_show
from .store import JobStore
from .uploads import save_upload
from .worker import cleanup_on_finish, run_worker


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="examples")
    p.add_argument("--minutes", type=float, default=58)
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "demo.db")
    store = JobStore(db, on_show_finished=cleanup_on_finish)
    limiter = SharedRateLimiter(db, "simulated", 100, capacity=100)

    duration = a.minutes * 60
    manifest = make_show(duration, seed=42)
    show_id = "demo58min"
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 4096)  # stands in for the uploaded show
    upload = save_upload(fake_mp3, os.path.join(tmp, "uploads"), show_id)
    store.enqueue_show(
        "kdlt", "Friday Night Northern Mix", duration, show_id=show_id, upload_path=upload.path, sim_manifest=manifest
    )

    rec = SimulatedRecognizer(store.sim_manifest, transient_rate=0.1)
    threads = [
        threading.Thread(
            target=run_worker, args=(store, rec, limiter, f"w{i}"), kwargs={"exit_when_idle": True, "base_backoff_s": 0.01}
        )
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = assemble(store.show_results(show_id), host="DJ Aurora")
    entries, notes = live365.fit_marker_limits(entries)
    csv_text = live365.export_csv(entries, duration)
    cue_text = live365.export_cue(
        entries, performer="KDLT", title="Friday Night Northern Mix", filename="friday-night.mp3", duration_s=duration
    )
    std_cue = live365.export_standard_cue(entries, performer="KDLT", title="Friday Night Northern Mix", filename="friday-night.mp3")
    for name, text in [("live365.csv", csv_text), ("live365.cue", cue_text), ("standard.cue", std_cue)]:
        with open(os.path.join(a.out, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    summary = {
        "show": dict(store.show(show_id)),
        "upload_deleted": not os.path.exists(upload.path),
        "metered_hours": round(store.usage_hours("kdlt"), 3),
        "tracks": [
            {k: getattr(e, k) for k in ("offset_s", "media_type", "title", "artist", "album", "label", "isrc")}
            for e in entries
        ],
        "marker_fit_notes": notes,
        "live365_warnings": live365.validate_csv(csv_text, duration).warnings,
    }
    summary["show"].pop("sim_manifest", None)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ("upload_deleted", "metered_hours", "marker_fit_notes")}, indent=2))
    print(csv_text)


if __name__ == "__main__":
    main()
