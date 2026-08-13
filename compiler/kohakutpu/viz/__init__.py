"""The playground: a kernel in, every level out.

`playground.compile_kernel` is the whole surface; `playground.html` is the page
that calls it and `python -m kohakutpu.viz` serves both.
"""

from kohakutpu.viz.playground import PAGE, compile_kernel, serve

__all__ = ["PAGE", "compile_kernel", "serve"]
