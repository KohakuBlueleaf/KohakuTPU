"""Direct predicate select: bitwise payloads and wrong-arm mutant."""

import json
import os
from pathlib import Path

import numpy as np

from application.tests.runtime.test_pipe_batch import model  # noqa: F401
from kohakutpu.stack import kernel as K


def test_predicate_select_bytes_and_mutant(model):  # noqa: F811
    dev = model
    recs = json.loads(Path(os.environ["KTPU_PIPE_PREDICATE_ARTIFACT"]).read_text())
    rng = np.random.default_rng(100)
    data = rng.integers(0, 65536, (16, 128), dtype=np.uint16)
    data[(data & 0x7C00) == 0x7C00] = 0x7BFF
    data[0, :4] = [0, 0x8000, 1, 0x8001]
    selector = np.resize(
        np.array([0, 1, -1, np.nan, np.inf, 0.5], np.float16), data.shape
    )
    expected_bits = np.where(selector == 1, data, np.uint16(0))
    expected_bits[(expected_bits & 0x7FFF) == 0] = 0
    expected = expected_bits.tobytes()
    x, s, y = dev.alloc(data.nbytes), dev.alloc(selector.nbytes), dev.alloc(data.nbytes)
    dev.write(x, data.tobytes())
    dev.write(s, selector.tobytes())
    for label in ("before", "after", "mutant"):
        c = K.compile(recs[label]["l1"], name="select", machine=dev.machine, level="l1")
        assert not c.verdict["refused"]
        dev.run(c, {"x": x, "selector": s, "y": y})
        got = dev.read(y, data.nbytes)
        assert (got == expected) == (label != "mutant"), label
    dev.free(x)
    dev.free(s)
    dev.free(y)
