"""Measure Python/XDMA read-loop CPU versus native POSIX polling on a regular file.

No card or device latency is modeled. The bounded iteration count isolates host
copies/dispatch/syscalls at the lead's observed 20 polls per launch.
"""

import argparse
import ctypes
import json
import statistics
import tempfile
import time
from pathlib import Path

from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.compiler import _rs

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = _rs.QueueLayout(MACHINE.read_text(encoding="utf-8"))


class FileXdma(XdmaLinux):
    def __init__(self, fd):
        self.fd = fd
        super().__init__(recover=None)

    def _open(self):
        self._c2h = self.fd
        self._pread = ctypes.CDLL(None, use_errno=True).pread
        self._pread.restype = ctypes.c_ssize_t
        self._pread.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_longlong,
        ]
        self._addr = ctypes.addressof(ctypes.c_char.from_buffer(self._staging))

    def close(self):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--polls", type=int, default=20)
    ap.add_argument("--repeat", type=int, default=1000)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    results = {}
    with tempfile.TemporaryFile() as backing:
        backing.write(bytes(4096))
        backing.flush()
        transport = FileXdma(backing.fileno())
        for mode in ("python_xdma_reads", "native_reads"):
            samples = []
            for _ in range(7):
                start = time.process_time_ns()
                for _ in range(args.repeat):
                    if mode == "native_reads":
                        raw, n = _rs.wait_tail(
                            backing.fileno(), 0, 0, 0, 0.05, 0, args.polls, LAYOUT
                        )
                        assert n == args.polls and raw == bytes(384)
                    else:
                        for _ in range(args.polls):
                            raw = transport._c2h_lines(0, 384)
                        assert raw == bytes(384)
                samples.append((time.process_time_ns() - start) / args.repeat / 1e6)
            results[mode] = {
                "ms_per_launch": statistics.median(samples[1:]),
                "samples": samples,
            }
        transport._staging.close()
    results.update(
        polls=args.polls,
        repeat=args.repeat,
        device_latency=False,
        transport="regular-file POSIX pread",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
