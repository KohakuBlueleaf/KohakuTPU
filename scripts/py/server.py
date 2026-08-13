"""A local web front end for the live simulator.

    python server.py            then open http://127.0.0.1:8765

The interesting part is not the HTTP -- it is that a single elaborated xsim
snapshot stays warm behind it, so a request costs ~1 s instead of ~16 s. That
is the only reason a browser round trip makes sense here at all; without the
persistent session this would just be a slow form.

Standard library only, and the page is one self-contained file, so there is
nothing to install and nothing to serve from a CDN.
"""

import argparse
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ktpu.hw import bench, tensor
from ktpu.hw.sim import F_TARGET, MACS_PER_CLUSTER, MEASURED, Session, SimError

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE.parents[1] / "src" / "ktpu" / "viz" / "index.html"

session = None
session_lock = threading.Lock()


def get_session():
    """One session for the process, built on first use."""
    global session
    with session_lock:
        if session is None:
            s = Session()
            print("  elaborating (once) ...", flush=True)
            s.start()
            print("  simulator warm", flush=True)
            session = s
    return session


def limits():
    """The machine's capacities, and the truth about the built snapshot.

    ``snapshot_current`` is the one field that has to be ASKED rather than
    assumed. The page draws a warm/cold badge from it, and deriving that badge
    from the module's cluster count alone made it lie: a fresh process whose
    on-disk snapshot was elaborated for a different NCL showed "warm" and then
    took fifteen seconds. Answered without starting a simulator -- it is a
    stat of the snapshot against the sources.
    """
    probe = session or Session()
    return {
        "words": bench.WORDS,
        "ncmd": bench.NCMD,
        "stage_flits": bench.STAGE_FLITS,
        "tiles": bench.TILES,
        "l1_a": bench.L1_A_ENTRIES,
        "l1_b": bench.L1_B_ENTRIES,
        "l1_banks": bench.L1_BANKS,
        "max_host": bench.MAX_HOST,
        "preq": list(bench.PREQ),
        # The count the warm snapshot was built for, and the counts a request
        # may ask for. Anything else is a mesh mag_driver_tb cannot generate.
        "clusters": bench.NCLUSTERS,
        "cluster_counts": list(bench.CLUSTER_COUNTS),
        "snapshot_current": probe.snapshot_is_current(),
        # `mesh_ncl` is compiled into the snapshot, so changing it costs a cold
        # rebuild; `use` only picks which of those clusters are kicked.
        "mesh_ncl": bench.NCLUSTERS,
        "use_max": bench.NCLUSTERS,
        "packings": list(bench.PACKINGS),
        # From the layout module rather than repeated here, so a change to the
        # hardware's lane or block size cannot leave a stale copy behind.
        "lanes": tensor.LANES,
        "kblock": tensor.KBLOCK,
        "distributions": list(bench.DISTRIBUTIONS),
        "frequency": F_TARGET,
        "measured": MEASURED,
        # Lets the page estimate a run's length before it is paid for: `use`
        # divides the work but not the machine, so fewer clusters is slower.
        "macs_per_cluster": MACS_PER_CLUSTER,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter than the default
        # /api/plan answers on every keystroke, so logging it would bury the
        # runs, which are the only requests that cost anything.
        if "/api/run" in (args[0] if args else ""):
            print(f"  {args[0]}", flush=True)

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/limits":
            return self._send(200, json.dumps(limits()))
        return self._send(404, json.dumps({"error": "not found"}))

    def _request(self):
        """The shape and machine a POST body asks for, validated."""
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        dist = str(req.get("dist", "lowrank"))
        if dist not in bench.DISTRIBUTIONS:
            raise ValueError(
                f"unknown distribution {dist!r}; use one of "
                f"{list(bench.DISTRIBUTIONS)}"
            )
        # Which operands were quantised on upload. The answer must not depend
        # on it, so flipping it checks the pre-quantised fetch path.
        preq = tuple(bool(v) for v in req.get("preq", list(bench.PREQ)))
        if len(preq) != 2:
            raise ValueError("preq must be two booleans, [A, B]")
        packing = str(req.get("packing", "spread"))
        if packing not in bench.PACKINGS:
            raise ValueError(
                f"unknown packing {packing!r}; use one of {list(bench.PACKINGS)}"
            )
        ncl = int(req.get("ncl", bench.NCLUSTERS))
        return {
            # M, K, N -- the shape order the whole driver uses.
            "m": int(req.get("m", 16)),
            "k": int(req.get("k", 32)),
            "n": int(req.get("n", 16)),
            "seed": int(req.get("seed", 0xC0FFEE)),
            "dist": dist,
            # HOW MANY CLUSTERS THE MACHINE HAS. Compiled in, so a change costs
            # the cold elaboration; the page says so rather than looking hung.
            "ncl": ncl,
            # HOW MANY OF THEM THIS PROGRAM KICKS. Free -- the rest simply idle.
            "use": int(req.get("use", ncl)),
            "packing": packing,
            "preq": preq,
        }

    def do_POST(self):
        if self.path not in ("/api/run", "/api/plan"):
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            q = self._request()
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return self._send(400, json.dumps({"error": f"bad request: {e}"}))

        # No simulator: padding, tile, passes, rounds and footprint come from
        # the same functions the run uses, so a control can be priced first.
        if self.path == "/api/plan":
            return self._send(
                200,
                json.dumps(
                    # `dist` too: the FP16 output-range warning is a property of
                    # the generator as much as of the shape.
                    bench.preview(
                        q["m"],
                        q["k"],
                        q["n"],
                        q["ncl"],
                        q["preq"],
                        q["use"],
                        q["packing"],
                        q["dist"],
                    )
                ),
            )

        # Both counts: a mesh the bench does not build, or a dispatch subset
        # outside it, are wrong in ways the run itself cannot report.
        why = bench.check(
            q["m"], q["k"], q["n"], q["ncl"], q["preq"], q["use"], q["packing"]
        )
        if why:
            return self._send(400, json.dumps({"error": "; ".join(why)}))

        try:
            result = get_session().run(
                q["m"],
                q["k"],
                q["n"],
                q["seed"],
                dist=q["dist"],
                ncl=q["ncl"],
                preq=q["preq"],
                use=q["use"],
                packing=q["packing"],
            )
        except SimError as e:
            return self._send(500, json.dumps({"error": str(e)}))
        return self._send(200, json.dumps(result))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--warm", action="store_true", help="elaborate at startup, not on first request"
    )
    args = ap.parse_args()

    if not PAGE.exists():
        sys.exit(f"missing page: {PAGE}")
    if args.warm:
        get_session()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  KohakuTPU visualiser on http://{args.host}:{args.port}")
    print("  ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    main()
