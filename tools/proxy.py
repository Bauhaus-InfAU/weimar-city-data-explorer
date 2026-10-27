#!/usr/bin/env python3
"""
proxy.py — tiny CORS-adding reverse proxy for the WEGA 3D data host.

Thuringia publishes ready-made Cesium assets (3D Tiles buildings + quantized-mesh
terrain) at https://geoportal5.geoportal-th.de/wega-3d-data/ ... but that host
sends NO `Access-Control-Allow-Origin` header, so a browser page served from
localhost / GitHub Pages cannot fetch them directly.

This proxy forwards GET requests for that one path prefix and adds
`Access-Control-Allow-Origin: *`. It is a DEV convenience only — for a deployed
site, host an equivalent (e.g. a Cloudflare Worker) and point the app at it.

Run it next to the web server:
    python tools/proxy.py                 # listens on http://localhost:8788
Then in the browser the app uses e.g.
    http://localhost:8788/wega-3d-data/buildings/tileset.json

Stdlib only. Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import http.server
import sys
import urllib.error
import urllib.request

UPSTREAM = "https://geoportal5.geoportal-th.de"
ALLOWED_PREFIXES = ("/wega-3d-data/",)
COPY_HEADERS = ("Content-Type", "Content-Encoding", "Content-Length", "Last-Modified", "ETag")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self.path.startswith(ALLOWED_PREFIXES):
            self.send_response(403)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        url = UPSTREAM + self.path
        req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/proxy"})
        try:
            with urllib.request.urlopen(req, timeout=60) as up:
                body = up.read()
                self.send_response(up.status)
                for h in COPY_HEADERS:
                    v = up.headers.get(h)
                    if v and h.lower() != "content-length":
                        self.send_header(h, v)
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            self._cors()
            msg = f"proxy error: {e}".encode()
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, fmt, *args):
        sys.stderr.write("proxy: " + (fmt % args) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CORS proxy -> {UPSTREAM}  on  http://localhost:{args.port}{ALLOWED_PREFIXES[0]}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
