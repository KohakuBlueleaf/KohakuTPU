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

from kohakutpu import bench, tensor
from kohakutpu.sim import F_TARGET, MACS_PER_CLUSTER, MEASURED, Session, SimError

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "viz" / "index.html"

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
        # THE MESH IS COMPILED IN; THE DISPATCH IS NOT. `mesh_ncl` is what the
        # warm snapshot was elaborated for and changing it costs a cold rebuild;
        # `use` picks how many of those clusters a program kicks and costs
        # nothing, because it is only which coordinates the kernel names. That
        # is what makes the cluster count a control instead of a rebuild.
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
        # Lets the page estimate a run's length BEFORE it is paid for. `use`
        # divides the work but not the simulated machine, so wall clock rises
        # as the cluster count falls -- one cluster doing the whole problem is
        # the slowest thing the page can be asked for, and it is one click away.
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
        # Which operands were quantised on upload. The ANSWER must not depend
        # on it, so flipping it in the browser is a direct check that the
        # pre-quantised fetch path is faithful and not just fast.
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
            "m": int(req.get("m", 16)),
            "n": int(req.get("n", 16)),
            "k": int(req.get("k", 32)),
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

        # NO SIMULATOR. What the shape becomes -- padding, tile, passes,
        # rounds, memory footprint -- answered from the same functions the run
        # uses, so the page can say what a control will do BEFORE it costs a
        # run. It used to be answerable only by running and reading the status
        # line afterwards, which at eight clusters meant a cold elaboration to
        # discover N had been padded from 16 to 1024.
        if self.path == "/api/plan":
            return self._send(
                200,
                json.dumps(
                    bench.preview(q["m"], q["n"], q["k"], q["ncl"], q["preq"],
                                  q["use"], q["packing"])
                ),
            )

        # Checked against the DISPATCHED count, not the mesh: it is `use` that
        # divides N and sizes each cluster's band, so the same shape can fit
        # when eight clusters run and overflow the bench RAM when two of the
        # same eight do. Validating against the mesh would reject legal shapes
        # and admit illegal ones.
        why = bench.check(q["m"], q["n"], q["k"], q["ncl"], q["preq"],
                          q["use"], q["packing"])
        if why:
            return self._send(400, json.dumps({"error": "; ".join(why)}))

        try:
            result = get_session().run(
                q["m"], q["n"], q["k"], q["seed"],
                dist=q["dist"], ncl=q["ncl"], preq=q["preq"],
                use=q["use"], packing=q["packing"],
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
