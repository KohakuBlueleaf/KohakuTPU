"""Optional input-boundary observer used by the perf recorder, inert otherwise.

The span includes input upload, execution and output readback at the workload's
existing boundary. The observer fences before/after it; never around each kernel.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager

#: Set by the recorder: `observer(index, group)` fences one input.
observer: Callable[..., AbstractContextManager] | None = None


@contextmanager
def span(index, group="input"):
    if observer is None:
        yield
    else:
        with observer(index, group):
            yield
