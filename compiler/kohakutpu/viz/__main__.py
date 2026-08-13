"""`python -m kohakutpu.viz` -- serve the playground.

With a file argument it renders that kernel's panels to stdout instead, which
is the same report without a browser.
"""

import sys

from kohakutpu.viz.playground import render, serve

if len(sys.argv) > 1 and not sys.argv[1].isdigit():
    print(render(open(sys.argv[1], encoding="utf-8").read()))  # noqa: SIM115
else:
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8017)
