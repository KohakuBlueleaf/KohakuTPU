"""Machine-bound Rust package reader for the framework's callback interpreter."""

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


class Reader:
    def __init__(self, machine_json=None, mesh=0):
        self.machine_json = machine_json or (
            MF.MACHINES / "multimesh_v9t6.json"
        ).read_text(encoding="utf-8")
        self.mesh = mesh
        self.format = _rs.package_format(self.machine_json, mesh)

    def read(self, raw, bindings=None):
        return _rs.package_read(raw, self.machine_json, self.mesh, list(bindings or ()))

    def signature(self, units):
        return _rs.package_signature(list(units))
