from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def major_minor(version: str) -> str:
    match = re.search(r"(\d+)\.(\d+)", version)
    return f"{match.group(1)}.{match.group(2)}" if match else version.strip()


class FarmDatabase:
    def __init__(self, path: Path, lease_seconds: int = 1800):
        self.path = path
        self.lease_seconds = lease_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._workers: dict[str, dict[str, Any]] = {}
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  blend_sha256 TEXT NOT NULL,
                  blender_version TEXT NOT NULL,
                  frame_start INTEGER NOT NULL,
                  frame_end INTEGER NOT NULL,
                  frame_step INTEGER NOT NULL DEFAULT 1,
                  output_format TEXT NOT NULL DEFAULT 'PNG',
                  engine TEXT NOT NULL DEFAULT 'CYCLES',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS frames (
                  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                  frame INTEGER NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending',
                  worker_id TEXT,
                  lease_expires_at TEXT,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  stderr_tail TEXT,
                  render_seconds REAL,
                  PRIMARY KEY (job_id, frame)
                );
                CREATE INDEX IF NOT EXISTS idx_frames_claim ON frames(state, job_id, frame);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_job(self, job_id: str, params: Any, digest: str) -> None:
        frames = range(params.frame_start, params.frame_end + 1, params.frame_step)
        created_at = utc_now().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs (
                  id, name, blend_sha256, blender_version, frame_start, frame_end,
                  frame_step, output_format, engine, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    params.name,
                    digest,
                    params.blender_version,
                    params.frame_start,
                    params.frame_end,
                    params.frame_step,
                    params.output_format,
                    params.engine,
                    created_at,
                ),
            )
            self._connection.executemany(
                "INSERT INTO frames (job_id, frame) VALUES (?, ?)",
                ((job_id, frame) for frame in frames),
            )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT j.*, COUNT(f.frame) AS total,
                  SUM(CASE WHEN f.state='done' THEN 1 ELSE 0 END) AS done
                FROM jobs j JOIN frames f ON f.job_id=j.id
                GROUP BY j.id ORDER BY j.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job_row = self._connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job_row:
                return None
            frame_rows = self._connection.execute(
                "SELECT * FROM frames WHERE job_id=? ORDER BY frame", (job_id,)
            ).fetchall()
            recent = self._connection.execute(
                """
                SELECT render_seconds FROM frames
                WHERE job_id=? AND render_seconds IS NOT NULL
                ORDER BY rowid DESC LIMIT 10
                """,
                (job_id,),
            ).fetchall()
            workers = [dict(worker) for worker in self._workers.values()]

        frames = [dict(row) for row in frame_rows]
        counts = Counter(frame["state"] for frame in frames)
        now = utc_now()
        for worker in workers:
            try:
                age = (now - datetime.fromisoformat(worker["last_seen"])).total_seconds()
            except (ValueError, TypeError):
                age = 0
            worker["stale"] = age > 60
            worker["last_seen_seconds"] = max(0, round(age))
        active_workers = sum(not worker["stale"] for worker in workers)
        remaining = counts["pending"] + counts["rendering"]
        average = sum(row["render_seconds"] for row in recent) / len(recent) if recent else None
        eta_seconds = (average * remaining / active_workers) if average and active_workers else None
        return {
            **dict(job_row),
            "counts": {
                state: counts[state] for state in ("pending", "rendering", "done", "failed")
            },
            "frames": frames,
            "workers": sorted(workers, key=lambda worker: worker["worker_id"]),
            "eta_seconds": eta_seconds,
        }

    def claim_work(self, worker_id: str, blender_version: str) -> dict[str, Any] | None:
        now = utc_now()
        lease_expires = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        version_key = major_minor(blender_version)
        with self._lock:
            self._workers[worker_id] = {
                "worker_id": worker_id,
                "blender_version": blender_version,
                "current_frame": None,
                "current_job_id": None,
                "last_seen": now.isoformat(),
            }
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                candidates = self._connection.execute(
                    """
                    SELECT f.rowid, f.job_id, f.frame, j.blend_sha256, j.output_format,
                           j.engine, j.blender_version
                    FROM frames f JOIN jobs j ON j.id=f.job_id
                    WHERE f.state='pending' AND j.status='active'
                    ORDER BY j.created_at, f.frame
                    """
                ).fetchall()
                selected = next(
                    (
                        row
                        for row in candidates
                        if major_minor(row["blender_version"]) == version_key
                    ),
                    None,
                )
                if selected is None:
                    self._connection.commit()
                    return None
                changed = self._connection.execute(
                    """
                    UPDATE frames SET state='rendering', worker_id=?, lease_expires_at=?
                    WHERE rowid=? AND state='pending'
                    """,
                    (worker_id, lease_expires, selected["rowid"]),
                ).rowcount
                self._connection.commit()
                if changed != 1:
                    return None
            except Exception:
                self._connection.rollback()
                raise
            self._workers[worker_id]["current_frame"] = selected["frame"]
            self._workers[worker_id]["current_job_id"] = selected["job_id"]
        return {
            "job_id": selected["job_id"],
            "frame": selected["frame"],
            "blend_sha256": selected["blend_sha256"],
            "blend_url": f"/jobs/{selected['job_id']}/blend",
            "output_format": selected["output_format"],
            "engine": selected["engine"],
            "lease_seconds": self.lease_seconds,
        }

    def get_blend_digest(self, job_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT blend_sha256 FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return row["blend_sha256"] if row else None

    def complete_frame(
        self, job_id: str, frame: int, worker_id: str, render_seconds: float | None
    ) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connection:
            changed = self._connection.execute(
                """
                UPDATE frames SET state='done', render_seconds=?, lease_expires_at=NULL,
                                  stderr_tail=NULL
                WHERE job_id=? AND frame=? AND state='rendering' AND worker_id=?
                  AND lease_expires_at > ?
                """,
                (render_seconds, job_id, frame, worker_id, now),
            ).rowcount
            if changed != 1:
                return False
            if worker_id in self._workers:
                self._workers[worker_id]["current_frame"] = None
                self._workers[worker_id]["current_job_id"] = None
                self._workers[worker_id]["last_seen"] = utc_now().isoformat()
            remaining = self._connection.execute(
                "SELECT COUNT(*) AS count FROM frames WHERE job_id=? AND state!='done'",
                (job_id,),
            ).fetchone()["count"]
            if remaining == 0:
                self._connection.execute("UPDATE jobs SET status='complete' WHERE id=?", (job_id,))
        return True

    def fail_frame(self, job_id: str, frame: int, worker_id: str, stderr_tail: str) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT attempts FROM frames
                WHERE job_id=? AND frame=? AND state='rendering' AND worker_id=?
                  AND lease_expires_at > ?
                """,
                (job_id, frame, worker_id, now),
            ).fetchone()
            if not row:
                return False
            attempts = row["attempts"] + 1
            state = "failed" if attempts >= 3 else "pending"
            self._connection.execute(
                """
                UPDATE frames SET state=?, attempts=?, stderr_tail=?, worker_id=NULL,
                                  lease_expires_at=NULL
                WHERE job_id=? AND frame=?
                """,
                (state, attempts, stderr_tail[-4096:], job_id, frame),
            )
            if worker_id in self._workers:
                self._workers[worker_id]["current_frame"] = None
                self._workers[worker_id]["current_job_id"] = None
                self._workers[worker_id]["last_seen"] = utc_now().isoformat()
        return True

    def cancel_job(self, job_id: str) -> bool:
        with self._lock, self._connection:
            return bool(
                self._connection.execute(
                    "UPDATE jobs SET status='cancelled' WHERE id=? AND status='active'", (job_id,)
                ).rowcount
            )

    def requeue_frame(self, job_id: str, frame: int) -> bool:
        with self._lock, self._connection:
            return bool(
                self._connection.execute(
                    """
                    UPDATE frames SET state='pending', attempts=0, stderr_tail=NULL,
                                      worker_id=NULL, lease_expires_at=NULL
                    WHERE job_id=? AND frame=? AND state='failed'
                    """,
                    (job_id, frame),
                ).rowcount
            )

    def sweep_expired_leases(self, now: datetime | None = None) -> int:
        cutoff = (now or utc_now()).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT job_id, frame, attempts FROM frames
                WHERE state='rendering' AND lease_expires_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                attempts = row["attempts"] + 1
                state = "failed" if attempts >= 3 else "pending"
                self._connection.execute(
                    """
                    UPDATE frames SET state=?, attempts=?, worker_id=NULL, lease_expires_at=NULL,
                                      stderr_tail=CASE WHEN ?='failed' THEN
                                        COALESCE(stderr_tail, 'Worker lease expired three times')
                                        ELSE stderr_tail END
                    WHERE job_id=? AND frame=? AND state='rendering'
                    """,
                    (state, attempts, state, row["job_id"], row["frame"]),
                )
        return len(rows)

    def set_lease_for_test(self, job_id: str, frame: int, expires_at: str) -> None:
        """Set a lease timestamp for deterministic state-machine tests."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE frames SET lease_expires_at=? WHERE job_id=? AND frame=?",
                (expires_at, job_id, frame),
            )
