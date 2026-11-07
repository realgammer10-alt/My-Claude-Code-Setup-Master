"""Local live monitor for Claude Code session JSONL activity."""
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit


_ACTIVE_AFTER_SECONDS = 120
_RECENT_AFTER_SECONDS = 900
_MAX_SESSIONS = 500
_MAX_REFRESH_SECONDS = 300


def build_status(
    projects_dir: Path,
    *,
    now: float | None = None,
    active_after_seconds: int = _ACTIVE_AFTER_SECONDS,
    recent_after_seconds: int = _RECENT_AFTER_SECONDS,
) -> dict:
    """Return file-activity and projects-directory health without reading logs."""
    if (
        isinstance(active_after_seconds, bool)
        or not isinstance(active_after_seconds, int)
    ):
        raise ValueError("active_after_seconds must be an integer")
    if (
        isinstance(recent_after_seconds, bool)
        or not isinstance(recent_after_seconds, int)
    ):
        raise ValueError("recent_after_seconds must be an integer")
    if active_after_seconds < 0:
        raise ValueError("active_after_seconds must be non-negative")
    if recent_after_seconds < active_after_seconds:
        raise ValueError("recent_after_seconds must be at least active_after_seconds")
    if now is not None:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("now must be a finite timestamp")
        try:
            is_finite_timestamp = math.isfinite(now)
        except OverflowError:
            is_finite_timestamp = False
        if not is_finite_timestamp:
            raise ValueError("now must be a finite timestamp")

    root = Path(projects_dir).expanduser()
    current_time = time.time() if now is None else now
    generated_at = datetime.fromtimestamp(current_time, tz=UTC).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    issues: list[str] = []
    sessions: list[dict] = []
    project_dirs: list[Path] = []

    try:
        if not root.is_dir():
            return _unavailable_status(generated_at)
        project_dirs = sorted(
            (entry for entry in root.iterdir()
             if not entry.name.startswith(".")
             and not entry.is_symlink()
             and entry.is_dir()),
            key=lambda entry: entry.name.casefold(),
        )
    except OSError:
        return _unavailable_status(generated_at)

    for project_dir in project_dirs:
        try:
            entries = list(project_dir.iterdir())
        except OSError:
            issues.append(f"Could not list session files for {project_dir.name}.")
            continue

        for path in entries:
            if path.suffix.lower() != ".jsonl" or path.is_symlink():
                continue
            try:
                file_stat = path.stat()
            except OSError:
                issues.append(f"Could not inspect a session file in {project_dir.name}.")
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue

            age_seconds = max(0, current_time - file_stat.st_mtime)
            if age_seconds <= active_after_seconds:
                activity = "active"
            elif age_seconds <= recent_after_seconds:
                activity = "recent"
            else:
                activity = "idle"
            modified_at = datetime.fromtimestamp(
                file_stat.st_mtime, tz=UTC
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            sessions.append({
                "project": project_dir.name,
                "session_id": path.stem,
                "status": activity,
                "last_activity": modified_at,
                "age_seconds": round(age_seconds),
                "size_bytes": file_stat.st_size,
            })

    sessions.sort(key=lambda session: session["last_activity"], reverse=True)
    total_sessions = len(sessions)
    active_sessions = sum(session["status"] == "active" for session in sessions)
    recent_sessions = sum(session["status"] != "idle" for session in sessions)

    project_summaries = []
    for project_dir in project_dirs:
        project_sessions = [
            session for session in sessions if session["project"] == project_dir.name
        ]
        if not project_sessions:
            continue
        latest = project_sessions[0]
        project_summaries.append({
            "project": project_dir.name,
            "session_count": len(project_sessions),
            "active_sessions": sum(
                session["status"] == "active" for session in project_sessions
            ),
            "status": (
                "active" if any(session["status"] == "active"
                                 for session in project_sessions)
                else latest["status"]
            ),
            "last_activity": latest["last_activity"],
        })
    project_summaries.sort(
        key=lambda project: project["last_activity"], reverse=True
    )

    overall_status = "degraded" if issues else "healthy"
    return {
        "status": overall_status,
        "generated_at": generated_at,
        "projects_scanned": len(project_dirs),
        "session_count": total_sessions,
        "active_sessions": active_sessions,
        "recent_sessions": recent_sessions,
        "truncated_sessions": max(0, total_sessions - _MAX_SESSIONS),
        "sessions": sessions[:_MAX_SESSIONS],
        "projects": project_summaries,
        "checks": [
            {
                "name": "projects_directory",
                "status": "healthy",
                "message": "Session projects directory is readable.",
            },
            {
                "name": "session_scan",
                "status": "degraded" if issues else "healthy",
                "message": (
                    "; ".join(issues[:10]) if issues
                    else "Session files are being monitored."
                ),
            },
        ],
    }


def _unavailable_status(generated_at: str) -> dict:
    return {
        "status": "unavailable",
        "generated_at": generated_at,
        "projects_scanned": 0,
        "session_count": 0,
        "active_sessions": 0,
        "recent_sessions": 0,
        "truncated_sessions": 0,
        "sessions": [],
        "projects": [],
        "checks": [
            {
                "name": "projects_directory",
                "status": "unavailable",
                "message": (
                    "Session projects directory is missing or cannot be read."
                ),
            },
            {
                "name": "session_scan",
                "status": "unavailable",
                "message": "No session files were scanned.",
            },
        ],
    }


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Claude Code Session Monitor</title>
  <style>
    :root { color-scheme: dark; font: 15px/1.5 system-ui, sans-serif; background: #10131b; color: #e8ebf3; }
    body { max-width: 1120px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
    h1 { margin: 0; font-size: 1.75rem; }
    .subtle, .muted { color: #a7afc2; }
    header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; margin-bottom: 1.5rem; }
    #overall { border: 1px solid #536078; border-radius: 999px; padding: .3rem .8rem; text-transform: uppercase; font-size: .75rem; font-weight: 700; }
    .healthy, .active { color: #86efac; } .recent { color: #fde68a; } .idle { color: #a7afc2; }
    .degraded, .unavailable, .error { color: #fca5a5; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .8rem; margin: 1.5rem 0; }
    .card, section { background: #191e2a; border: 1px solid #2b3344; border-radius: 12px; }
    .card { padding: 1rem; } .card strong { display: block; font-size: 1.7rem; }
    section { padding: 1rem; margin-top: 1rem; overflow-x: auto; }
    h2 { font-size: 1.1rem; margin: 0 0 .75rem; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th, td { padding: .65rem .5rem; border-bottom: 1px solid #2b3344; white-space: nowrap; }
    th { color: #a7afc2; font-size: .8rem; font-weight: 600; }
    tbody tr:last-child td { border-bottom: 0; }
    #error { color: #fca5a5; min-height: 1.5rem; }
    @media (max-width: 620px) { body { padding: 1.25rem .75rem; } header { flex-direction: column; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Claude Code Session Monitor</h1><div class="subtle">Read-only activity from local session file metadata</div></div>
    <div id="overall" class="muted">Connecting</div>
  </header>
  <div id="error" role="status" aria-live="polite"></div>
  <div class="cards">
    <div class="card"><span class="muted">Active sessions</span><strong id="active">—</strong></div>
    <div class="card"><span class="muted">Recent sessions</span><strong id="recent">—</strong></div>
    <div class="card"><span class="muted">Projects scanned</span><strong id="project-count">—</strong></div>
    <div class="card"><span class="muted">Session files</span><strong id="session-count">—</strong></div>
  </div>
  <section><h2>Health checks</h2><table><thead><tr><th>Check</th><th>Status</th><th>Details</th></tr></thead><tbody id="checks"></tbody></table></section>
  <section><h2>Recent sessions</h2><table><thead><tr><th>Project</th><th>Session</th><th>Status</th><th>Last activity (UTC)</th><th>Size</th></tr></thead><tbody id="sessions"></tbody></table><p id="truncated" class="muted"></p></section>
  <p class="subtle">Updated <time id="updated">—</time> · polling every __REFRESH_SECONDS__ seconds · session contents are never read or exposed.</p>
  <script>
    const refreshMs = __REFRESH_SECONDS__ * 1000;
    const byId = id => document.getElementById(id);
    function cell(row, value, className = "") {
      const td = document.createElement("td");
      td.textContent = value;
      if (className) td.className = className;
      row.appendChild(td);
    }
    function render(data) {
      byId("overall").textContent = data.status;
      byId("overall").className = data.status;
      byId("active").textContent = data.active_sessions;
      byId("recent").textContent = data.recent_sessions;
      byId("project-count").textContent = data.projects_scanned;
      byId("session-count").textContent = data.session_count;
      byId("updated").textContent = data.generated_at;
      byId("error").textContent = "";
      const checks = byId("checks");
      checks.replaceChildren();
      for (const check of data.checks) {
        const row = document.createElement("tr");
        cell(row, check.name);
        cell(row, check.status, check.status);
        cell(row, check.message);
        checks.appendChild(row);
      }
      const sessions = byId("sessions");
      sessions.replaceChildren();
      for (const session of data.sessions) {
        const row = document.createElement("tr");
        cell(row, session.project);
        cell(row, session.session_id);
        cell(row, session.status, session.status);
        cell(row, session.last_activity);
        cell(row, `${(session.size_bytes / 1024).toFixed(1)} KB`);
        sessions.appendChild(row);
      }
      byId("truncated").textContent = data.truncated_sessions
        ? `${data.truncated_sessions} older session files omitted from the table.`
        : "";
    }
    async function refresh() {
      try {
        const response = await fetch("/api/status", { cache: "no-store" });
        if (!response.ok) throw new Error(`Status API returned HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        byId("overall").textContent = "disconnected";
        byId("overall").className = "unavailable";
        byId("error").textContent = error.message;
      }
    }
    refresh();
    window.setInterval(refresh, refreshMs);
  </script>
</body>
</html>
"""


def create_monitor_server(
    projects_dir: Path,
    *,
    port: int = 8765,
    refresh_seconds: int = 5,
) -> ThreadingHTTPServer:
    """Create a loopback-only monitor server (port 0 is useful for tests)."""
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 <= port <= 65535
    ):
        raise ValueError("port must be between 0 and 65535")
    if (
        isinstance(refresh_seconds, bool)
        or not isinstance(refresh_seconds, int)
        or not 1 <= refresh_seconds <= _MAX_REFRESH_SECONDS
    ):
        raise ValueError(
            f"refresh_seconds must be between 1 and {_MAX_REFRESH_SECONDS}"
        )
    root = Path(projects_dir).expanduser()

    class MonitorRequestHandler(BaseHTTPRequestHandler):
        server_version = "SessionMetricsMonitor/1"
        sys_version = ""

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/":
                page = _DASHBOARD_HTML.replace(
                    "__REFRESH_SECONDS__", str(refresh_seconds)
                ).encode("utf-8")
                self._send_response(200, page, "text/html; charset=utf-8")
                return
            if route in ("/api/status", "/healthz"):
                status = build_status(root)
                payload = (
                    status if route == "/api/status"
                    else {"status": status["status"]}
                )
                response_code = (
                    200 if route == "/api/status" or status["status"] == "healthy"
                    else 503
                )
                self._send_response(
                    response_code,
                    json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self._send_response(
                404,
                b'{"error":"not found"}',
                "application/json; charset=utf-8",
            )

        def _send_response(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), MonitorRequestHandler)
    server.daemon_threads = True
    return server


def run_monitor(
    projects_dir: Path,
    *,
    port: int = 8765,
    refresh_seconds: int = 5,
) -> None:
    """Run the local monitor until interrupted."""
    server = create_monitor_server(
        projects_dir, port=port, refresh_seconds=refresh_seconds
    )
    print(
        f"Session monitor: http://127.0.0.1:{server.server_port}/ "
        f"(refreshes every {refresh_seconds}s; Ctrl+C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSession monitor stopped.")
    finally:
        server.server_close()
