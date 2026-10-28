"""One image's sources: its build config, board, die top, RTL defines and firmware.

Every reader returns (value, source), the source a repo-relative `path:line`
or a named derivation, so the generated machine file says where each fact
came from.
"""

import importlib.util
import json
import re
import sys
from functools import cache, cached_property
from pathlib import Path

from . import header, verilog

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

import gen_board
import gen_card

TOPS = ROOT / "src" / "kohakutpu" / "top" / "generated"
SRU_TOPS = ROOT / "src" / "kohakutpu" / "sru" / "top" / "generated"
FW = ROOT / "software" / "firmware"
FW_INC = FW / "kohakuaccel" / "include" / "ka"
FW_BUILD = ROOT / "build" / "fw"
MEASURED = ROOT / "machines" / "measured.json"


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


@cache
def _lines(path: Path) -> tuple:
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _firmware_build():
    spec = importlib.util.spec_from_file_location("fw_build", FW / "build.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {FW / 'build.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Image:
    """The sources of image `ver` (scripts/tcl/<ver>/00_config.tcl) running
    firmware image `fw`."""

    def __init__(
        self,
        ver: str,
        fw: str = "kohakutpu_node",
        die: str | None = None,
        board: str | None = None,
    ):
        self.ver = ver
        self.fw = fw
        self.cfg = gen_card.Config(ver)
        if board:
            self.board_dict = json.loads(
                (ROOT / "boards" / f"{board}.json").read_text(encoding="utf-8")
            )
        else:
            self.board_dict = gen_board.board(ver)
        mods = {m for _, m in self.cfg.meshes()}
        if die:
            mods = {die}
        if len(mods) != 1:
            raise SystemExit(
                f"{ver}: dies use {sorted(mods)}; a machine file describes one die shape"
            )
        self.die_module = mods.pop()
        self.top_path = next(
            p
            for p in (TOPS / f"{self.die_module}.v", SRU_TOPS / f"{self.die_module}.v")
            if p.exists()
        )
        if die and not board:
            shape = gen_board.die_map(die)
            self.board_dict = dict(self.board_dict)
            self.board_dict["units"] = {
                "MG": [list(p) for p in shape.cus],
                "VC": [list(p) for p in shape.vecs],
            }
            self.board_dict["sys_ports"] = [list(p) for p in shape.mags]
            self.board_dict["fetch_port"] = list(shape.mags[-1])

    @cached_property
    def defines(self) -> dict:
        out = {}
        for d in gen_card.image_defines(self.ver):
            name, _, value = d.partition("=")
            out[name] = value or "1"
        return out

    def board(self, key: str):
        return self.board_dict[key], f"boards/{self.board_dict['name']}.json:{key}"

    def config(self, name: str):
        return self.cfg.s(name), f"scripts/tcl/{self.ver}/00_config.tcl:{name}"

    def rtl(self, path: str, name: str):
        p = ROOT / path
        value, line = verilog.params(p, self.defines)[name]
        return value, f"{path}:{line}"

    def top(self, name: str):
        return self.rtl(rel(self.top_path), name)

    def core(self, name: str):
        return self.rtl("src/kohakutpu/vector2/v2_core.v", name)

    def grep(self, path: str, pattern: str):
        """The first line of repo file `path` matching `pattern`: (match, `path:line`).
        For a fact an RTL file states as a decode, a width or a literal, not
        as a parameter."""
        rx = re.compile(pattern)
        for n, line in enumerate(_lines(ROOT / path), 1):
            m = rx.search(line)
            if m:
                return m, f"{path}:{n}"
        raise SystemExit(f"{path}: no line matches {pattern!r}")

    def rtl_width(self, path: str, name: str):
        """Bits of the signal `name` as `path` declares it (`[N:0] name`)."""
        m, src = self.grep(path, rf"\[(\d+):0\]\s+(?:\w+\s*,\s*)*{name}\b")
        return int(m.group(1)) + 1, src

    def rtl_array(self, path: str, name: str, dim: int = 0):
        """Entries of dimension `dim` of the array `name` (`name [0:N]...`)."""
        m, src = self.grep(path, rf"\b{name}\s*((?:\[0:\d+\])+)")
        dims = [int(d) + 1 for d in re.findall(r"\[0:(\d+)\]", m.group(1))]
        return dims[dim], src

    def rtl_override(self, path: str, name: str, inst: str | None = None):
        """The first instance override `.name(N)` in `path` (on the line naming
        instance `inst`, when given), or None when there is none."""
        tail = rf".*\b{inst}\b" if inst else ""
        rx = re.compile(rf"\.{name}\((\d+)\){tail}")
        for n, line in enumerate(_lines(ROOT / path), 1):
            m = rx.search(line)
            if m:
                return int(m.group(1)), f"{path}:{n}"
        return None

    def vh_define(self, path: str, name: str):
        """A numeric `` `define `` of a Verilog header."""
        m, src = self.grep(path, rf"^`define\s+{name}\s+(\d+)\s*$")
        return int(m.group(1)), src

    def fw_define(self, path: str, name: str):
        value, line = header.defines(FW_INC / path)[name]
        return value, f"{rel(FW_INC / path)}:{line}"

    def fw_src_define(self, path: str, name: str):
        """A #define of a firmware source file (`path` under software/firmware)."""
        value, line = header.defines(FW / path)[name]
        return value, f"{rel(FW / path)}:{line}"

    def fw_flags(self, path: str, prefix: str):
        got = header.defines(FW_INC / path)
        flags = {
            k.removeprefix(prefix): v
            for k, (v, _) in got.items()
            if k.startswith(prefix)
        }
        return flags, f"{rel(FW_INC / path)}:{prefix}*"

    def fw_enum(self, path: str, name: str):
        got = header.enum(FW_INC / path, name)
        return got["items"], f"{rel(FW_INC / path)}:{got['line']}"

    def fw_size(self):
        path = FW_BUILD / f"{self.fw}.size"
        sections = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if (
                len(parts) == 3
                and parts[1].isdigit()
                and not parts[0].startswith(".debug")
            ):
                sections[parts[0]] = int(parts[1])
        return sections, rel(path)

    def fw_parts(self):
        mod = _firmware_build()
        return list(mod.IMAGES[self.fw]), "software/firmware/build.py:IMAGES"

    def fw_memory(self):
        mod = _firmware_build()
        imem, spad = mod.MEMORY.get(self.fw, (None, mod.IMEM_BYTES, mod.SPAD_BYTES))[1:]
        return {
            "imem_bytes": imem,
            "spad_bytes": spad,
        }, "software/firmware/build.py:MEMORY"

    def fw_unit_classes(self):
        parts = self.fw_parts()[0]
        classes = []
        for part in parts:
            units = FW / part
            if units.name == "units":
                classes += sorted(p.stem for p in units.glob("*.c"))
        return classes, "software/firmware/build.py:IMAGES"

    def measured(self, key: str):
        data = json.loads(MEASURED.read_text(encoding="utf-8"))
        entry = data[key]
        return entry["value"], f"machines/measured.json:{key} ({entry['source']})"
