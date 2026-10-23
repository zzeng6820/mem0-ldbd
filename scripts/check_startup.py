"""Exercise the real server process without credentials or external API requests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="ldbd-mem0-smoke-") as temp:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, HOST="127.0.0.1", PORT=str(port),
               MEM0_API_KEY="", WRAPPER_API_KEY="startup-check-only",
               REQUEST_DB_PATH=str(Path(temp) / "requests.sqlite3"))
    with (Path(temp) / "server.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, str(root / "scripts/run.py")],
                                   cwd=root, env=env, stdout=log, stderr=log)
        try:
            base = f"http://127.0.0.1:{port}"
            for attempt in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Server exited before startup")
                try:
                    with urlopen(base + "/live", timeout=1) as response:
                        assert response.status == 200
                        assert json.load(response) == {"status": "ok"}
                    break
                except (URLError, OSError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Server did not start")
            try:
                urlopen(base + "/health", timeout=2)
                raise AssertionError("Unconfigured server must not be healthy")
            except HTTPError as error:
                assert error.code == 503
                assert json.load(error) == {"status": "not_configured"}
            print("STARTUP_OK: /live=200, /health=503 without Mem0 key; no external calls")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
