"""The simulated card: a Verilated multimesh_v8t behind the two methods.

`VerilatorTransport` spawns the harness (sim/verilator/harness/v8t_card_main.cpp,
built by `scripts/py/vlt.py v8t_card --cc ...`) and speaks its line protocol over
stdin/stdout. Same addresses as the JTAG manager sees on silicon: the harness
plays host manager 0, so a driver written for the card runs here unchanged.

    t = VerilatorTransport()                       # build/vlt_v8t_card under WSL
    t.write64(0x1_0000_0000, 0x1234)               # node 0's memory window
    t.read_block(0x800000, 64)                     # node 0's control window

The DRAM backdoors (`backdoor_read/write`) and `run(cycles)` exist only here.
"""

import pathlib
import re
import subprocess

from kohakuaccel.transport.base import MASK64, Transport, TransportUnavailable, aligned

ROOT = pathlib.Path(__file__).resolve().parents[3]
WSL_DISTRO = "Ubuntu-24.04"


def _to_wsl(p: pathlib.Path) -> str:
    s = pathlib.Path(p).resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


class VerilatorTransport(Transport):
    """A card that is a process."""

    bulk = True
    beat_bytes = 8
    max_block = 2048

    def __init__(
        self,
        build_dir: pathlib.Path | str | None = None,
        wsl: bool = True,
        distro: str = WSL_DISTRO,
        settle: int = 40000,
        timeout: float = 600.0,
    ) -> None:
        build = (
            pathlib.Path(build_dir) if build_dir else ROOT / "build" / "vlt_v8t_card"
        )
        vsim = build / "obj_dir" / "vsim"
        if not vsim.exists():
            raise TransportUnavailable(
                f"no model at {vsim}; build it with `python scripts/py/vlt.py v8t_card "
                f"--cc sim/verilator/harness/v8t_card_main.cpp --keep`"
            )
        inv = f"./obj_dir/vsim --settle {settle}"
        if wsl:
            cmd = [
                "wsl",
                "-d",
                distro,
                "--",
                "bash",
                "-lc",
                f"cd {_to_wsl(build)} && stdbuf -o0 {inv}",
            ]
        else:
            cmd = [str(vsim), "--settle", str(settle)]
        # The model's own $display lines go to stderr (the harness keeps the
        # reply channel private), into a file rather than a pipe nobody drains.
        self.model_log = build / "model.log"
        self._errf = open(self.model_log, "w")  # noqa: SIM115 -- closed in close()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._errf,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )
        self.timeout = timeout
        self.calls = 0
        ready = self._line()
        if not ready.startswith("READY"):
            raise TransportUnavailable(f"harness did not come up: {ready!r}")
        self.ready = ready

    # ------------------------------------------------------------ protocol
    def _line(self) -> str:
        # Only a reply is returned; anything else on the channel is reported
        # and skipped, never mistaken for the answer to the command in flight.
        while True:
            line = self.proc.stdout.readline()
            if not line:
                tail = ""
                try:
                    tail = self.model_log.read_text(encoding="utf-8")[-2000:]
                except OSError:
                    pass
                raise RuntimeError(f"harness closed the pipe: {tail.strip()}")
            line = line.strip()
            if line.startswith(("V ", "OK", "E ", "READY")):
                return line
            print(f"  [model] {line}", flush=True)

    def _cmd(self, text: str) -> str:
        self.calls += 1
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        reply = self._line()
        if reply.startswith("E "):
            raise RuntimeError(f"harness: {reply[2:]} (on {text[:60]!r})")
        return reply

    # ----------------------------------------------------------- Transport
    def write64(self, addr: int, data: int) -> None:
        aligned(addr)
        self._cmd(f"W {addr:x} {data & MASK64:x}")

    def read64(self, addr: int) -> int:
        aligned(addr)
        _, word = self._cmd(f"R {addr:x}").split(" ", 1)
        return int(word, 16)

    def write_block(self, addr: int, data: bytes) -> None:
        aligned(addr)
        if len(data) % 8:
            raise ValueError(f"{len(data)} bytes is not a whole number of 64-bit words")
        for off in range(0, len(data), self.max_block):
            chunk = data[off : off + self.max_block]
            self._cmd(f"WB {addr + off:x} {chunk.hex()}")

    def read_block(self, addr: int, nbytes: int) -> bytes:
        aligned(addr)
        if nbytes % 8:
            raise ValueError(f"{nbytes} bytes is not a whole number of 64-bit words")
        out = bytearray()
        for off in range(0, nbytes, self.max_block):
            n = min(self.max_block, nbytes - off)
            out += bytes.fromhex(self._cmd(f"RB {addr + off:x} {n:x}")[2:])
        return bytes(out)

    # ------------------------------------------------------- simulation-only
    def backdoor_read(self, channel: int, word: int) -> bytes:
        """One 512-bit DRAM word of `channel`, straight out of the model."""
        h = self._cmd(f"BR {channel:x} {word:x}")[2:]
        return int(h, 16).to_bytes(64, "little")

    def backdoor_write(self, channel: int, word: int, data: bytes) -> None:
        if len(data) != 64:
            raise ValueError("a DRAM word is 64 bytes")
        self._cmd(f"BW {channel:x} {word:x} {int.from_bytes(data, 'little'):0128x}")

    def run(self, cycles: int) -> None:
        """Advance the sysnode clock `cycles` cycles with no host traffic."""
        self._cmd(f"T {cycles:x}")

    def status(self) -> dict:
        v = self._cmd("S")[2:]
        return {k: int(x, 16) for k, x in (kv.split("=") for kv in v.split())}

    def close(self) -> None:
        try:
            self.proc.stdin.write("Q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 -- closing; the process is going away
            self.proc.kill()
        self._errf.close()

    def __repr__(self) -> str:
        return f"VerilatorTransport({self.calls} calls)"
