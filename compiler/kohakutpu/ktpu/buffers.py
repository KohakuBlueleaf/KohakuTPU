"""An L1 parameter's memory layout, from its type: what the host writes for an
input and how it reads an output back.

    f16[1024, 128] @dram                    row-major fp16
    mx7a[1024, 1024] @dram(gm=64, nk=2)     MXFP7, the clusters' A packing,
                                            tile-major (`matmul.pack_a`)
    mx7b[1024, 1024] @dram(gn=64, nk=2)     MXFP7, B packing (`matmul.pack_b`)
    f16[1024, 1024] @dram(tile=64x64)       drained sub-tiles: tiles of 64x64
                                            sub-tiles (256x256), row-major
                                            tiles, row-major sub-tiles in one
    mx7b[64, 1024] @dram(gn=16, nk=2, t=1)  any of these holding the transpose
                                            of the host's [1024, 64] value
"""

from dataclasses import dataclass, field

import numpy as np
from kohakuaccel.text.syntax import Call, Dims, Int, Kw, Name, Placed, View
from kohakutpu.hw import tensor as T
from kohakutpu.ir.l1.kernels import matmul as MM

DTYPES = {"f16": np.float16, "f32": np.float32}


@dataclass
class Buffer:
    dtype: str
    shape: tuple
    space: str = "dram"
    layout: dict = field(default_factory=dict)

    @classmethod
    def of(cls, t) -> "Buffer":
        space, layout = "dram", {}
        if isinstance(t, Placed):
            t, at = t.x, t.space
            if isinstance(at, Call):
                for a in at.args:
                    if not isinstance(a, Kw):
                        raise TypeError(f"a layout is NAME=VALUE, got {a!r}")
                    v = a.value
                    layout[a.name] = v.values if isinstance(v, Dims) else v.value
                at = Name(at.name)
            space = at.text
        if not isinstance(t, View) or not all(isinstance(d, Int) for d in t.slices):
            raise ValueError(f"an L1 buffer is DTYPE[constant dims], got {t!r}")
        b = cls(t.name, tuple(d.value for d in t.slices), space, layout)
        b.check()
        return b

    def check(self) -> None:
        want = {"mx7a": {"gm", "nk"}, "mx7b": {"gn", "nk"}}.get(self.dtype)
        if want is not None and set(self.layout) - {"t"} != want:
            raise ValueError(f"{self.dtype} wants @dram({', '.join(sorted(want))}=...)")
        if self.dtype not in ("mx7a", "mx7b", *DTYPES):
            raise ValueError(f"no buffer dtype {self.dtype}")
        if set(self.layout) - {"gm", "gn", "nk", "tile", "t"}:
            raise ValueError(f"layout keys: gm gn nk tile t; got {sorted(self.layout)}")
        if self.layout.get("t") and len(self.shape) != 2:
            raise ValueError("t=1 transposes a 2-D buffer")

    @property
    def host_dtype(self):
        """What the host's values are before packing: fp16 for MXFP7."""
        return np.float16 if self.dtype.startswith("mx7") else DTYPES[self.dtype]

    @property
    def host_shape(self) -> tuple:
        """The host value's shape: the buffer's, reversed under `t=1`."""
        return self.shape[::-1] if self.layout.get("t") else self.shape

    def pack(self, x) -> bytes:
        x = np.asarray(x, self.host_dtype).reshape(self.host_shape)
        if self.layout.get("t"):
            x = np.ascontiguousarray(x.T)
        if self.dtype == "mx7a":
            return MM.pack_a(x, self.layout["gm"], self.layout["nk"])
        if self.dtype == "mx7b":
            return MM.pack_b(x, self.layout["gn"], self.layout["nk"])
        if "tile" in self.layout:
            return self._tiled(x)
        return np.ascontiguousarray(x).tobytes()

    @property
    def nbytes(self) -> int:
        n = int(np.prod(self.shape))
        if self.dtype.startswith("mx7"):
            return n  # one entry (128 B) per 128 values
        return n * np.dtype(DTYPES[self.dtype]).itemsize

    def unpack(self, raw: bytes) -> np.ndarray:
        """float64 values in the host's shape."""
        out = self._unpack(raw)
        return out.T if self.layout.get("t") else out

    def _unpack(self, raw: bytes) -> np.ndarray:
        if self.dtype.startswith("mx7"):
            raise ValueError("an MXFP7 buffer is read back through its fp16 source")
        if "tile" in self.layout:
            gm, gn = self.layout["tile"]
            m, n = self.shape
            out = np.zeros((m, n))
            size = gm * gn * 32
            tn = n // (4 * gn)
            for t in range(len(raw) // size):
                words = [
                    int.from_bytes(raw[t * size + s : t * size + s + 32], "little")
                    for s in range(0, size, 32)
                ]
                i, j = divmod(t, tn)
                out[4 * gm * i : 4 * gm * (i + 1), 4 * gn * j : 4 * gn * (j + 1)] = (
                    T.unpack_c(words, 4 * gm, 4 * gn, gn)
                )
            return out
        return (
            np.frombuffer(raw, DTYPES[self.dtype])
            .astype(np.float64)
            .reshape(self.shape)
        )

    def _tiled(self, x) -> bytes:
        gm, gn = self.layout["tile"]
        m, n = self.shape
        tiles = x.reshape(m // (4 * gm), gm, 4, n // (4 * gn), gn, 4)
        return np.ascontiguousarray(tiles.transpose(0, 3, 1, 4, 2, 5)).tobytes()


__all__ = ["Buffer"]
