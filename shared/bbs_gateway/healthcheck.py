"""Container health check: the SSH port answers with an SSH banner."""

import os
import socket
import sys

port = int(os.environ.get("BBS_LISTEN_PORT", "2222"))
try:
    with socket.create_connection(("127.0.0.1", port), timeout=4) as s:
        ok = s.recv(64).startswith(b"SSH-2.0-")
except OSError:
    ok = False
sys.exit(0 if ok else 1)
