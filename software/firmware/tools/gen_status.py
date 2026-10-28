"""Generate the host's completion status and wait names from the firmware's
`ka/package/status.h` (RUL.031): writes
software/driver/kohakuaccel/driver/node/queue/status.py. `--check` exits 1
when the file differs from what the header gives.

    python software/firmware/tools/gen_status.py [--check]
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HEADER = ROOT / "software/firmware/kohakuaccel/include/ka/package/status.h"
OUT = ROOT / "software/driver/kohakuaccel/driver/node/queue/status.py"


def enum(text: str, name: str) -> dict:
    """{item: value} of `enum name { ... }`."""
    body = re.search(rf"enum\s+{name}\s*\{{(.*?)\}}", text, re.DOTALL).group(1)
    return {
        k: int(v, 0)
        for k, v in re.findall(
            r"^\s*(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)", body, re.MULTILINE
        )
    }


def render() -> str:
    text = HEADER.read_text(encoding="utf-8")
    status = enum(text, "ka_status")
    waits = enum(text, "ka_wait")
    lines = [
        '"""Completion status and TIMEOUT wait names, generated from the firmware\'s',
        "ka/package/status.h by software/firmware/tools/gen_status.py; do not edit.",
        '"""',
        "",
        "#: Completion status codes (KA_ST_*).",
        "STATUS = {",
    ]
    lines += [f'    {v:#04x}: "{k.removeprefix("KA_ST_")}",' for k, v in status.items()]
    lines += [
        "}",
        "",
        "#: What a TIMEOUT's detail says was being waited for (KA_WAIT_*).",
        "WAITS = {",
    ]
    lines += [
        f'    {v}: "{k.removeprefix("KA_WAIT_").lower()}",' for k, v in waits.items()
    ]
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = render()
    if a.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print(f"{OUT.name}: {'matches' if same else 'differs from status.h'}")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
