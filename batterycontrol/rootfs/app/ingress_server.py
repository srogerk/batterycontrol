#!/usr/bin/env python3
"""
Ingress-server för BatteryControl-add-onen.

Ersätter scp-steget: dashboarden skrivs till /config/www/batterycontrol/ (för
/local/-åtkomst) OCH serveras här på port 8099 så att den syns i sidopanelen
via HA:s ingress-proxy. Ingen publik URL behövs.

Läser den färdiggenererade web/index.html ur /app/web (eller /data om
generate_web skrivit dit). Faller tillbaka på en enkel statussida om
dashboarden ännu inte genererats.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("INGRESS_PORT", "8099"))

CANDIDATES = [
    "/app/web/index.html",
    "/data/index.html",
    os.path.join(os.environ.get("CONFIG_DIR", "/config"), "www", "batterycontrol", "index.html"),
]


def find_dashboard():
    for path in CANDIDATES:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read(), path
            except OSError:
                continue
    return None, None


PLACEHOLDER = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<title>BatteryControl</title>
<style>
 body{font-family:system-ui,sans-serif;background:#11151c;color:#e6edf3;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .box{text-align:center;max-width:32rem;padding:2rem}
 h1{font-size:1.4rem;font-weight:600;margin:0 0 .75rem}
 p{color:#8b949e;line-height:1.6;margin:.4rem 0}
 code{background:#1c2430;padding:.15rem .4rem;border-radius:4px;font-size:.9em}
</style></head><body><div class="box">
<h1>BatteryControl</h1>
<p>Dashboarden är inte genererad ännu.</p>
<p>Den skapas vid första kvartsoptimeringen. Kontrollera add-on-loggen om
meddelandet ligger kvar efter några minuter.</p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body, source = find_dashboard()
            if body is None:
                body = PLACEHOLDER
                source = "placeholder"
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Source", str(source))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/health":
            _, source = find_dashboard()
            payload = json.dumps({"ok": True, "dashboard": source}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # Statiska filer ur /app/web (t.ex. data.json)
        name = os.path.basename(self.path)
        for base in ("/app/web", "/data"):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate) and ".." not in self.path:
                try:
                    with open(candidate, "rb") as f:
                        payload = f.read()
                except OSError:
                    break
                ctype = "application/json" if name.endswith(".json") else "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

        self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[ingress] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[ingress] lyssnar på 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
