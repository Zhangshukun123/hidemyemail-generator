import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BRIDGE_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hidemyemail_generator"
    / "openai_protocol_login_bridge.js"
)


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class ProtocolLoginBridgeTests(unittest.TestCase):
    def test_fetches_code_and_returns_session_without_exposing_code(self):
        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                received["path"] = self.path
                received["token"] = self.headers.get("X-Local-Token")
                received["body"] = json.loads(self.rfile.read(length))
                payload = json.dumps(
                    {
                        "ok": True,
                        "code": "384555",
                        "receivedAt": "2026-08-04T10:00:00+00:00",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                project = Path(temp_dir)
                services = project / "services"
                services.mkdir()
                (services / "chatgpt-service.js").write_text(
                    "module.exports = {\n"
                    "  login: async (account, fetchCode, onStatus) => {\n"
                    "    const emails = await fetchCode(account);\n"
                    "    const code = emails[0].bodyText;\n"
                    "    onStatus('verify_code', `submit code: ${code}`);\n"
                    "    return { accessToken: 'at-test', user: { email: account.email } };\n"
                    "  },\n"
                    "};\n",
                    encoding="utf-8",
                )
                env = os.environ.copy()
                env.update(
                    {
                        "HME_PROTOCOL_EMAIL": "bridge@icloud.com",
                        "HME_PROTOCOL_PASSWORD": "Secret!A7",
                        "HME_PROTOCOL_PROJECT_DIR": str(project),
                        "HME_CODE_SERVICE_URL": f"http://127.0.0.1:{server.server_port}",
                        "HME_CODE_SERVICE_TOKEN": "local-test-token",
                        "NO_PROXY": "127.0.0.1,localhost",
                    }
                )
                completed = subprocess.run(
                    [str(shutil.which("node")), str(BRIDGE_FILE)],
                    cwd=project,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=False,
                )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("384555", completed.stdout)
        events = [
            json.loads(line.removeprefix("HME_PROTOCOL_EVENT:"))
            for line in completed.stdout.splitlines()
            if line.startswith("HME_PROTOCOL_EVENT:")
        ]
        self.assertEqual(events[-1]["status"], "success")
        self.assertEqual(events[-1]["session"]["accessToken"], "at-test")
        self.assertEqual(received["path"], "/api/gpt-code")
        self.assertEqual(received["token"], "local-test-token")
        self.assertEqual(received["body"]["email"], "bridge@icloud.com")
        self.assertTrue(received["body"]["since"])


if __name__ == "__main__":
    unittest.main()
