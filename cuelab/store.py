"""Durable job store for show processing.

A show is split into fixed sample windows ("segments"). Each segment is one
recognition call and one row in the store, with its own state:

    pending -> leased -> done
                 |  \
                 |   -> pending (transient failure, retried after backoff)
                 -> dead (gave up after max_attempts; flagged for review)

Workers *lease* a segment for a limited time. If a worker crashes or the
server restarts, the lease runs out and another worker picks the segment up.
Finished segments are never recognised again, so a restart resumes a show
instead of starting it over.

Every lease carries a fencing token. A worker whose lease was taken over
cannot overwrite the newer worker's result: its completion is rejected.

Fair scheduling: when choosing the next segment, the store picks the station
that was served least recently among stations with claimable work, then the
oldest segment of that station. One station's 4-hour upload therefore takes
turns with everyone else instead of blocking them.

SQLite (WAL mode) keeps the lab self-contained and CI-friendly. The same
claim/complete/fence logic maps directly onto Postgres
(SELECT ... FOR UPDATE SKIP LOCKED) for production.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id          TEXT PRIMARY KEY,
    served_seq  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shows (
    id            TEXT PRIMARY KEY,
    station_id    TEXT NOT NULL REFERENCES stations(id),
    title         TEXT NOT NULL,
    duration_s    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    upload_path   TEXT,
    sim_manifest  TEXT,
    created_seq   INTEGER NOT NULL,
    finished_at   REAL
);
CREATE TABLE IF NOT EXISTS segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id      TEXT NOT NULL REFERENCES shows(id),
    idx          INTEGER NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    fence        INTEGER NOT NULL DEFAULT 0,
    lease_owner  TEXT,
    lease_until  REAL,
    not_before   REAL NOT NULL DEFAULT 0,
    last_error   TEXT,
    UNIQUE (show_id, idx)
);
CREATE INDEX IF NOT EXISTS seg_claim ON segments (status, not_before);
CREATE INDEX IF NOT EXISTS seg_show ON segments (show_id, idx);
CREATE TABLE IF NOT EXISTS results (
    segment_id  INTEGER PRIMARY KEY REFERENCES segments(id),
    show_id     TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    show_id     TEXT PRIMARY KEY,
    station_id  TEXT NOT NULL,
    seconds     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  INTEGER NOT NULL,
    worker_id   TEXT NOT NULL,
    outcome     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);
"""

TERMINAL = ("done", "dead")


class StaleLease(Exception):
    """The lease was taken over by another worker; this result is discarded."""


@dataclass(frozen=True)
class Claim:
    segment_id: int
    show_id: str
    station_id: str
    idx: int
    start_s: float
    end_s: float
    fence: int
    attempt: int
    worker_id: str


class JobStore:
    def __init__(
        self,
        path: str,
        *,
        max_attempts: int = 4,
        clock: Callable[[], float] = time.time,
        on_show_finished: Optional[Callable[[str, str, Optional[str]], None]] = None,
    ):
        self.path = path
        self.max_attempts = max_attempts
        self.clock = clock
        self.on_show_finished = on_show_finished
        self._local = threading.local()
        self._conn().execute("PRAGMA journal_mode=WAL")
        self._conn().executescript(SCHEMA)

    # -- connection handling ------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    class _Tx:
        def __init__(self, conn: sqlite3.Connection):
            self.conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            return False

    def _tx(self) -> "_Tx":
        return JobStore._Tx(self._conn())

    @staticmethod
    def _next(c: sqlite3.Connection, name: str) -> int:
        c.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )
        return c.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    # -- enqueue ------------------------------------------------------------
    def enqueue_show(
        self,
        station_id: str,
        title: str,
        duration_s: float,
        *,
        window_s: float = 12.0,
        step_s: float = 30.0,
        upload_path: Optional[str] = None,
        sim_manifest: Optional[list] = None,
        show_id: Optional[str] = None,
    ) -> str:
        """Register a show and split it into sample windows."""
        if duration_s <= 0:
            raise ValueError("duration must be positive")
        show_id = show_id or uuid.uuid4().hex[:12]
        starts = []
        t = 0.0
        while t < duration_s:
            starts.append(t)
            t += step_s
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO stations(id) VALUES (?)", (station_id,))
            seq = self._next(c, "show_seq")
            c.execute(
                "INSERT INTO shows(id, station_id, title, duration_s, upload_path, sim_manifest, created_seq) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    show_id,
                    station_id,
                    title,
                    duration_s,
                    upload_path,
                    json.dumps(sim_manifest) if sim_manifest is not None else None,
                    seq,
                ),
            )
            c.executemany(
                "INSERT INTO segments(show_id, idx, start_s, end_s) VALUES (?,?,?,?)",
                [(show_id, i, s, min(s + window_s, duration_s)) for i, s in enumerate(starts)],
            )
        return show_id

    # -- claim --------------------------------------------------------------
    def claim_next(self, worker_id: str, lease_s: float = 60.0) -> Optional[Claim]:
        now = self.clock()
        finished: list[tuple[str, str, Optional[str]]] = []
        with self._tx() as c:
            # Expired leases that already used every attempt are given up on.
            dead = c.execute(
                "SELECT id, show_id FROM segments WHERE status='leased' AND lease_until < ? AND attempts >= ?",
                (now, self.max_attempts),
            ).fetchall()
            for row in dead:
                c.execute(
                    "UPDATE segments SET status='dead', lease_owner=NULL, "
                    "last_error=COALESCE(last_error,'lease expired') WHERE id=?",
                    (row["id"],),
                )
            for show_id in {r["show_id"] for r in dead}:
                f = self._maybe_finish(c, show_id, now)
                if f:
                    finished.append(f)

            claimable = (
                "((seg.status='pending' AND seg.not_before <= :now) "
                " OR (seg.status='leased' AND seg.lease_until < :now))"
            )
            station = c.execute(
                f"""
                SELECT sh.station_id AS sid FROM segments seg
                JOIN shows sh ON sh.id = seg.show_id
                JOIN stations st ON st.id = sh.station_id
                WHERE {claimable}
                GROUP BY sh.station_id
                ORDER BY MIN(st.served_seq), sh.station_id
                LIMIT 1
                """,
                {"now": now},
            ).fetchone()
            if station is None:
                claim = None
            else:
                seg = c.execute(
                    f"""
                    SELECT seg.*, sh.station_id FROM segments seg
                    JOIN shows sh ON sh.id = seg.show_id
                    WHERE sh.station_id = :sid AND {claimable}
                    ORDER BY sh.created_seq, seg.idx
                    LIMIT 1
                    """,
                    {"sid": station["sid"], "now": now},
                ).fetchone()
                fence = seg["fence"] + 1
                attempt = seg["attempts"] + 1
                c.execute(
                    "UPDATE segments SET status='leased', lease_owner=?, lease_until=?, "
                    "attempts=?, fence=? WHERE id=?",
                    (worker_id, now + lease_s, attempt, fence, seg["id"]),
                )
                c.execute(
                    "UPDATE stations SET served_seq=? WHERE id=?",
                    (self._next(c, "serve_seq"), seg["station_id"]),
                )
                c.execute(
                    "UPDATE shows SET status='processing' WHERE id=? AND status='queued'",
                    (seg["show_id"],),
                )
                claim = Claim(
                    segment_id=seg["id"],
                    show_id=seg["show_id"],
                    station_id=seg["station_id"],
                    idx=seg["idx"],
                    start_s=seg["start_s"],
                    end_s=seg["end_s"],
                    fence=fence,
                    attempt=attempt,
                    worker_id=worker_id,
                )
        self._notify(finished)
        return claim

    def heartbeat(self, claim: Claim, lease_s: float = 60.0) -> None:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE segments SET lease_until=? WHERE id=? AND fence=? AND status='leased'",
                (self.clock() + lease_s, claim.segment_id, claim.fence),
            )
            if cur.rowcount == 0:
                raise StaleLease(claim.segment_id)

    # -- outcomes -----------------------------------------------------------
    def complete(self, claim: Claim, payload: Optional[dict]) -> None:
        """Record a result. Rejected if another worker took the lease over."""
        now = self.clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE segments SET status='done', lease_owner=NULL, lease_until=NULL "
                "WHERE id=? AND fence=? AND status='leased'",
                (claim.segment_id, claim.fence),
            )
            if cur.rowcount == 0:
                raise StaleLease(claim.segment_id)
            c.execute(
                "INSERT INTO results(segment_id, show_id, worker_id, payload) VALUES (?,?,?,?)",
                (claim.segment_id, claim.show_id, claim.worker_id, json.dumps(payload)),
            )
            finished = self._maybe_finish(c, claim.show_id, now)
        self._notify([finished] if finished else [])

    def fail(self, claim: Claim, error: str, *, base_backoff_s: float = 1.0) -> str:
        """Transient failure: retry later with exponential backoff, or give up."""
        now = self.clock()
        with self._tx() as c:
            seg = c.execute(
                "SELECT attempts, fence, status FROM segments WHERE id=?", (claim.segment_id,)
            ).fetchone()
            if seg["fence"] != claim.fence or seg["status"] != "leased":
                raise StaleLease(claim.segment_id)
            if seg["attempts"] >= self.max_attempts:
                c.execute(
                    "UPDATE segments SET status='dead', lease_owner=NULL, lease_until=NULL, last_error=? WHERE id=?",
                    (error, claim.segment_id),
                )
                outcome = "dead"
            else:
                delay = base_backoff_s * (2 ** (seg["attempts"] - 1))
                c.execute(
                    "UPDATE segments SET status='pending', lease_owner=NULL, lease_until=NULL, "
                    "not_before=?, last_error=? WHERE id=?",
                    (now + delay, error, claim.segment_id),
                )
                outcome = "retry"
            finished = self._maybe_finish(c, claim.show_id, now)
        self._notify([finished] if finished else [])
        return outcome

    def log_call(self, claim: Claim, outcome: str) -> None:
        self._conn().execute(
            "INSERT INTO call_log(segment_id, worker_id, outcome) VALUES (?,?,?)",
            (claim.segment_id, claim.worker_id, outcome),
        )

    def _maybe_finish(self, c: sqlite3.Connection, show_id: str, now: float):
        open_count = c.execute(
            "SELECT COUNT(*) FROM segments WHERE show_id=? AND status NOT IN ('done','dead')",
            (show_id,),
        ).fetchone()[0]
        if open_count:
            return None
        show = c.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
        if show["status"] in ("done", "needs_review"):
            return None
        dead = c.execute(
            "SELECT COUNT(*) FROM segments WHERE show_id=? AND status='dead'", (show_id,)
        ).fetchone()[0]
        status = "needs_review" if dead else "done"
        c.execute("UPDATE shows SET status=?, finished_at=? WHERE id=?", (status, now, show_id))
        # Usage is metered once per show, in hours of audio processed.
        c.execute(
            "INSERT OR IGNORE INTO usage(show_id, station_id, seconds) VALUES (?,?,?)",
            (show_id, show["station_id"], show["duration_s"]),
        )
        return (show_id, status, show["upload_path"])

    def _notify(self, finished: Iterable) -> None:
        if self.on_show_finished:
            for show_id, status, upload_path in finished:
                self.on_show_finished(show_id, status, upload_path)

    # -- queries ------------------------------------------------------------
    def show(self, show_id: str) -> sqlite3.Row:
        return self._conn().execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()

    def show_results(self, show_id: str) -> list[dict]:
        rows = self._conn().execute(
            """
            SELECT seg.idx, seg.start_s, seg.end_s, seg.status, r.payload
            FROM segments seg LEFT JOIN results r ON r.segment_id = seg.id
            WHERE seg.show_id=? ORDER BY seg.idx
            """,
            (show_id,),
        ).fetchall()
        return [
            {
                "idx": r["idx"],
                "start_s": r["start_s"],
                "end_s": r["end_s"],
                "status": r["status"],
                "match": json.loads(r["payload"]) if r["payload"] else None,
            }
            for r in rows
        ]

    def sim_manifest(self, show_id: str) -> Optional[list]:
        row = self._conn().execute("SELECT sim_manifest FROM shows WHERE id=?", (show_id,)).fetchone()
        return json.loads(row["sim_manifest"]) if row and row["sim_manifest"] else None

    def progress(self, show_id: str) -> dict:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) n FROM segments WHERE show_id=? GROUP BY status", (show_id,)
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        total = sum(counts.values())
        done = counts.get("done", 0) + counts.get("dead", 0)
        return {"total": total, "finished": done, "percent": round(100 * done / total, 1) if total else 0, **counts}

    def usage_hours(self, station_id: str) -> float:
        row = self._conn().execute(
            "SELECT COALESCE(SUM(seconds),0) FROM usage WHERE station_id=?", (station_id,)
        ).fetchone()
        return row[0] / 3600.0

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()
