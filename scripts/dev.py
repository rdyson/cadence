#!/usr/bin/env python3
"""
cadence local dev server
Serves the frontend on localhost with a mock API that stores state in a local JSON file.
No AWS credentials needed. Great for iterating on CSS/JS/HTML.

Usage: python scripts/dev.py [--port 8000] [--config cadence.yaml]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

STATE_FILE = ".dev-state.json"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


class DevHandler(SimpleHTTPRequestHandler):
    """Serves frontend/ for static files and mocks the API for /state."""

    def __init__(self, *args, config: dict | None = None, **kwargs):
        self.config = config or {}
        super().__init__(*args, directory="frontend", **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.endswith("/state"):
            self._handle_get_state()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.endswith("/state"):
            self._handle_post_state()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self._cors_response(200, {})

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _cors_response(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_get_state(self):
        state = load_state()
        self._cors_response(200, {"users": state})

    def _handle_post_state(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        item = body.get("item")
        checked = body.get("checked")

        if item is None or checked is None:
            self._cors_response(400, {"error": "Missing 'item' or 'checked'"})
            return

        # In dev mode, use the first user's email if no auth
        auth = self.headers.get("Authorization", "")
        users = self.config.get("users", [])
        # Try to extract email from token, fall back to first user
        email = users[0]["email"] if users else "dev@localhost"

        # Decode JWT if present (no validation — it's dev mode)
        if auth.startswith("Bearer ") or auth.startswith("bearer "):
            token = auth.split(" ", 1)[1]
            try:
                import base64
                # JWT has 3 parts, payload is the second
                payload = token.split(".")[1]
                # Add padding
                payload += "=" * (4 - len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                email = claims.get("email", email)
            except Exception:
                pass

        state = load_state()
        user_state = state.get(email, {})

        if checked:
            user_state[item] = True
        else:
            user_state.pop(item, None)

        state[email] = user_state
        save_state(state)

        self._cors_response(200, {"ok": True})

    def log_message(self, format, *args):
        # Coloured log: green for 2xx, yellow for 3xx/4xx, red for 5xx
        status = args[1] if len(args) > 1 else ""
        if str(status).startswith("2"):
            colour = "\033[32m"
        elif str(status).startswith(("3", "4")):
            colour = "\033[33m"
        else:
            colour = "\033[31m"
        reset = "\033[0m"
        sys.stderr.write(f"  {colour}{args[0]}{reset} {status}\n")


def build_cadence_json(config_path: str) -> None:
    """Build cadence.json, injecting a dev-mode API URL."""
    print("▶ Building cadence.json...")
    subprocess.run(
        [sys.executable, "scripts/build.py", "--config", config_path],
        check=True,
    )


def patch_cadence_json_for_dev(port: int) -> None:
    """Patch the built cadence.json to point API URL at localhost."""
    path = Path("frontend/cadence.json")
    with open(path) as f:
        data = json.load(f)

    data["aws"]["api_url"] = f"http://localhost:{port}"
    # Remove cognito config so the app skips real auth
    data["_dev_mode"] = True

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Cadence local dev server")
    parser.add_argument("--port", "-p", type=int, default=8000)
    parser.add_argument("--config", "-c", default="cadence.yaml")
    parser.add_argument("--skip-build", action="store_true", help="Don't rebuild cadence.json")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Error: {args.config} not found")
        sys.exit(1)

    import yaml
    with open(args.config) as f:
        config = yaml.safe_load(f)

    if not args.skip_build:
        build_cadence_json(args.config)

    patch_cadence_json_for_dev(args.port)

    handler = partial(DevHandler, config=config)
    httpd = HTTPServer(("", args.port), handler)

    print(f"\n🪿 Cadence dev server running at http://localhost:{args.port}")
    print(f"   Serving frontend/ with mock API")
    print(f"   State file: {STATE_FILE}")
    print(f"   Users: {', '.join(u['name'] for u in config.get('users', []))}")
    print(f"\n   Press Ctrl+C to stop\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n   Stopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
