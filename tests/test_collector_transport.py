"""Exercise HTTP transport through an edge that rejects library-default clients."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("transport_worker", ROOT / "collector/worker.py")
hermes = load("transport_hermes", ROOT / "collector/hermes/madison_common.py")


class EdgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.received.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))
        ua = self.headers.get("User-Agent", "")
        blocked = self.server.block_all or not ua or ua.startswith("Python-urllib/")
        status = 403 if blocked else (200 if self.headers.get("Authorization") == "Bearer test-token" else 401)
        body = b"error code: 1010" if blocked else b'{"ok":true,"status":"matched"}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("cf-ray", "test-ray")
        self.end_headers()
        self.wfile.write(body)

    do_GET = handle_request
    do_POST = handle_request


class CollectorTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), EdgeHandler)
        self.server.received = []
        self.server.block_all = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / "env").write_text(f"MADISON_URL={self.url}\nMADISON_TOKEN=test-token\n")
        (self.directory / "version.json").write_text((ROOT / "collector/version.json").read_text())
        self.worker = worker.Worker(self.directory)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_worker_get_and_post_pass_edge_with_application_identity(self):
        # Reproduce the old failure before exercising the actual worker transport.
        req = urllib.request.Request(self.url + "/api/collector/status", headers={"Authorization": "Bearer test-token"})
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(req)
        self.assertEqual(blocked.exception.code, 403)
        blocked.exception.close()
        self.assertTrue(self.worker.request("/api/collector/status")["ok"])
        payload = {"action": "installed", "version": self.worker.release["version"]}
        self.assertTrue(self.worker.request("/api/collector/status", payload)["ok"])
        get, post = self.server.received[-2:]
        self.assertEqual((get[0], post[0]), ("GET", "POST"))
        self.assertTrue(get[2]["User-Agent"].startswith("MADISON-Collector/"))
        self.assertEqual(post[3], payload)

    def test_failed_end_stays_queued_with_diagnostics_and_recovers(self):
        with patch.object(worker, "frontend", return_value="cli"):
            self.worker.enqueue("claude-code", {"session_id": "test-session"})
        self.server.block_all = True
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.worker.run()
        self.assertIn("HTTP 403", output.getvalue())
        self.assertIn("test-ray", output.getvalue())
        self.assertNotIn("test-token", output.getvalue())
        self.assertEqual(len(list(self.worker.queue.glob("*.json"))), 1)
        event_id = self.server.received[-1][3]["event_id"]
        self.server.block_all = False
        self.worker.run()
        self.assertEqual(list(self.worker.queue.glob("*.json")), [])
        events = [r[3] for r in self.server.received if r[1] == "/api/events"]
        self.assertEqual([e["event_id"] for e in events], [event_id, event_id])

    def test_hermes_transport_also_identifies_the_application(self):
        payload = {"agent": "hermes", "event_id": "test-event"}
        with patch.object(hermes, "URL", self.url), patch.object(hermes, "TOKEN", "test-token"):
            hermes._post(payload)
        request = self.server.received[-1]
        self.assertEqual(request[1], "/api/events")
        self.assertEqual(request[2]["User-Agent"], "MADISON-Hermes")
        self.assertEqual(request[3], payload)
