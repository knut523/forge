#!/usr/bin/env python3
"""forge <-> Claude Code bridge.

Exposes the Claude Code CLI already installed and authenticated in this
container as a plain completion endpoint, so forge can use "this session" as an
engine without holding any API key of its own.

Two deliberate restrictions:

* **No tools.** Every tool is passed to --disallowed-tools, so this endpoint can
  only produce text. forge does its own file writing inside its own workspace;
  an endpoint that could also run Bash here would be a remote shell wearing a
  different hat, in a container that mounts the docker socket.
* **Bearer token required**, and it binds inside the container only. Anything
  that can reach it can spend the session's quota, so it is not left open.
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ.get("SHIM_TOKEN", "")
CLAUDE = os.environ.get("CLAUDE_BIN", "/usr/local/bin/claude")
PORT = int(os.environ.get("SHIM_PORT", "8930"))

# Text generation only. The engine that consumes this writes files itself.
BLOCKED = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch",
           "WebSearch", "Task", "NotebookEdit", "MultiEdit", "TodoWrite"]


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "claude": os.path.exists(CLAUDE)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not TOKEN or self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            return self._json(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "prompt required"})

        # The prompt goes on STDIN, not argv: a large review context (a whole PR
        # diff) is hundreds of KB and would overflow ARG_MAX ("Argument list too
        # long") if passed as `-p <prompt>`. `claude -p` with no positional prompt
        # reads the prompt from stdin.
        cmd = [CLAUDE, "-p", "--disallowed-tools", *BLOCKED]
        if body.get("system"):
            cmd += ["--append-system-prompt", str(body["system"])]
        if body.get("model"):
            cmd += ["--model", str(body["model"])]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=int(body.get("timeout", 600)),
                               env={**os.environ, "HOME": "/home/coder"})
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "claude timed out"})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if r.returncode != 0:
            return self._json(502, {"error": (r.stderr or r.stdout or "")[-800:]})
        return self._json(200, {"text": r.stdout})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
