"""Tests for the live session monitor."""
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch


_SCRIPT_DIR = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "session_metrics_monitor", _SCRIPT_DIR / "_monitor.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("could not load the session monitor module")
_monitor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_monitor)


class BuildStatusTests(unittest.TestCase):
    def test_reports_session_activity_without_reading_file_contents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "-work-demo"
            project.mkdir()
            session_file = project / "session-123.jsonl"
            session_file.write_text("not parsed by monitor", encoding="utf-8")
            session_file.touch()
            now = session_file.stat().st_mtime + 30

            status = _monitor.build_status(root, now=now)

        self.assertEqual(status["status"], "healthy")
        self.assertEqual(status["active_sessions"], 1)
        self.assertEqual(status["sessions"][0]["session_id"], "session-123")
        self.assertEqual(status["sessions"][0]["status"], "active")
        self.assertNotIn("not parsed by monitor", json.dumps(status))

    def test_classifies_recent_and_idle_session_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            recent = project / "recent.jsonl"
            idle = project / "idle.jsonl"
            recent.touch()
            idle.touch()
            now = max(recent.stat().st_mtime, idle.stat().st_mtime) + 3600
            import os
            os.utime(recent, (now - 600, now - 600))
            os.utime(idle, (now - 3600, now - 3600))

            status = _monitor.build_status(root, now=now)

        by_id = {session["session_id"]: session for session in status["sessions"]}
        self.assertEqual(by_id["recent"]["status"], "recent")
        self.assertEqual(by_id["idle"]["status"], "idle")
        self.assertEqual(status["recent_sessions"], 1)
        self.assertEqual(status["projects"][0]["status"], "recent")

    def test_missing_projects_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            status = _monitor.build_status(missing, now=0)

        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(status["checks"][0]["status"], "unavailable")

    def test_ignores_directories_with_jsonl_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            (project / "not-a-session.jsonl").mkdir()
            (project / "session.jsonl").touch()

            status = _monitor.build_status(Path(temp_dir), now=0)

        self.assertEqual(status["session_count"], 1)
        self.assertEqual(status["sessions"][0]["session_id"], "session")

    def test_rejects_invalid_activity_thresholds(self):
        with self.assertRaises(ValueError):
            _monitor.build_status(Path("."), active_after_seconds=-1)
        with self.assertRaises(ValueError):
            _monitor.build_status(
                Path("."), active_after_seconds=10, recent_after_seconds=9
            )
        with self.assertRaises(ValueError):
            _monitor.build_status(Path("."), active_after_seconds=True)
        with self.assertRaises(ValueError):
            _monitor.build_status(Path("."), recent_after_seconds=1.5)
        with self.assertRaises(ValueError):
            _monitor.build_status(Path("."), now=float("nan"))


class MonitorServerTests(unittest.TestCase):
    def request(self, base_url, route):
        with urlopen(f"{base_url}{route}", timeout=3) as response:
            return response.status, response.headers, response.read()

    def test_serves_dashboard_status_api_and_health_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            (project / "session.jsonl").touch()
            server = _monitor.create_monitor_server(
                Path(temp_dir), port=0, refresh_seconds=2
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                page_status, page_headers, page = self.request(base_url, "/")
                api_status, api_headers, api_body = self.request(
                    base_url, "/api/status"
                )
                health_status, _, health_body = self.request(base_url, "/healthz")
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()

        self.assertEqual(page_status, 200)
        self.assertIn("text/html", page_headers["Content-Type"])
        self.assertIn(b"setInterval(refresh", page)
        self.assertIn("no-store", page_headers["Cache-Control"])
        self.assertEqual(api_status, 200)
        self.assertIn("application/json", api_headers["Content-Type"])
        self.assertEqual(json.loads(api_body)["session_count"], 1)
        self.assertEqual(health_status, 200)
        self.assertEqual(json.loads(health_body)["status"], "healthy")

    def test_health_endpoint_returns_service_unavailable_for_missing_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = _monitor.create_monitor_server(
                Path(temp_dir) / "missing", port=0
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(
                        f"http://127.0.0.1:{server.server_port}/healthz",
                        timeout=3,
                    )
                body = error.exception.read()
                error.exception.close()
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()

        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.loads(body)["status"], "unavailable")

    def test_health_endpoint_returns_service_unavailable_when_degraded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = _monitor.create_monitor_server(Path(temp_dir), port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.object(
                    _monitor, "build_status", return_value={"status": "degraded"}
                ):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(
                            f"http://127.0.0.1:{server.server_port}/healthz",
                            timeout=3,
                        )
                    body = error.exception.read()
                    error.exception.close()
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()

        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.loads(body)["status"], "degraded")

    def test_unknown_routes_return_404(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = _monitor.create_monitor_server(Path(temp_dir), port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(
                        f"http://127.0.0.1:{server.server_port}/private",
                        timeout=3,
                    )
                error.exception.close()
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()

        self.assertEqual(error.exception.code, 404)

    def test_rejects_invalid_server_options(self):
        with self.assertRaises(ValueError):
            _monitor.create_monitor_server(Path("."), port=65536)
        with self.assertRaises(ValueError):
            _monitor.create_monitor_server(Path("."), refresh_seconds=301)
        with self.assertRaises(ValueError):
            _monitor.create_monitor_server(Path("."), port=1.5)
        with self.assertRaises(ValueError):
            _monitor.create_monitor_server(Path("."), refresh_seconds=True)


if __name__ == "__main__":
    unittest.main()
