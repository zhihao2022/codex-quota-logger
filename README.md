# Codex Quota Logger

A minimal Codex quota history logger and local dashboard.

It records the current Codex quota every 60 seconds into SQLite and exposes a small local web dashboard with:

- current remaining quota
- reset time
- historical remaining-quota curves
- 6-hour / 24-hour / 7-day / 30-day views
- hover tooltips
- automatic refresh

The entire application runs as one Python process.

There is no Flask, FastAPI, SQLAlchemy, Node.js, frontend build system, Chart.js, or persistent Codex app-server.

## Architecture

```text
~/.codex/auth.json
        │
        │ read only
        ▼
quota_logger.py
        │
        ├── every 60 seconds
        │
        ▼
https://chatgpt.com/backend-api/wham/usage
        │
        ▼
    SQLite
 data/quota.db
        │
        ▼
Python HTTP server
127.0.0.1:8765
        │
        ▼
embedded HTML/CSS/JS/SVG dashboard
```

The logger never modifies `~/.codex/auth.json`.

It only reads:

```text
tokens.access_token
tokens.account_id
```

and uses them to query the Codex usage endpoint.

## Requirements

The server needs:

```text
Python >= 3.10
uv
an existing Codex login in ~/.codex/auth.json
network access to chatgpt.com
```

No Python runtime dependencies are required beyond the standard library.

## Create the environment

From the repository directory:

```bash
uv sync
```

This creates:

```text
.venv/
```

and also creates or updates:

```text
uv.lock
```

Both the application and its environment are managed through uv.

Check the Python interpreter:

```bash
uv run python --version
```

## Run

Start the logger with:

```bash
uv run python quota_logger.py
```

Expected output looks similar to:

```text
2026-09-04 12:30:00 INFO    database: /path/to/codex-quota-logger/data/quota.db
2026-09-04 12:30:00 INFO    auth file: /home/zhihao/.codex/auth.json
2026-09-04 12:30:00 INFO    dashboard: http://127.0.0.1:8765
2026-09-04 12:30:00 INFO    sampling interval: 60.0 seconds
2026-09-04 12:30:01 INFO    sampled quota: 5-hour=90%, 7-day=89%
```

The first quota sample is collected immediately.

Subsequent samples are collected every 60 seconds.

## Dashboard

By default the server only listens on:

```text
127.0.0.1:8765
```

Open the dashboard on the server itself at:

```text
http://127.0.0.1:8765
```

The dashboard shows the current quota and historical remaining-quota curves.

The line chart is implemented with native SVG and JavaScript.

There are no external frontend dependencies or CDN requests.

## Access from another computer

The HTTP server intentionally binds only to localhost by default.

For a remote Linux server, create an SSH tunnel from your local machine:

```bash
ssh -L 8765:127.0.0.1:8765 <user>@<server>
```

Then open locally:

```text
http://127.0.0.1:8765
```

VS Code Remote SSH port forwarding can also forward remote port `8765`.

Do not bind this service directly to a public interface unless you understand the security implications.

## Keep it running after disconnecting SSH

A simple option is tmux.

Create a session:

```bash
tmux new -s codex-quota
```

Inside the tmux session:

```bash
cd /path/to/codex-quota-logger
uv run python quota_logger.py
```

Detach with:

```text
Ctrl+B
D
```

Later, reconnect with:

```bash
tmux attach -t codex-quota
```

The logger then continues running even when VS Code or SSH is disconnected.

The logger does not depend on the VS Code Codex extension process remaining alive.

## HTTP API

### Current quota

```bash
curl http://127.0.0.1:8765/api/current
```

Example:

```json
{
  "now": 1788496200,
  "samples": [
    {
      "sampled_at": 1788496190,
      "window_seconds": 18000,
      "used_percent": 10.0,
      "remaining_percent": 90.0,
      "resets_at": 1788507090
    },
    {
      "sampled_at": 1788496190,
      "window_seconds": 604800,
      "used_percent": 11.0,
      "remaining_percent": 89.0,
      "resets_at": 1788758995
    }
  ]
}
```

### Historical quota

Default history range is 7 days:

```bash
curl 'http://127.0.0.1:8765/api/history'
```

Specify a range in hours:

```bash
curl 'http://127.0.0.1:8765/api/history?hours=24'
```

The UI uses:

```text
6 hours
24 hours
168 hours
720 hours
```

for its four range buttons.

### Health

```bash
curl http://127.0.0.1:8765/health
```

Example:

```json
{
  "status": "ok",
  "now": 1788496200,
  "interval_seconds": 60.0,
  "last_attempt_at": 1788496190,
  "last_success_at": 1788496190,
  "last_error": null,
  "consecutive_failures": 0,
  "last_sample_count": 2,
  "rows": 42
}
```

Possible status values are:

```text
starting
ok
degraded
error
```

## Database

The database is created automatically at:

```text
data/quota.db
```

The schema is intentionally minimal:

```sql
CREATE TABLE quota_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at      INTEGER NOT NULL,
    window_seconds  INTEGER NOT NULL,
    used_percent    REAL NOT NULL,
    resets_at       INTEGER
);
```

For the current Codex limits:

```text
18000 seconds  = 5 hours
604800 seconds = 7 days
```

The database stores `used_percent`.

The frontend derives:

```text
remaining_percent = 100 - used_percent
```

This avoids storing redundant values.

SQLite uses WAL mode so that the sampler can write while the HTTP server reads historical data.

## Command-line options

Show all options:

```bash
uv run python quota_logger.py --help
```

Defaults:

```text
auth:       ~/.codex/auth.json
database:   data/quota.db
host:       127.0.0.1
port:       8765
interval:   60 seconds
log level:  INFO
```

Examples:

```bash
uv run python quota_logger.py --port 9000
```

```bash
uv run python quota_logger.py --interval 120
```

```bash
uv run python quota_logger.py --db /path/to/quota.db
```

```bash
uv run python quota_logger.py --log-level DEBUG
```

The minimum allowed polling interval is 5 seconds.

For normal usage, keep the default 60-second interval.

## Authentication behavior

The logger reads the current authentication data from:

```text
~/.codex/auth.json
```

on every quota request.

This means that if Codex refreshes `auth.json`, the logger automatically sees the new credentials on its next poll.

The logger never writes to or modifies `auth.json`.

If the server-side token eventually becomes invalid, the logger records a warning such as:

```text
Codex authentication rejected: HTTP 401
```

and continues retrying on the normal polling interval.

It does not start a Codex app-server and does not implement its own OAuth refresh flow.

If this occurs, use Codex normally on the server once so that the official Codex client can refresh its authentication state.

## Data retention

Every successful poll is stored.

Even if the quota value has not changed, a new sample is recorded every 60 seconds.

This makes the database a true time-series observation log and preserves:

- periods of inactivity
- quota consumption timing
- quota reset transitions
- periods where sampling was unavailable

There is currently no automatic data deletion.

At the default interval and two quota windows, the database grows slowly enough for long-term personal use.

## Security

Do not copy Codex authentication tokens into this repository.

The logger reads:

```text
~/.codex/auth.json
```

directly and keeps the credentials outside the Git repository.

The application never returns tokens through its HTTP API or dashboard.

The default HTTP bind address is:

```text
127.0.0.1
```

so the dashboard is not exposed directly to the network.

## Validation

Compile the script:

```bash
uv run python -m py_compile quota_logger.py
```

Start it:

```bash
uv run python quota_logger.py
```

From another terminal, verify health:

```bash
curl -s http://127.0.0.1:8765/health
```

Verify the current quota:

```bash
curl -s http://127.0.0.1:8765/api/current
```

Then open:

```text
http://127.0.0.1:8765
```

The first page load should already show the current 5-hour and 7-day quota if the first poll succeeded.
