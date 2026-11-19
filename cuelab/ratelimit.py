"""Shared token-bucket rate limiter.

The bucket lives in the same database as the job store, so every worker
process on the server draws from one budget per provider (ACRCloud, Shazam).
No single show, station or process can exceed the account-wide limit.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable


class SharedRateLimiter:
    def __init__(
        self,
        path: str,
        name: str,
        rate_per_s: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate_per_s <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.path, self.name = path, name
        self.rate, self.capacity = rate_per_s, capacity
        self.clock, self.sleep = clock, sleep
        conn = self._connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS buckets (name TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO buckets(name, tokens, updated) VALUES (?,?,?)",
            (name, capacity, self.clock()),
        )
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def try_acquire(self) -> float:
        """Take one token. Returns 0 on success, else seconds to wait."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            tokens, updated = conn.execute(
                "SELECT tokens, updated FROM buckets WHERE name=?", (self.name,)
            ).fetchone()
            now = self.clock()
            tokens = min(self.capacity, tokens + max(0.0, now - updated) * self.rate)
            if tokens >= 1.0:
                conn.execute(
                    "UPDATE buckets SET tokens=?, updated=? WHERE name=?", (tokens - 1.0, now, self.name)
                )
                wait = 0.0
            else:
                conn.execute("UPDATE buckets SET tokens=?, updated=? WHERE name=?", (tokens, now, self.name))
                wait = (1.0 - tokens) / self.rate
            conn.execute("COMMIT")
            return wait
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def acquire(self) -> None:
        while True:
            wait = self.try_acquire()
            if wait == 0.0:
                return
            self.sleep(wait)
