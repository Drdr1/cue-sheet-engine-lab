# cue-sheet-engine-lab

A multi-tenant background engine that turns long radio show recordings into cue sheets, with a Live365 MultiTrack exporter.

It's built around the parts that break when a desktop cue sheet tool moves to a server:

- **Resume after a crash.** A 4-hour show must survive a server restart without being recognised from the start again.
- **Fairness.** One station's huge upload must not block every other station.
- **Shared limits.** Every station draws on the same ACRCloud/Shazam rate limit.
- **Strict exports.** Live365 rejects cue sheets that break its rules.

```
upload ──► temp storage (0600, sha256) ──► show split into sample windows
                                              │
            ┌──── fair scheduler (round robin across stations) ◄──┘
            ▼
   worker pool ──► shared token bucket ──► recognizer (ACRCloud / Shazam / simulated)
       │  lease + fencing token                   │ transient error → backoff retry
       ▼                                          ▼ out of retries → flagged for review
   results ──► tracklist assembly ──► Live365 .csv / .cue (validated) ──► download
       └──► show finished → audio deleted, hours metered per station
```

## What it does

| Area | How |
|---|---|
| Resume and retry | Each sample window is a row with its own state. Workers *lease* a window. If a worker dies, the lease expires and another worker takes over. Finished windows are never recognised again. Transient errors (timeouts, 429s) retry with exponential backoff. After `max_attempts`, the window is marked dead and the show is flagged `needs_review`, so it never loops forever. |
| No double writes | Every lease carries a fencing token. A slow worker whose lease was taken over can't overwrite the newer result. |
| Fair scheduling | The next window always comes from the station served least recently. Within a station, shows run in upload order. A 10-minute show is done within its first ~40 claims, even with a 4-hour upload queued ahead of it. |
| Shared rate limit | One token bucket per provider, stored in the database. All threads and all worker processes on the server share one budget. |
| Usage metering | Hours of audio are recorded once per finished show, per station (the basis for plan-hour limits). |
| Upload pipeline | Uploads are streamed to disk in 1 MB chunks. Each show gets its own `0700` directory and the file is `0600`. The pipeline checks the MP3 signature, enforces a size cap, and computes a SHA-256. Audio is deleted when the show finishes, whether it's done or needs review. |
| Track metadata | Title, artist, album, record label and ISRC are kept for every identified track. |
| Live365 preset | `.000` timing (`HH:MM:SS.mmm`) in both `.csv` and `.cue`. The header is exactly `offset,media_type,title,artist,album,year` and `media_type` is lowercase. Duplicate timestamps are rejected. Marker limits are checked **cumulatively**: at most 5 in the first 10 minutes, 7 in 15, 13 in 30, and 33 in 78. Every file is parsed back and validated before it's offered for download. |
| Marker fitting (optional) | When a show breaks a marker limit, the shortest talk/ID markers are removed first. Music markers are never removed, because every song needs its own artist and title for reporting. |
| Standard .cue | A classic `MM:SS:FF` (75 frames per second) cue sheet with `ISRC` lines, for other playout systems. |

## Run it

```bash
python -m pytest -q                       # 44 tests, ~13 s, no dependencies beyond pytest
python -m cuelab.demo --out examples      # one show end to end → examples/
python -m cuelab.loadtest --workers 8 --rate 60
python -m cuelab.worker --db engine.db --id w1   # a standalone worker process
```

Sample output from the demo is in [`examples/`](examples): `live365.csv`, `live365.cue`, `standard.cue` and `summary.json`.

## Load test result

8 workers. Five stations upload at the same time, and the biggest upload is queued **first**, which is the worst case for a plain FIFO queue. Shared limit: 60 recognition calls/s. 5% of calls fail with simulated 429s and are retried.

| Station | Audio queued | Show(s) finished after |
|---|---|---|
| hobbyist-2 | 0.5 h | 4.0 s |
| hobbyist-1 | 0.5 h | 4.5 s |
| kdlt | 1 h | 7.5 s |
| kwbx | 2 h | 12.4 s |
| big-station | 8 h | 24.5 s |

That's 1,440 windows plus 89 retries. All 6 shows finished, and metered hours match the audio exactly. The observed rate was 62.1 calls/s, which is the 60/s limit plus the bucket's initial burst. In a FIFO queue, the big station's 960 windows would go first, and the hobbyists' shows wouldn't even *start* for about 16 seconds.

## Tests that matter

- `test_killed_worker_process_resumes_without_redoing_finished_work`: starts a real worker process and `SIGKILL`s it partway through a 1-hour show. A second worker finishes the show. The test proves that **zero** finished windows were recognised again, and that the window in flight at the time of the kill was picked up.
- `test_stale_worker_cannot_overwrite_newer_result`: fencing tokens.
- `test_small_show_is_not_stuck_behind_four_hour_upload` and `test_three_stations_take_turns`: deterministic fairness.
- `test_processes_share_one_budget`: four OS processes together stay within one rate limit.
- `test_marker_limits_are_cumulative`: a 78-minute show with 6 markers in its first 10 minutes is rejected.
- `test_pipeline_produces_validated_live365_files_and_deletes_audio`: the whole pipeline with a 20% transient failure rate. Every song comes out with album, label and ISRC, and the audio file is gone afterwards.

## What is simulated (read this)

- **Recognition is simulated.** `SimulatedRecognizer` answers from a show manifest (what plays when), with configurable latency and injected transient failures. That's what makes resume, fairness and retries testable in CI. `ACRCloudRecognizer` shows the real adapter: signature v1 request signing, mapping of retryable status codes, and mapping of title/artist/album/label/ISRC. **It has not been run against the live ACRCloud API here.** A Shazam adapter would plug into the same `Recognizer` interface. Keys come from server environment variables only.
- **No real audio is decoded.** In production, each window is a short ffmpeg cut of the uploaded MP3, passed to the provider.
- **SQLite stands in for Postgres.** The store runs in WAL mode with `BEGIN IMMEDIATE`. The claim / fence / complete logic maps directly onto Postgres `SELECT … FOR UPDATE SKIP LOCKED` for a multi-server deployment.
- **Live365 rules** come from Live365's *MultiTrack Cue Sheet and CSV file requirements* help article. Two things aren't specified there, so they're flagged rather than guessed:
  - The article lists no marker limit for files longer than 78 minutes. Long shows get a warning, not a made-up limit.
  - In the `.cue`, non-music entries carry a standard `REM MEDIA_TYPE` comment, which cue parsers ignore. Confirm against a real Live365 upload.
- Station IDs and jingles show up as talk segments. Jingle recognition is a separate feature.

## Layout

```
cuelab/store.py        durable job store: leases, fencing, retries, fair claim, metering
cuelab/ratelimit.py    shared token bucket (cross-process)
cuelab/worker.py       worker loop + standalone worker process
cuelab/recognizer.py   Recognizer interface, simulated provider, ACRCloud adapter
cuelab/assemble.py     sample windows → tracklist (merge, talk, false positives, review gaps)
cuelab/live365.py      Live365 .csv/.cue export, validation, marker fitting; standard .cue
cuelab/uploads.py      streamed private temp storage, deleted after processing
cuelab/loadtest.py     multi-station load test
cuelab/demo.py         end-to-end demo
```

MIT licensed. Standard library only at runtime. Tested on Python 3.11 to 3.13.
