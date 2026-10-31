"""Walk an example offline on the template library, prebuilding each miss: what a
compiler change leaves stale before the e2e tier runs `--library-only`.

    python software/tests/e2e/fill.py mnist_mlp.py --model lenet --steps 110 ...
    python software/tests/e2e/fill.py vit_tiny.py --blocks 12 ...
"""

import json
import pathlib
import runpy
import sys
import time

from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
LIBRARY = ROOT / "build" / "templates"
EXAMPLES = ROOT / "software" / "kohakutpu" / "application" / "examples" / "tinygrad"
ORIGINAL = D.Device.compile_graph
FILLED = []


def compile_graph(self, source):
    misses = self.counters["library_misses"]
    try:
        return ORIGINAL(self, source)
    except RuntimeError:
        self.counters["library_misses"] = misses
        t0 = time.time()
        rs.library_prebuild(LIBRARY, source, rs.machine_json(self.machine))
        FILLED.append((json.loads(source)["name"], round(time.time() - t0, 1)))
        print("filled", len(FILLED), FILLED[-1], flush=True)
        return ORIGINAL(self, source)


class Offline(D.Device):
    def __init__(self, kind="offline", *args, **kw):
        kw.pop("transport", None)
        super().__init__("offline", *args, **kw)


def main():
    script = str(EXAMPLES / sys.argv[1])
    D.Device.compile_graph = compile_graph
    D.Device = Offline
    sys.argv = [script, *sys.argv[2:]]
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        print("filled", len(FILLED), sum(t for _, t in FILLED), "s", flush=True)


if __name__ == "__main__":
    main()
