"""Per-mesh, per-component clock control, addressed by a board meta file.

One mesh wizard carries four outputs off one VCO; each output is one
component's clock, and each is retuned independently by its own divider.
`mat1x` is not here: it is `mat2x` through a BUFGCE_DIV and moves with it.

    card = CardClocks.from_board(transport, "multimesh_v65")
    card.mesh(0).set("mag", 150)        # one component of one mesh
    card.mesh(1).set_profile("low")     # one mesh
    card.set_all("low")                 # the whole card
    card.mesh(2).read_all()             # {"noc": 100.0, ...}
"""

import json
import time
from pathlib import Path

from kohakutpu.clock import read32
from kohakutpu.clock.mmcm import (
    OUT_DIV_MAX,
    OUT_MAX,
    OUT_STRIDE,
    REG_STATUS,
    STATUS_LOCKED,
    ClockError,
    reg_clkout,
)

REG_LOAD = 0x25C
LOAD_DYNAMIC = 0x3

#: PG065 duty registers READ BACK ZERO and REJECT zero on write. A 64-bit
#: transport writes register pairs, and duty sits paired with CLKOUT1/3's
#: divider and with LOAD -- so the word must be composed, never read-merged,
#: or the MMCM faults the transaction (measured on v6.7, 2026-08-21).
#: The value is percent x 1000.
DUTY_HALF = 50000


def _is_duty(off: int) -> bool:
    return (
        off >= 0x210
        and (off - 0x210) % OUT_STRIDE == 0
        and (off - 0x210) // OUT_STRIDE <= OUT_MAX
    )

BOARDS = Path(__file__).resolve().parents[3] / "boards"


def load_board(name: str) -> dict:
    with open(BOARDS / f"{name}.json", encoding="utf-8") as fh:
        return json.load(fh)


class MeshClocks:
    """One mesh's wizard: named outputs, one VCO, one LOAD."""

    def __init__(self, transport, base: int, vco_mhz: float, outputs: dict):
        self.t = transport
        self.base = base
        self.vco = vco_mhz
        self.nominal_vco = vco_mhz
        self.outputs = {k: v["index"] for k, v in outputs.items()}
        self.nominal = {k: v["nominal_mhz"] for k, v in outputs.items()}

    def _write_cfg(self, off: int, value: int) -> None:
        """One config register, as the whole 64-bit word it lives in."""
        base = off & ~0x7

        def half(o: int) -> int:
            if o == off:
                return value & 0xFFFFFFFF
            if _is_duty(o - self.base):
                return DUTY_HALF
            return read32(self.t, o) & 0xFFFFFFFF

        self.t.write64(base, half(base) | (half(base + 4) << 32))

    def _div_for(self, name: str, mhz: float) -> int:
        if name not in self.outputs:
            raise ClockError(f"no output named {name!r}; have {list(self.outputs)}")
        if mhz > self.nominal[name]:
            raise ClockError(
                f"{name} nominal is {self.nominal[name]} MHz; {mhz} exceeds the "
                f"built (verified) ceiling -- raise it deliberately, not by typo"
            )
        k = int(self.vco // mhz)
        while k * mhz < self.vco:
            k += 1
        if not 1 <= k <= OUT_DIV_MAX:
            raise ClockError(f"{mhz} MHz needs divider {k}, outside 1..{OUT_DIV_MAX}")
        return k

    def read(self, name: str) -> float:
        k = read32(self.t, self.base + reg_clkout(self.outputs[name])) & 0xFF
        return self.vco / k if k else 0.0

    def read_all(self) -> dict:
        return {name: self.read(name) for name in self.outputs}

    def locked(self) -> bool:
        return bool(read32(self.t, self.base + REG_STATUS) & STATUS_LOCKED)

    def load(self, timeout: float = 2.0) -> None:
        """Apply the staged dividers and wait for lock.

        A LOAD that never unlocks is indistinguishable from one that never
        applied when polled over slow transport, so the divider REGISTERS are
        read back after lock as the application check.
        """
        self._write_cfg(self.base + REG_LOAD, LOAD_DYNAMIC)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.locked():
                return
            time.sleep(0.01)
        raise ClockError(
            f"wizard at {self.base:#x} did not lock within {timeout}s; the mesh "
            f"is on an unlocked clock -- reload a known-good setting"
        )

    def set(self, name: str, mhz: float, timeout: float = 2.0) -> float:
        """Retune ONE component to at most `mhz`. Returns the exact rate."""
        k = self._div_for(name, mhz)
        write32(self.t, self.base + reg_clkout(self.outputs[name]), k)
        self.load(timeout)
        got = self.read(name)
        if abs(got - self.vco / k) > 1e-6:
            raise ClockError(f"{name}: wrote divider {k}, read back {got} MHz")
        return got

    def plan_isolated(self, name: str, mhz: float, others: dict) -> tuple:
        """(m, {name: k}) putting ONE output at `mhz` and the rest near `others`.

        One VCO serves all four outputs, so a target no integer divide of the
        base VCO reaches forces a new M -- and then the OTHER outputs can only
        approximate their held rates. The plan minimizes the worst relative
        deviation of the held outputs; the caller reads the exact rates back.
        PFD is 25 MHz at D=4, so targets land on 25*M/k exactly or not at all.
        """
        best = None
        for m in range(32, 65):
            vco = 25.0 * m
            if not 800.0 <= vco <= 1600.0:
                continue
            k = round(vco / mhz)
            if not 1 <= k <= OUT_DIV_MAX or abs(vco / k - mhz) > 1e-6:
                continue
            ks = {name: k}
            worst = 0.0
            for other, want in others.items():
                ko = max(1, min(OUT_DIV_MAX, round(vco / want)))
                ks[other] = ko
                worst = max(worst, abs(vco / ko - want) / want)
            if best is None or worst < best[0]:
                best = (worst, m, ks)
        if best is None:
            raise ClockError(
                f"{mhz} MHz is not 25*M/k for any legal (M, k); step by 6.25"
            )
        return best[1], best[2]

    def set_isolated(self, name: str, mhz: float, others: dict,
                     timeout: float = 2.0) -> dict:
        """One output at exactly `mhz`, the rest as near `others` as the VCO
        allows, in one LOAD. Returns every output's exact rate."""
        m, ks = self.plan_isolated(name, mhz, others)
        self._write_cfg(self.base + 0x200, (m << 8) | 4)
        for out, k in ks.items():
            self._write_cfg(self.base + reg_clkout(self.outputs[out]), k)
        self.vco = 25.0 * m
        self.load(timeout)
        return self.read_all()

    def set_profile(self, profile: dict, timeout: float = 2.0) -> dict:
        """Retune every named component in one LOAD. Returns the exact rates.

        Also pins the VCO back to the board's nominal, undoing any
        `set_isolated` retune of M.
        """
        self.vco = self.nominal_vco
        self._write_cfg(self.base + 0x200, (int(self.vco / 25.0) << 8) | 4)
        for name, mhz in profile.items():
            k = self._div_for(name, mhz)
            self._write_cfg(self.base + reg_clkout(self.outputs[name]), k)
        self.load(timeout)
        got = self.read_all()
        for name, mhz in profile.items():
            k = self._div_for(name, mhz)
            if abs(got[name] - self.vco / k) > 1e-6:
                raise ClockError(f"{name}: wanted /{k}, wizard holds {got[name]} MHz")
        return got


class CardClocks:
    """Every mesh's wizard on one card, from the board meta."""

    def __init__(self, transport, board: dict):
        self.board = board
        stride = int(board["ctrl_stride"], 16)
        wbase = int(board["wizard_base"], 16)
        self.meshes = [
            MeshClocks(
                transport,
                wbase + i * stride,
                board["vco_mhz"],
                board["wizard_outputs"],
            )
            for i in range(board["meshes"])
        ]

    @classmethod
    def from_board(cls, transport, name: str) -> "CardClocks":
        return cls(transport, load_board(name))

    def mesh(self, index: int) -> MeshClocks:
        return self.meshes[index]

    def profile(self, name: str) -> dict:
        return self.board["profiles"][name]

    def set_all(self, profile) -> list:
        """Apply a named or literal profile to every mesh; returns per-mesh rates."""
        if isinstance(profile, str):
            profile = self.profile(profile)
        return [m.set_profile(profile) for m in self.meshes]

    def read_all(self) -> list:
        return [m.read_all() for m in self.meshes]
