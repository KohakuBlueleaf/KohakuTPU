"""Machine-selected non-vector packing must never request vector-only padding."""

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K


def test_mover_board_quant_padding_and_walk(monkeypatch):
    monkeypatch.delenv("KTPU_FP16_MX7_BACKEND", raising=False)
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    machine = machine_of("multimesh_v9t8")
    assert K.dsl_machine(machine).packing_backend == "mover_quant"
    row = MM.quant_kernel(Operand("full", pitch=48), 5, 48, 8, 64, "a", 2, 1)
    walk = MM.quant_walk_kernel(480, ((15, 32),), 0, 5, 48, 8, 64, "a", 2, 1)
    for kernel, given in (row, walk):
        traced = kernel.trace(given, machine=K.dsl_machine(machine))
        # The producer materializes padding; no vector-only request reaches L2.
        quant = [s for s in traced.graph["body"] if s.get("op") == "quant"]
        assert len(quant) == 1
        assert not {"pad", "pad_cols", "finite_batch", "overlap"} & {
            k for k, _ in quant[0]["kw"]
        }
        compiled = K.specialise(kernel, given, machine=machine)
        assert not compiled.verdict["refused"], compiled.verdict
        assert "quant " in compiled.l1_text
