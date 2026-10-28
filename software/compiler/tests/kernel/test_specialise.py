"""The retained Python specialisation memo over Rust compiles."""

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.examples import dsl as ex
from kohakutpu.stack import kernel as K


def test_a_second_specialisation_compiles_nothing(monkeypatch):
    machine = machine_of("multimesh_v9t8", nodes=1)
    e = ex.EXAMPLES["softmax"]
    a = K.specialise(e.kernel, e.given, machine=machine, **e.consts)

    def forbid(*args, **kwargs):
        raise AssertionError("warm specialisation recompiled")

    monkeypatch.setattr(K.rs, "compile", forbid)
    b = K.specialise(e.kernel, e.given, machine=machine, **e.consts)
    assert a is b
