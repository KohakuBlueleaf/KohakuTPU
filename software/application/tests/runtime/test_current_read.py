"""Read-back of subviews follows the producer's current storage form."""

import numpy as np
import pytest

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as F
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView


@pytest.mark.parametrize("dtype", [np.float16, np.float32])
@pytest.mark.parametrize("rows", [False, True])
@pytest.mark.parametrize("first,n", [(0, 6), (1, 4), (4, 8), (9, 6)])
def test_subview_current_form(dtype, rows, first, n):
    dev = D.Device(kind="offline", board="multimesh_v9t6")
    values = np.arange(15, dtype=np.float16)
    item = np.dtype(dtype).itemsize
    st = Storage(dev, 15 * item)
    dev.write(st.addr, np.full(15, -99, dtype=dtype).tobytes())
    if rows:
        key = F.rows_key(5, 16)
        raw = np.full((3, 16), -11, dtype=np.float16)
        raw[:, :5] = values.reshape(3, 5)
    else:
        key, raw = F.F16, values
    form = st.form(key, raw.nbytes)
    dev.write(form.addr, raw.tobytes())
    st.wrote(key)
    view = StorageView(st, first * item, n * item)
    got = np.frombuffer(F.read(dev, view, dtype, n), dtype)
    np.testing.assert_array_equal(got, values[first : first + n].astype(dtype))
    # The old offset shortcut reads the deliberately stale primary allocation.
    if first:
        mutant = np.frombuffer(dev.read(st.addr + view.offset, n * item), dtype)
        assert not np.array_equal(mutant, got)
    assert FLAT not in st.valid
    st.free()
