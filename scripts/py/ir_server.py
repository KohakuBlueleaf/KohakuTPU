"""The IR playground: write a kernel, see levels 1/2/3, and run it.

    python scripts/py/ir_server.py            # then open http://127.0.0.1:8770

Serves `ktpu/viz/playground.html` and one endpoint, `POST /api/compile`, which
takes `{"source": "..."}` and returns the rendered IR plus the numeric check.

**This executes the source it is sent**, which is what "write a kernel in the
browser" means. It binds to LOOPBACK ONLY and refuses anything else; do not put
it on a network and do not add a `--host` flag.
"""

import argparse
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import ktpu.target as target_mod
from ktpu.viz.ir import apply_target, compile_kernel

BASE = target_mod.VU13P_8CU

PAGE = ROOT / "src" / "ktpu" / "viz" / "playground.html"
MAX_SOURCE = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] not in ("/", "/index.html"):
            return self._send(404, b"not found", "text/plain")
        return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/compile":
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_SOURCE:
            return self._send(413, b"source too large", "text/plain")
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc:
            return self._send(400, str(exc).encode(), "text/plain")
        try:
            target = apply_target(BASE, req.get("target"))
        except ValueError as exc:
            return self._send(400, str(exc).encode(), "text/plain")
        out = compile_kernel(req.get("source", ""), target)
        return self._send(200, json.dumps(out).encode(), "application/json")

    def log_message(self, fmt, *args):
        """Quiet by default: one line per keystroke-triggered run is noise."""


def main() -> None:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument(
        "--target",
        default="VU13P_8CU",
        help="a named Target in ktpu.target, e.g. VU13P_MM8 or VU13P_VEC4",
    )
    ap.add_argument("--clusters", type=int, help="matmul clusters, overrides --target")
    ap.add_argument("--vector-cores", type=int, help="vector cores")
    ap.add_argument("--vector-lanes", type=int, help="ALUs per vector core")
    ap.add_argument("--mhz", type=float, help="clock, for the seconds figure")
    args = ap.parse_args()
    if not PAGE.exists():
        sys.exit(f"missing page: {PAGE}")

    named = getattr(target_mod, args.target, None)
    if not isinstance(named, target_mod.Target):
        have = [
            k for k, v in vars(target_mod).items() if isinstance(v, target_mod.Target)
        ]
        sys.exit(f"no target named {args.target!r}; have {', '.join(sorted(have))}")
    over = {
        k: v
        for k, v in (
            ("clusters", args.clusters),
            ("vector_cores", args.vector_cores),
            ("vector_lanes", args.vector_lanes),
            ("mhz", args.mhz),
        )
        if v is not None
    }
    BASE = apply_target(named, over)
    print(
        f"machine: {BASE.name}  {BASE.clusters} matmul / {BASE.vector_cores} vector"
        f"  {BASE.vector_lanes} lanes  {BASE.mhz:g} MHz"
    )
    # Loopback is not a default here, it is the security boundary: the endpoint
    # runs whatever Python it is handed.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ktpu IR playground  http://127.0.0.1:{args.port}   (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
