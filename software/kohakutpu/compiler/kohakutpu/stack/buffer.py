"""Host buffer layout and packing through the Rust binding."""

from dataclasses import dataclass

import numpy as np

from kohakutpu.compiler import _rs
from kohakutpu.language.numerics import fp16
from kohakutpu.language.text.syntax import fmt

DTYPES = {
    "f16": np.float16,
    "f32": np.float32,
    "u8": np.uint8,
    "u32": np.uint32,
    "i32": np.int32,
    "u64": np.uint64,
}


@dataclass(frozen=True)
class Buffer:
    type_text: str
    machine_json: str
    facts: dict

    @classmethod
    def of(cls, ty, machine_json):
        text = ty if isinstance(ty, str) else fmt(ty)
        return cls(text, machine_json, _rs.buffer(text, machine_json))

    @property
    def nbytes(self):
        return self.facts["bytes"]

    @property
    def space(self):
        return self.facts["space"]

    @property
    def layout(self):
        return {"node": self.facts["node"]}

    @property
    def dtype(self):
        return self.facts["dtype"]

    @property
    def host_shape(self):
        return tuple(self.facts["host_shape"])

    @property
    def host_dtype(self):
        return DTYPES[self.facts["host_dtype"]]

    def pack(self, host):
        value = np.asarray(host)
        if self.host_dtype == np.float16 and value.dtype != np.float16:
            value = fp16.write(value)
        value = np.ascontiguousarray(value, dtype=self.host_dtype).reshape(
            self.host_shape
        )
        return _rs.buffer_pack(self.type_text, value.tobytes(), self.machine_json)

    def unpack(self, raw):
        data = _rs.buffer_unpack(self.type_text, raw, self.machine_json)
        return np.frombuffer(data, dtype="<f8").reshape(self.host_shape)
