"""Create the Tacit dev GitHub App from infra/github/app-manifest.json.

    pnpm github:create-app [--org ORG]

1. Gets a smee.io channel so GitHub can deliver webhooks to localhost.
2. Opens a local page that submits the manifest to GitHub; you click "Create".
3. Exchanges the returned code for the App's id, secrets and private key, saves the key
   outside the repo, and writes the values into .env.
"""

import argparse
import json
import re
import secrets
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from githubkit import GitHub

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "github" / "app-manifest.json"
ENV = ROOT / ".env"
KEY_DIR = Path.home() / ".config" / "tacit"
PORT = 3999


def new_smee_channel() -> str:
    resp = httpx.head("https://smee.io/new", follow_redirects=False, timeout=15)
    url = resp.headers.get("location")
    if not url:
        raise SystemExit("Couldn't create a smee.io channel; pass --webhook-url instead.")
    return url


def set_env(values: dict[str, str]) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    ENV.write_text(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", help="Create the App under this org instead of your account")
    ap.add_argument("--webhook-url", help="Public webhook URL (default: a new smee.io channel)")
    args = ap.parse_args()

    webhook_proxy = args.webhook_url or new_smee_channel()
    state = secrets.token_urlsafe(16)
    manifest = json.loads(MANIFEST.read_text())
    manifest["hook_attributes"]["url"] = webhook_proxy
    manifest["redirect_url"] = f"http://localhost:{PORT}/callback"
    target = (
        f"https://github.com/organizations/{args.org}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, body: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/":
                self._send(
                    f"""<form id=f method=post action="{target}?state={state}">
                    <input type=hidden name=manifest value="{escape(json.dumps(manifest))}">
                    <button>Create the Tacit GitHub App</button></form>
                    <script>document.getElementById('f').submit()</script>"""
                )
            elif url.path == "/callback":
                q = parse_qs(url.query)
                if q.get("state", [""])[0] != state:
                    self._send("State mismatch. Close this tab and run the script again.")
                    return
                gh = GitHub()  # keep a reference; githubkit namespaces need a live client
                app = gh.rest.apps.create_from_manifest(q["code"][0]).json()
                result.update(app)
                self._send("Done! You can close this tab and go back to the terminal.")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Opening http://localhost:{PORT} ... (click 'Create GitHub App' on GitHub)")
    webbrowser.open(f"http://localhost:{PORT}")
    while not result:
        server.handle_request()

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key_path = KEY_DIR / f"github-app-{result['id']}.pem"
    key_path.write_text(result["pem"])
    key_path.chmod(0o600)
    set_env(
        {
            "GITHUB_APP_ID": str(result["id"]),
            "GITHUB_APP_SLUG": result["slug"],
            "GITHUB_APP_CLIENT_ID": result["client_id"],
            "GITHUB_APP_CLIENT_SECRET": result["client_secret"],
            "GITHUB_APP_PRIVATE_KEY_PATH": str(key_path),
            "GITHUB_WEBHOOK_SECRET": result["webhook_secret"],
            "GITHUB_WEBHOOK_PROXY_URL": webhook_proxy,
        }
    )
    print(f"Created {result['html_url']}")
    print(f"Private key saved to {key_path} (outside the repo). .env updated.")
    print("Next: restart dev servers, run `pnpm dev:webhooks` and `pnpm dev:worker`,")
    print("then link GitHub and click 'Connect GitHub repositories' in your workspace.")


if __name__ == "__main__":
    main()
