#!/usr/bin/env python3
"""Minimal Codex quota logger.

Responsibilities:
1. Read the existing Codex login state from ~/.codex/auth.json (read-only).
2. Poll the Codex/ChatGPT usage endpoint at a fixed interval.
3. Persist quota snapshots to SQLite.
4. Serve a tiny local HTTP dashboard with an embedded HTML/CSS/JS/SVG frontend.

Runtime dependencies: Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_NAME = "codex-quota-logger"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_DB_PATH = Path("data") / "quota.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_INTERVAL = 60.0
COMPACTION_PERIOD_SECONDS = 24 * 60 * 60
COMPACTION_MIN_GAP_SECONDS = 180

LOG = logging.getLogger(APP_NAME)


class QuotaError(RuntimeError):
    """Raised when quota data cannot be fetched or parsed."""


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def format_window(window_seconds: int) -> str:
    if window_seconds == 18_000:
        return "5-hour"
    if window_seconds == 604_800:
        return "7-day"

    if window_seconds % 86_400 == 0:
        days = window_seconds // 86_400
        return f"{days}-day"

    if window_seconds % 3_600 == 0:
        hours = window_seconds // 3_600
        return f"{hours}-hour"

    if window_seconds % 60 == 0:
        minutes = window_seconds // 60
        return f"{minutes}-minute"

    return f"{window_seconds}-second"


def coerce_epoch(value: Any) -> int | None:
    """Convert a timestamp-like value to Unix seconds when possible."""

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        stripped = value.strip()

        if not stripped:
            return None

        try:
            return int(float(stripped))
        except ValueError:
            pass

        try:
            return int(datetime.fromisoformat(stripped.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    return None


class QuotaClient:
    """Read Codex authentication state and fetch the current quota snapshot."""

    def __init__(
        self,
        auth_path: Path,
        usage_url: str = USAGE_URL,
        timeout: float = 15.0,
    ) -> None:
        self.auth_path = auth_path
        self.usage_url = usage_url
        self.timeout = timeout

    def _read_credentials(self) -> tuple[str, str]:
        try:
            raw = self.auth_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise QuotaError(f"Codex auth file not found: {self.auth_path}") from exc
        except OSError as exc:
            raise QuotaError(f"Unable to read Codex auth file: {exc}") from exc

        try:
            auth = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QuotaError(f"Invalid JSON in {self.auth_path}: {exc}") from exc

        tokens = auth.get("tokens")

        if not isinstance(tokens, dict):
            raise QuotaError("auth.json does not contain a valid 'tokens' object")

        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")

        if not isinstance(access_token, str) or not access_token:
            raise QuotaError("tokens.access_token is missing")

        if not isinstance(account_id, str) or not account_id:
            raise QuotaError("tokens.account_id is missing")

        return access_token, account_id

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch all quota windows exposed under rate_limit."""

        access_token, account_id = self._read_credentials()

        request = urllib.request.Request(
            self.usage_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-Id": account_id,
                "Accept": "application/json",
                "User-Agent": "codex-cli",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()

        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise QuotaError(
                    f"Codex authentication rejected: HTTP {exc.code}. "
                    "The server-side ~/.codex/auth.json may need to be refreshed "
                    "by using Codex normally on this server."
                ) from exc

            raise QuotaError(f"Usage endpoint returned HTTP {exc.code}") from exc

        except urllib.error.URLError as exc:
            raise QuotaError(f"Unable to reach usage endpoint: {exc.reason}") from exc

        except TimeoutError as exc:
            raise QuotaError("Usage request timed out") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QuotaError("Usage endpoint returned invalid JSON") from exc

        rate_limit = payload.get("rate_limit")

        if not isinstance(rate_limit, dict):
            raise QuotaError("Usage response does not contain a valid rate_limit object")

        now = int(time.time())
        samples_by_window: dict[int, dict[str, Any]] = {}

        for window in rate_limit.values():
            if not isinstance(window, dict):
                continue

            used_raw = window.get("used_percent")

            if used_raw is None:
                continue

            window_seconds_raw = window.get("limit_window_seconds")

            # Defensive compatibility with app-server-style names if the
            # backend schema changes slightly in the future.
            if window_seconds_raw is None:
                minutes = window.get("window_duration_mins")
                if minutes is None:
                    minutes = window.get("windowDurationMins")

                if isinstance(minutes, (int, float)):
                    window_seconds_raw = int(minutes * 60)

            try:
                window_seconds = int(window_seconds_raw)
                used_percent = float(used_raw)
            except (TypeError, ValueError):
                continue

            if window_seconds <= 0:
                continue

            if not 0.0 <= used_percent <= 100.0:
                continue

            resets_at = coerce_epoch(window.get("reset_at"))

            if resets_at is None:
                reset_after = window.get("reset_after_seconds")
                if isinstance(reset_after, (int, float)):
                    resets_at = now + int(reset_after)

            samples_by_window[window_seconds] = {
                "window_seconds": window_seconds,
                "used_percent": used_percent,
                "resets_at": resets_at,
            }

        samples = sorted(
            samples_by_window.values(),
            key=lambda item: item["window_seconds"],
        )

        if not samples:
            raise QuotaError("No usable quota windows were found in rate_limit")

        return samples


class Database:
    """SQLite persistence layer."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_samples (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at      INTEGER NOT NULL,
                    window_seconds  INTEGER NOT NULL CHECK(window_seconds > 0),
                    used_percent    REAL NOT NULL
                                    CHECK(used_percent >= 0 AND used_percent <= 100),
                    resets_at       INTEGER
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quota_samples_window_time
                ON quota_samples(window_seconds, sampled_at)
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS compaction_state (
                    window_seconds       INTEGER PRIMARY KEY,
                    tail_first_id        INTEGER NOT NULL,
                    tail_last_id         INTEGER NOT NULL,
                    tail_last_sampled_at INTEGER NOT NULL,
                    tail_used_percent    REAL NOT NULL,
                    tail_resets_at       INTEGER
                )
                """
            )

    def insert_samples(
        self,
        sampled_at: int,
        samples: list[dict[str, Any]],
    ) -> None:
        rows = [
            (
                sampled_at,
                int(sample["window_seconds"]),
                float(sample["used_percent"]),
                sample.get("resets_at"),
            )
            for sample in samples
        ]

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO quota_samples (
                    sampled_at,
                    window_seconds,
                    used_percent,
                    resets_at
                )
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    def current(self) -> list[dict[str, Any]]:
        """Return the newest stored sample for every quota window."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    q.sampled_at,
                    q.window_seconds,
                    q.used_percent,
                    q.resets_at
                FROM quota_samples AS q
                WHERE q.id IN (
                    SELECT MAX(id)
                    FROM quota_samples
                    GROUP BY window_seconds
                )
                ORDER BY q.window_seconds
                """
            ).fetchall()

        return [
            {
                "sampled_at": int(row["sampled_at"]),
                "window_seconds": int(row["window_seconds"]),
                "used_percent": float(row["used_percent"]),
                "remaining_percent": clamp_percent(
                    100.0 - float(row["used_percent"])
                ),
                "resets_at": (
                    int(row["resets_at"])
                    if row["resets_at"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def history(
        self,
        from_timestamp: int,
        to_timestamp: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    sampled_at,
                    window_seconds,
                    used_percent
                FROM quota_samples
                WHERE sampled_at >= ?
                  AND sampled_at <= ?
                ORDER BY window_seconds, sampled_at, id
                """,
                (from_timestamp, to_timestamp),
            ).fetchall()

        grouped: dict[int, list[list[float | int]]] = {}

        for row in rows:
            window_seconds = int(row["window_seconds"])
            remaining = clamp_percent(100.0 - float(row["used_percent"]))

            grouped.setdefault(window_seconds, []).append(
                [
                    int(row["sampled_at"]),
                    round(remaining, 6),
                ]
            )

        return [
            {
                "window_seconds": window_seconds,
                "points": points,
            }
            for window_seconds, points in sorted(grouped.items())
        ]

    def row_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM quota_samples"
            ).fetchone()

        return int(row["count"])

    def compact_identical_runs(
        self,
        expected_interval: float,
    ) -> dict[str, int]:
        """Compact stable quota runs while preserving their endpoints."""

        max_gap_seconds = max(
            COMPACTION_MIN_GAP_SECONDS,
            int(expected_interval * 3),
        )
        stats = {
            "windows": 0,
            "new_rows": 0,
            "deleted_rows": 0,
            "max_gap_seconds": max_gap_seconds,
        }

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                windows = [
                    int(row["window_seconds"])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT window_seconds
                        FROM quota_samples
                        ORDER BY window_seconds
                        """
                    ).fetchall()
                ]

                for window_seconds in windows:
                    state = connection.execute(
                        """
                        SELECT tail_first_id, tail_last_id,
                               tail_last_sampled_at, tail_used_percent,
                               tail_resets_at
                        FROM compaction_state
                        WHERE window_seconds = ?
                        """,
                        (window_seconds,),
                    ).fetchone()

                    if state is not None:
                        tail_exists = connection.execute(
                            """
                            SELECT 1 FROM quota_samples
                            WHERE id = ? AND window_seconds = ?
                            """,
                            (int(state["tail_last_id"]), window_seconds),
                        ).fetchone()
                        if tail_exists is None:
                            LOG.warning(
                                "compaction state for %s is stale; "
                                "rebuilding from existing history",
                                format_window(window_seconds),
                            )
                            connection.execute(
                                "DELETE FROM compaction_state "
                                "WHERE window_seconds = ?",
                                (window_seconds,),
                            )
                            state = None

                    cursor_id = int(state["tail_last_id"]) if state else 0
                    rows = connection.execute(
                        """
                        SELECT id, sampled_at, used_percent, resets_at
                        FROM quota_samples
                        WHERE window_seconds = ? AND id > ?
                        ORDER BY id
                        """,
                        (window_seconds, cursor_id),
                    ).fetchall()
                    if not rows:
                        continue

                    stats["windows"] += 1
                    stats["new_rows"] += len(rows)

                    if state is None:
                        run_first_id = None
                        run_last_id = None
                        previous_sampled_at = None
                        previous_signature = None
                    else:
                        run_first_id = int(state["tail_first_id"])
                        run_last_id = int(state["tail_last_id"])
                        previous_sampled_at = int(state["tail_last_sampled_at"])
                        previous_signature = (
                            float(state["tail_used_percent"]),
                            int(state["tail_resets_at"])
                            if state["tail_resets_at"] is not None
                            else None,
                        )

                    delete_ids = []
                    for row in rows:
                        row_id = int(row["id"])
                        sampled_at = int(row["sampled_at"])
                        signature = (
                            float(row["used_percent"]),
                            int(row["resets_at"])
                            if row["resets_at"] is not None
                            else None,
                        )

                        continues_run = False
                        if (
                            run_first_id is not None
                            and run_last_id is not None
                            and previous_sampled_at is not None
                            and previous_signature is not None
                        ):
                            gap = sampled_at - previous_sampled_at
                            continues_run = (
                                signature == previous_signature
                                and 0 <= gap <= max_gap_seconds
                            )

                        if continues_run:
                            if run_last_id != run_first_id:
                                delete_ids.append(run_last_id)
                            run_last_id = row_id
                        else:
                            run_first_id = row_id
                            run_last_id = row_id

                        previous_sampled_at = sampled_at
                        previous_signature = signature

                    if delete_ids:
                        connection.executemany(
                            "DELETE FROM quota_samples WHERE id = ?",
                            [(row_id,) for row_id in delete_ids],
                        )
                        stats["deleted_rows"] += len(delete_ids)

                    assert run_first_id is not None
                    assert run_last_id is not None
                    assert previous_sampled_at is not None
                    assert previous_signature is not None
                    connection.execute(
                        """
                        INSERT INTO compaction_state (
                            window_seconds, tail_first_id, tail_last_id,
                            tail_last_sampled_at, tail_used_percent,
                            tail_resets_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(window_seconds) DO UPDATE SET
                            tail_first_id = excluded.tail_first_id,
                            tail_last_id = excluded.tail_last_id,
                            tail_last_sampled_at = excluded.tail_last_sampled_at,
                            tail_used_percent = excluded.tail_used_percent,
                            tail_resets_at = excluded.tail_resets_at
                        """,
                        (
                            window_seconds,
                            run_first_id,
                            run_last_id,
                            previous_sampled_at,
                            previous_signature[0],
                            previous_signature[1],
                        ),
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return stats


class Sampler:
    """Fixed-cadence quota sampler."""

    def __init__(
        self,
        client: QuotaClient,
        database: Database,
        interval: float,
        stop_event: threading.Event,
    ) -> None:
        self.client = client
        self.database = database
        self.interval = interval
        self.stop_event = stop_event

        self._lock = threading.Lock()

        self._state: dict[str, Any] = {
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "last_sample_count": 0,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def poll_once(self) -> None:
        sampled_at = int(time.time())

        with self._lock:
            self._state["last_attempt_at"] = sampled_at

        try:
            samples = self.client.fetch()
            self.database.insert_samples(sampled_at, samples)

        except Exception as exc:
            message = str(exc)

            with self._lock:
                self._state["last_error"] = message
                self._state["consecutive_failures"] += 1
                self._state["last_sample_count"] = 0

            LOG.warning("quota poll failed: %s", message)
            return

        with self._lock:
            self._state["last_success_at"] = sampled_at
            self._state["last_error"] = None
            self._state["consecutive_failures"] = 0
            self._state["last_sample_count"] = len(samples)

        summary = ", ".join(
            f"{format_window(int(sample['window_seconds']))}="
            f"{100.0 - float(sample['used_percent']):g}%"
            for sample in samples
        )

        LOG.info("sampled quota: %s", summary)

    def run(self) -> None:
        """Poll immediately, then keep a drift-resistant fixed cadence."""

        next_deadline = time.monotonic()

        while not self.stop_event.is_set():
            self.poll_once()

            next_deadline += self.interval
            now = time.monotonic()

            # If a request or system pause caused us to miss one or more
            # deadlines, skip the missed ticks instead of polling in a burst.
            if next_deadline <= now:
                missed = int((now - next_deadline) // self.interval) + 1
                next_deadline += missed * self.interval

            wait_seconds = max(0.0, next_deadline - time.monotonic())

            if self.stop_event.wait(wait_seconds):
                break


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex Quota</title>
<style>
:root {
    color-scheme: light dark;

    --bg: #f5f7fb;
    --panel: rgba(255, 255, 255, 0.86);
    --panel-solid: #ffffff;
    --text: #172033;
    --muted: #748096;
    --border: rgba(26, 41, 66, 0.10);
    --grid: rgba(60, 76, 100, 0.12);
    --shadow: 0 18px 50px rgba(36, 50, 76, 0.08);

    --blue: #4f7cff;
    --purple: #8b67e8;
    --green: #27a86b;
    --red: #d95858;
    --amber: #c68a2a;
}

@media (prefers-color-scheme: dark) {
    :root {
        --bg: #0d1117;
        --panel: rgba(21, 27, 36, 0.88);
        --panel-solid: #151b24;
        --text: #e7edf7;
        --muted: #8995a8;
        --border: rgba(220, 230, 245, 0.09);
        --grid: rgba(220, 230, 245, 0.09);
        --shadow: 0 18px 50px rgba(0, 0, 0, 0.22);

        --blue: #6c91ff;
        --purple: #a17bf2;
        --green: #42bd82;
        --red: #ef7070;
        --amber: #e0a749;
    }
}

* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    min-height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

body {
    padding: 36px 20px 60px;
}

.shell {
    width: min(1120px, 100%);
    margin: 0 auto;
}

.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    margin-bottom: 24px;
}

.brand h1 {
    margin: 0;
    font-size: 25px;
    line-height: 1.2;
    font-weight: 680;
    letter-spacing: -0.025em;
}

.brand p {
    margin: 7px 0 0;
    color: var(--muted);
    font-size: 13px;
}

.status {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--muted);
    font-size: 13px;
    white-space: nowrap;
}

.status-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--muted);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--muted) 13%, transparent);
}

.status.ok .status-dot {
    background: var(--green);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--green) 14%, transparent);
}

.status.degraded .status-dot {
    background: var(--amber);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--amber) 14%, transparent);
}

.status.error .status-dot {
    background: var(--red);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--red) 14%, transparent);
}

.cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
    margin-bottom: 16px;
}

.card,
.chart-card {
    background: var(--panel);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    backdrop-filter: blur(18px);
}

.card {
    border-radius: 18px;
    padding: 22px;
}

.card-top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 14px;
}

.window-name {
    color: var(--muted);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.02em;
}

.percent {
    margin-top: 13px;
    font-size: 42px;
    line-height: 1;
    font-weight: 690;
    letter-spacing: -0.045em;
}

.percent span {
    font-size: 18px;
    margin-left: 2px;
    color: var(--muted);
    font-weight: 560;
}

.progress {
    height: 7px;
    margin-top: 18px;
    background: color-mix(in srgb, var(--muted) 13%, transparent);
    border-radius: 999px;
    overflow: hidden;
}

.progress > div {
    height: 100%;
    border-radius: inherit;
    transition: width 300ms ease;
}

.card-meta {
    margin-top: 12px;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.55;
}

.empty-cards {
    grid-column: 1 / -1;
    padding: 32px;
    color: var(--muted);
    text-align: center;
    border: 1px dashed var(--border);
    border-radius: 18px;
}

.chart-card {
    border-radius: 20px;
    padding: 20px 20px 15px;
}

.chart-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 9px;
}

.chart-title {
    font-size: 14px;
    font-weight: 650;
}

.range-buttons {
    display: flex;
    gap: 5px;
    padding: 4px;
    border-radius: 11px;
    background: color-mix(in srgb, var(--muted) 10%, transparent);
}

.range-buttons button {
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--muted);
    padding: 6px 10px;
    border-radius: 8px;
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
}

.range-buttons button:hover {
    color: var(--text);
}

.range-buttons button.active {
    background: var(--panel-solid);
    color: var(--text);
    box-shadow: 0 1px 5px rgba(0, 0, 0, 0.08);
}

.legend {
    min-height: 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    align-items: center;
    margin: 4px 0 2px 58px;
}

.legend-item {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: 12px;
}

.legend-swatch {
    width: 17px;
    height: 3px;
    border-radius: 999px;
}

.chart-wrap {
    position: relative;
    width: 100%;
}

#chart {
    display: block;
    width: 100%;
    aspect-ratio: 1000 / 360;
    overflow: visible;
}

.chart-empty {
    position: absolute;
    inset: 0;
    display: none;
    place-items: center;
    color: var(--muted);
    font-size: 13px;
    pointer-events: none;
}

.tooltip {
    position: absolute;
    z-index: 20;
    display: none;
    min-width: 160px;
    padding: 10px 12px;
    border-radius: 11px;
    background: color-mix(in srgb, var(--panel-solid) 94%, transparent);
    border: 1px solid var(--border);
    box-shadow: 0 12px 34px rgba(0, 0, 0, 0.16);
    pointer-events: none;
    font-size: 12px;
    line-height: 1.5;
    backdrop-filter: blur(12px);
}

.tooltip-time {
    color: var(--muted);
    margin-bottom: 6px;
}

.tooltip-row {
    display: flex;
    justify-content: space-between;
    gap: 22px;
}

.tooltip-name {
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

.tooltip-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
}

.footer {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin-top: 14px;
    padding: 0 4px;
    color: var(--muted);
    font-size: 11px;
}

@media (max-width: 700px) {
    body {
        padding: 22px 12px 40px;
    }

    .header,
    .chart-header {
        align-items: flex-start;
        flex-direction: column;
    }

    .range-buttons {
        width: 100%;
    }

    .range-buttons button {
        flex: 1;
    }

    .chart-card {
        padding: 16px 10px 12px;
    }

    .legend {
        margin-left: 48px;
    }

    #chart {
        min-height: 280px;
    }

    .footer {
        flex-direction: column;
        gap: 4px;
    }
}
</style>
</head>
<body>
<main class="shell">
    <header class="header">
        <div class="brand">
            <h1>Codex Quota</h1>
            <p>Local quota history from your Codex account</p>
        </div>

        <div id="status" class="status">
            <span class="status-dot"></span>
            <span id="statusText">Starting…</span>
        </div>
    </header>

    <section id="cards" class="cards">
        <div class="empty-cards">Waiting for the first quota sample…</div>
    </section>

    <section class="chart-card">
        <div class="chart-header">
            <div class="chart-title">Remaining quota</div>

            <div class="range-buttons">
                <button data-hours="6">6h</button>
                <button data-hours="24">24h</button>
                <button data-hours="168" class="active">7d</button>
                <button data-hours="720">30d</button>
            </div>
        </div>

        <div id="legend" class="legend"></div>

        <div id="chartWrap" class="chart-wrap">
            <svg
                id="chart"
                viewBox="0 0 1000 360"
                preserveAspectRatio="xMidYMid meet"
                aria-label="Codex quota history"
            >
                <defs id="chartDefs"></defs>
                <g id="gridLayer"></g>
                <g id="areaLayer"></g>
                <g id="lineLayer"></g>
                <g id="hoverLayer"></g>
                <rect
                    id="hitbox"
                    x="58"
                    y="20"
                    width="922"
                    height="298"
                    fill="transparent"
                    pointer-events="all"
                ></rect>
            </svg>

            <div id="chartEmpty" class="chart-empty">
                No samples in this time range.
            </div>

            <div id="tooltip" class="tooltip"></div>
        </div>
    </section>

    <footer class="footer">
        <span id="lastSample">Last sample: —</span>
        <span id="sampleCount">Stored samples: —</span>
    </footer>
</main>

<script>
"use strict";

const WIDTH = 1000;
const HEIGHT = 360;

const MARGIN = {
    left: 58,
    right: 20,
    top: 20,
    bottom: 42,
};

const PLOT_LEFT = MARGIN.left;
const PLOT_RIGHT = WIDTH - MARGIN.right;
const PLOT_TOP = MARGIN.top;
const PLOT_BOTTOM = HEIGHT - MARGIN.bottom;
const PLOT_WIDTH = PLOT_RIGHT - PLOT_LEFT;
const PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP;

const COLORS = [
    "#4f7cff",
    "#8b67e8",
    "#25a876",
    "#e08a3d",
    "#df5f75",
    "#42a5b3",
];

let selectedHours = Number(localStorage.getItem("quota-range-hours") || "168");

if (![6, 24, 168, 720].includes(selectedHours)) {
    selectedHours = 168;
}

let historyData = null;
let currentData = null;
let healthData = null;

const cards = document.getElementById("cards");
const status = document.getElementById("status");
const statusText = document.getElementById("statusText");
const lastSample = document.getElementById("lastSample");
const sampleCount = document.getElementById("sampleCount");

const svg = document.getElementById("chart");
const defs = document.getElementById("chartDefs");
const gridLayer = document.getElementById("gridLayer");
const areaLayer = document.getElementById("areaLayer");
const lineLayer = document.getElementById("lineLayer");
const hoverLayer = document.getElementById("hoverLayer");
const hitbox = document.getElementById("hitbox");

const chartWrap = document.getElementById("chartWrap");
const chartEmpty = document.getElementById("chartEmpty");
const tooltip = document.getElementById("tooltip");
const legend = document.getElementById("legend");

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function windowLabel(seconds) {
    if (seconds === 18000) {
        return "5-hour";
    }

    if (seconds === 604800) {
        return "7-day";
    }

    if (seconds % 86400 === 0) {
        return `${seconds / 86400}-day`;
    }

    if (seconds % 3600 === 0) {
        return `${seconds / 3600}-hour`;
    }

    if (seconds % 60 === 0) {
        return `${seconds / 60}-minute`;
    }

    return `${seconds}-second`;
}

function colorFor(windowSeconds, index) {
    if (windowSeconds === 18000) {
        return COLORS[0];
    }

    if (windowSeconds === 604800) {
        return COLORS[1];
    }

    return COLORS[index % COLORS.length];
}

function formatPercent(value) {
    const rounded = Math.round(Number(value) * 10) / 10;

    if (Number.isInteger(rounded)) {
        return String(rounded);
    }

    return rounded.toFixed(1);
}

function formatDateTime(timestamp) {
    if (!timestamp) {
        return "—";
    }

    return new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    }).format(new Date(timestamp * 1000));
}

function formatTick(timestamp, hours) {
    const date = new Date(timestamp * 1000);

    if (hours <= 24) {
        return new Intl.DateTimeFormat(undefined, {
            hour: "2-digit",
            minute: "2-digit",
        }).format(date);
    }

    return new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
    }).format(date);
}

function formatDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));

    const days = Math.floor(seconds / 86400);
    seconds %= 86400;

    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;

    const minutes = Math.floor(seconds / 60);

    if (days > 0) {
        return `${days}d ${hours}h`;
    }

    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    }

    return `${minutes}m`;
}

function resetText(timestamp) {
    if (!timestamp) {
        return "Reset time unavailable";
    }

    const now = Date.now() / 1000;
    const delta = timestamp - now;

    if (delta <= 0) {
        return `Reset expected · ${formatDateTime(timestamp)}`;
    }

    return `Reset in ${formatDuration(delta)} · ${formatDateTime(timestamp)}`;
}

function renderCards() {
    const samples = currentData?.samples || [];

    if (samples.length === 0) {
        cards.innerHTML =
            '<div class="empty-cards">Waiting for the first quota sample…</div>';
        lastSample.textContent = "Last sample: —";
        return;
    }

    cards.innerHTML = samples
        .map((sample, index) => {
            const remaining = Number(sample.remaining_percent);
            const color = colorFor(sample.window_seconds, index);

            return `
                <article class="card">
                    <div class="card-top">
                        <div class="window-name">
                            ${escapeHtml(windowLabel(sample.window_seconds))}
                        </div>
                    </div>

                    <div class="percent">
                        ${escapeHtml(formatPercent(remaining))}<span>%</span>
                    </div>

                    <div class="progress">
                        <div
                            style="
                                width: ${Math.max(0, Math.min(100, remaining))}%;
                                background: ${color};
                            "
                        ></div>
                    </div>

                    <div class="card-meta">
                        ${escapeHtml(resetText(sample.resets_at))}
                    </div>
                </article>
            `;
        })
        .join("");

    const newest = Math.max(...samples.map((sample) => sample.sampled_at));

    lastSample.textContent = `Last sample: ${formatDateTime(newest)}`;
}

function renderHealth() {
    const state = healthData || {};

    status.classList.remove("ok", "degraded", "error");

    if (state.status === "ok") {
        status.classList.add("ok");
        statusText.textContent = "Connected";
    } else if (state.status === "degraded") {
        status.classList.add("degraded");
        statusText.textContent = "Sampling degraded";
    } else if (state.status === "error") {
        status.classList.add("error");
        statusText.textContent = "Sampling error";
    } else {
        statusText.textContent = "Starting…";
    }

    if (typeof state.rows === "number") {
        sampleCount.textContent =
            `Stored samples: ${state.rows.toLocaleString()}`;
    }
}

function scaleX(timestamp, from, to) {
    if (to <= from) {
        return PLOT_LEFT;
    }

    return (
        PLOT_LEFT +
        ((timestamp - from) / (to - from)) * PLOT_WIDTH
    );
}

function scaleY(percent) {
    return (
        PLOT_BOTTOM -
        (Math.max(0, Math.min(100, percent)) / 100) * PLOT_HEIGHT
    );
}

function makeLinePath(points, from, to) {
    if (!points.length) {
        return "";
    }

    return points
        .map((point, index) => {
            const x = scaleX(point[0], from, to);
            const y = scaleY(point[1]);

            return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
        })
        .join(" ");
}

function makeAreaPath(points, from, to) {
    if (!points.length) {
        return "";
    }

    const firstX = scaleX(points[0][0], from, to);
    const lastX = scaleX(points[points.length - 1][0], from, to);

    const line = points
        .map((point) => {
            const x = scaleX(point[0], from, to);
            const y = scaleY(point[1]);
            return `L ${x.toFixed(2)} ${y.toFixed(2)}`;
        })
        .join(" ");

    return [
        `M ${firstX.toFixed(2)} ${PLOT_BOTTOM}`,
        line,
        `L ${lastX.toFixed(2)} ${PLOT_BOTTOM}`,
        "Z",
    ].join(" ");
}

function renderGrid(from, to) {
    const yValues = [0, 25, 50, 75, 100];

    const yGrid = yValues
        .map((value) => {
            const y = scaleY(value);

            return `
                <line
                    x1="${PLOT_LEFT}"
                    x2="${PLOT_RIGHT}"
                    y1="${y}"
                    y2="${y}"
                    stroke="var(--grid)"
                    stroke-width="1"
                ></line>

                <text
                    x="${PLOT_LEFT - 12}"
                    y="${y + 4}"
                    text-anchor="end"
                    fill="var(--muted)"
                    font-size="11"
                >${value}%</text>
            `;
        })
        .join("");

    const tickCount = 5;
    const xParts = [];

    for (let i = 0; i < tickCount; i += 1) {
        const ratio = i / (tickCount - 1);
        const timestamp = from + (to - from) * ratio;
        const x = scaleX(timestamp, from, to);

        xParts.push(`
            <text
                x="${x}"
                y="${PLOT_BOTTOM + 27}"
                text-anchor="${
                    i === 0 ? "start" :
                    i === tickCount - 1 ? "end" :
                    "middle"
                }"
                fill="var(--muted)"
                font-size="11"
            >${escapeHtml(formatTick(timestamp, selectedHours))}</text>
        `);
    }

    gridLayer.innerHTML = yGrid + xParts.join("");
}

function renderChart() {
    const data = historyData;

    if (!data) {
        return;
    }

    const from = Number(data.from);
    const to = Number(data.to);
    const series = data.series || [];

    const hasPoints = series.some((item) => item.points?.length > 0);

    chartEmpty.style.display = hasPoints ? "none" : "grid";

    renderGrid(from, to);

    defs.innerHTML = "";
    areaLayer.innerHTML = "";
    lineLayer.innerHTML = "";
    hoverLayer.innerHTML = "";
    tooltip.style.display = "none";

    legend.innerHTML = series
        .map((item, index) => {
            const color = colorFor(item.window_seconds, index);

            return `
                <span class="legend-item">
                    <span
                        class="legend-swatch"
                        style="background: ${color}"
                    ></span>
                    ${escapeHtml(windowLabel(item.window_seconds))}
                </span>
            `;
        })
        .join("");

    series.forEach((item, index) => {
        const points = item.points || [];

        if (!points.length) {
            return;
        }

        const color = colorFor(item.window_seconds, index);
        const gradientId = `quotaFill${index}`;

        defs.insertAdjacentHTML(
            "beforeend",
            `
            <linearGradient
                id="${gradientId}"
                x1="0"
                y1="0"
                x2="0"
                y2="1"
            >
                <stop
                    offset="0%"
                    stop-color="${color}"
                    stop-opacity="0.28"
                ></stop>
                <stop
                    offset="100%"
                    stop-color="${color}"
                    stop-opacity="0.025"
                ></stop>
            </linearGradient>
            `,
        );

        areaLayer.insertAdjacentHTML(
            "beforeend",
            `
            <path
                d="${makeAreaPath(points, from, to)}"
                fill="url(#${gradientId})"
                stroke="none"
            ></path>
            `,
        );

        lineLayer.insertAdjacentHTML(
            "beforeend",
            `
            <path
                d="${makeLinePath(points, from, to)}"
                fill="none"
                stroke="${color}"
                stroke-width="2.25"
                stroke-linecap="round"
                stroke-linejoin="round"
                vector-effect="non-scaling-stroke"
            ></path>
            `,
        );
    });
}

function nearestPoint(points, timestamp) {
    if (!points || points.length === 0) {
        return null;
    }

    let low = 0;
    let high = points.length - 1;

    while (low < high) {
        const mid = Math.floor((low + high) / 2);

        if (points[mid][0] < timestamp) {
            low = mid + 1;
        } else {
            high = mid;
        }
    }

    const right = points[low];
    const left = low > 0 ? points[low - 1] : null;

    if (!left) {
        return right;
    }

    return (
        Math.abs(left[0] - timestamp) <= Math.abs(right[0] - timestamp)
            ? left
            : right
    );
}

function hideTooltip() {
    hoverLayer.innerHTML = "";
    tooltip.style.display = "none";
}

function showTooltip(event) {
    if (!historyData) {
        return;
    }

    const series = historyData.series || [];

    if (!series.some((item) => item.points?.length > 0)) {
        return;
    }

    const rect = svg.getBoundingClientRect();

    const svgX =
        ((event.clientX - rect.left) / rect.width) * WIDTH;

    if (svgX < PLOT_LEFT || svgX > PLOT_RIGHT) {
        hideTooltip();
        return;
    }

    const from = Number(historyData.from);
    const to = Number(historyData.to);

    const ratio = (svgX - PLOT_LEFT) / PLOT_WIDTH;
    const targetTimestamp = from + ratio * (to - from);

    const rows = [];
    const circles = [];

    series.forEach((item, index) => {
        const point = nearestPoint(item.points, targetTimestamp);

        if (!point) {
            return;
        }

        const color = colorFor(item.window_seconds, index);

        const pointX = scaleX(point[0], from, to);
        const pointY = scaleY(point[1]);

        circles.push(`
            <circle
                cx="${pointX}"
                cy="${pointY}"
                r="4.3"
                fill="${color}"
                stroke="var(--panel-solid)"
                stroke-width="2"
                vector-effect="non-scaling-stroke"
            ></circle>
        `);

        rows.push(`
            <div class="tooltip-row">
                <span class="tooltip-name">
                    <span
                        class="tooltip-dot"
                        style="background: ${color}"
                    ></span>
                    ${escapeHtml(windowLabel(item.window_seconds))}
                </span>

                <strong>
                    ${escapeHtml(formatPercent(point[1]))}%
                </strong>
            </div>
        `);
    });

    hoverLayer.innerHTML = `
        <line
            x1="${svgX}"
            x2="${svgX}"
            y1="${PLOT_TOP}"
            y2="${PLOT_BOTTOM}"
            stroke="var(--muted)"
            stroke-opacity="0.42"
            stroke-width="1"
            stroke-dasharray="4 4"
            vector-effect="non-scaling-stroke"
        ></line>

        ${circles.join("")}
    `;

    tooltip.innerHTML = `
        <div class="tooltip-time">
            ${escapeHtml(formatDateTime(targetTimestamp))}
        </div>
        ${rows.join("")}
    `;

    tooltip.style.display = "block";

    const wrapRect = chartWrap.getBoundingClientRect();

    let left = event.clientX - wrapRect.left + 14;
    let top = event.clientY - wrapRect.top - tooltip.offsetHeight / 2;

    if (left + tooltip.offsetWidth > wrapRect.width - 8) {
        left =
            event.clientX -
            wrapRect.left -
            tooltip.offsetWidth -
            14;
    }

    top = Math.max(
        4,
        Math.min(
            wrapRect.height - tooltip.offsetHeight - 4,
            top,
        ),
    );

    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}

async function fetchJson(url) {
    const response = await fetch(url, {
        cache: "no-store",
    });

    if (!response.ok) {
        throw new Error(`${url}: HTTP ${response.status}`);
    }

    return response.json();
}

async function refresh() {
    try {
        const [current, history, health] = await Promise.all([
            fetchJson("/api/current"),
            fetchJson(`/api/history?hours=${selectedHours}`),
            fetchJson("/health"),
        ]);

        currentData = current;
        historyData = history;
        healthData = health;

        renderCards();
        renderHealth();
        renderChart();

    } catch (error) {
        console.error(error);

        status.classList.remove("ok", "degraded");
        status.classList.add("error");

        statusText.textContent = "Dashboard error";
    }
}

document.querySelectorAll(".range-buttons button").forEach((button) => {
    const hours = Number(button.dataset.hours);

    button.classList.toggle(
        "active",
        hours === selectedHours,
    );

    button.addEventListener("click", async () => {
        selectedHours = hours;
        localStorage.setItem(
            "quota-range-hours",
            String(selectedHours),
        );

        document
            .querySelectorAll(".range-buttons button")
            .forEach((candidate) => {
                candidate.classList.toggle(
                    "active",
                    candidate === button,
                );
            });

        try {
            historyData = await fetchJson(
                `/api/history?hours=${selectedHours}`,
            );

            renderChart();
        } catch (error) {
            console.error(error);
        }
    });
});

hitbox.addEventListener("mousemove", showTooltip);
hitbox.addEventListener("mouseleave", hideTooltip);

refresh();
setInterval(refresh, 60_000);
</script>
</body>
</html>
"""


class QuotaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        database: Database,
        sampler: Sampler,
    ) -> None:
        self.database = database
        self.sampler = sampler

        super().__init__(
            server_address,
            QuotaRequestHandler,
        )


class QuotaRequestHandler(BaseHTTPRequestHandler):
    server: QuotaHTTPServer

    def log_message(
        self,
        fmt: str,
        *args: Any,
    ) -> None:
        LOG.debug(
            "http %s - %s",
            self.address_string(),
            fmt % args,
        )

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        payload: Any,
        status: int = 200,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self._send_bytes(
            status,
            "application/json; charset=utf-8",
            body,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/":
                self._handle_index()
                return

            if parsed.path == "/api/current":
                self._handle_current()
                return

            if parsed.path == "/api/history":
                self._handle_history(parsed.query)
                return

            if parsed.path == "/health":
                self._handle_health()
                return

            self._send_json(
                {"error": "not_found"},
                status=404,
            )

        except BrokenPipeError:
            return

        except Exception:
            LOG.exception("unhandled HTTP request error")

            try:
                self._send_json(
                    {"error": "internal_server_error"},
                    status=500,
                )
            except Exception:
                pass

    def _handle_index(self) -> None:
        body = HTML.encode("utf-8")

        self._send_bytes(
            200,
            "text/html; charset=utf-8",
            body,
        )

    def _handle_current(self) -> None:
        self._send_json(
            {
                "now": int(time.time()),
                "samples": self.server.database.current(),
            }
        )

    def _handle_history(self, query_string: str) -> None:
        query = parse_qs(query_string)

        raw_hours = query.get("hours", ["168"])[0]

        try:
            hours = float(raw_hours)
        except ValueError:
            self._send_json(
                {"error": "invalid_hours"},
                status=400,
            )
            return

        # Protect the local server against accidentally huge queries while
        # still allowing up to one year of history through the API.
        if not 0.25 <= hours <= 24 * 366:
            self._send_json(
                {
                    "error": "hours_out_of_range",
                    "min_hours": 0.25,
                    "max_hours": 24 * 366,
                },
                status=400,
            )
            return

        to_timestamp = int(time.time())
        from_timestamp = to_timestamp - int(hours * 3600)

        self._send_json(
            {
                "from": from_timestamp,
                "to": to_timestamp,
                "hours": hours,
                "series": self.server.database.history(
                    from_timestamp,
                    to_timestamp,
                ),
            }
        )

    def _handle_health(self) -> None:
        sampler_state = self.server.sampler.status()

        now = int(time.time())
        last_success = sampler_state.get("last_success_at")
        last_error = sampler_state.get("last_error")
        failures = int(
            sampler_state.get("consecutive_failures") or 0
        )

        if last_success is None:
            status = "error" if last_error else "starting"

        else:
            age = now - int(last_success)

            stale_after = max(
                180,
                int(self.server.sampler.interval * 3),
            )

            if failures > 0 or age > stale_after:
                status = "degraded"
            else:
                status = "ok"

        self._send_json(
            {
                "status": status,
                "now": now,
                "interval_seconds": self.server.sampler.interval,
                "last_attempt_at": sampler_state.get("last_attempt_at"),
                "last_success_at": sampler_state.get("last_success_at"),
                "last_error": last_error,
                "consecutive_failures": failures,
                "last_sample_count": sampler_state.get("last_sample_count"),
                "rows": self.server.database.row_count(),
            }
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record Codex quota history in SQLite and serve a local dashboard."
        )
    )

    parser.add_argument(
        "--auth",
        type=Path,
        default=DEFAULT_AUTH_PATH,
        help=f"Codex auth.json path (default: {DEFAULT_AUTH_PATH})",
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )

    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"HTTP bind address (default: {DEFAULT_HOST})",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP port (default: {DEFAULT_PORT})",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Polling interval in seconds (default: {DEFAULT_INTERVAL:g})",
    )

    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity (default: INFO)",
    )

    args = parser.parse_args()

    args.auth = args.auth.expanduser()
    args.db = args.db.expanduser()

    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    return args


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    database = Database(args.db)
    database.initialize()

    LOG.info("database: %s", database.path.resolve())
    LOG.info("auth file: %s", args.auth)

    def compact_database() -> None:
        try:
            stats = database.compact_identical_runs(
                expected_interval=args.interval,
            )
        except Exception:
            LOG.exception("quota history compaction failed")
            return

        if stats["new_rows"] == 0:
            LOG.debug("quota history compaction: nothing new")
            return

        LOG.info(
            "quota history compacted: windows=%d new_rows=%d deleted=%d "
            "max_gap=%ds",
            stats["windows"],
            stats["new_rows"],
            stats["deleted_rows"],
            stats["max_gap_seconds"],
        )

    LOG.info("running startup quota history compaction")
    compact_database()

    client = QuotaClient(
        auth_path=args.auth,
    )

    stop_event = threading.Event()

    sampler = Sampler(
        client=client,
        database=database,
        interval=args.interval,
        stop_event=stop_event,
    )

    try:
        server = QuotaHTTPServer(
            (args.host, args.port),
            database=database,
            sampler=sampler,
        )
    except OSError as exc:
        LOG.error(
            "unable to bind HTTP server on %s:%s: %s",
            args.host,
            args.port,
            exc,
        )
        return 1

    sampler_thread = threading.Thread(
        target=sampler.run,
        name="quota-sampler",
        daemon=False,
    )

    http_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="quota-http",
        daemon=False,
    )

    def maintenance_loop() -> None:
        while not stop_event.wait(COMPACTION_PERIOD_SECONDS):
            compact_database()

    maintenance_thread = threading.Thread(
        target=maintenance_loop,
        name="quota-maintenance",
        daemon=False,
    )

    def request_shutdown(
        signum: int,
        _frame: Any,
    ) -> None:
        LOG.info("received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    sampler_thread.start()
    http_thread.start()
    maintenance_thread.start()

    LOG.info(
        "dashboard: http://%s:%s",
        args.host,
        args.port,
    )
    LOG.info(
        "sampling interval: %.1f seconds",
        args.interval,
    )

    try:
        while not stop_event.wait(3600):
            pass

    except KeyboardInterrupt:
        stop_event.set()

    finally:
        stop_event.set()

        server.shutdown()
        server.server_close()

        http_thread.join(timeout=5)
        maintenance_thread.join(timeout=5)
        sampler_thread.join(timeout=max(5.0, args.interval + 2.0))

        LOG.info("stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
