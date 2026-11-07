"""Machine-derived physical binding alignment shared by eager and retained graphs."""

import functools
import json


@functools.cache
def burst_line(machine_json: str) -> int | None:
    """The base alignment F-001's handling proves mover writes under
    (L2.MOV.020); None on an image without the flaw."""
    m = json.loads(machine_json)["machine"]
    return m["card"]["dram"]["line_bytes"] if m["flaws"]["F-001"]["present"] else None


def unaligned(full: dict, line: int | None, ports) -> list[str]:
    """Of `ports`, the buffers whose L1.MOV.019 proof leans on a write-line
    base, those bound off `line`."""
    if line is None:
        return []
    return sorted(k for k in ports if k in full and full[k] % line)


@functools.cache
def cluster_burst(machine_json: str) -> int | None:
    """F-016's cluster burst alignment within a 4 KiB page."""
    m = json.loads(machine_json)["machine"]
    if not m["flaws"]["F-016"]["present"]:
        return None
    return m["mg"]["wburst"] * m["die"]["word_bytes"]


def burst_unaligned(full: dict, burst: int | None, ports) -> list[str]:
    """Only bases on which a compiled memory-drain proof depends."""
    if burst is None:
        return []
    return sorted(k for k in ports if k in full and full[k] % 4096 % burst)
