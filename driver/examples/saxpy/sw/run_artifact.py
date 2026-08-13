"""Execute a compiled artifact read from stdin, and check the arithmetic.

    python -m examples.saxpy.main --json | python -m examples.saxpy.sw.run_artifact

The compiler is not imported here and is not installed here. All that crosses is
JSON, which is the whole point of the artifact being plain data.
"""

import json
import struct
import sys

from kohakuaccel.runtime import execute
from kohakuaccel.sim import SimMachine

from .unit import SaxpyUnit

X_ADDR = 0x0001_0000
Y_ADDR = 0x0002_0000
ELEM = 4


def run(artifact: dict, n: int = 64, a: float = 2.5) -> dict:
    """Upload x and y, execute `artifact`, and report how much came out right."""
    x = [float(i) for i in range(n)]
    y = [float(100 - i) for i in range(n)]

    card = SimMachine(units={(1, 0): SaxpyUnit(), (2, 0): SaxpyUnit()})
    card.upload(X_ADDR, struct.pack(f"<{n}f", *x))
    card.upload(Y_ADDR, struct.pack(f"<{n}f", *y))

    code = execute(artifact, card)
    got = struct.unpack(f"<{n}f", card.download(Y_ADDR, n * ELEM))
    want = [a * xi + yi for xi, yi in zip(x, y)]
    wrong = [i for i, (g, w) in enumerate(zip(got, want)) if g != w]
    return {"code": code, "wrong": len(wrong), "n": n, "first_wrong": wrong[:1]}


def main() -> int:
    result = run(json.load(sys.stdin))
    print(json.dumps(result))
    return 0 if result["code"] == 0 and not result["wrong"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
