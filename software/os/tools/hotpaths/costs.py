"""Read linked map costs and trap frames from one measured image directory."""

import argparse
import json
import pathlib
import re


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image_dir", type=pathlib.Path)
    a = ap.parse_args()
    text = (a.image_dir / "kos.map").read_text()
    totals = {"machine": 0, "kernel": 0, "library": 0}
    functions = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[-1] in (
            "k_event_out",
            "k_user_pa",
            "k_copy_out",
            "k_copy_in",
            "k_copy_str",
            "k_poll",
        ):
            functions[fields[-1]] = int(fields[2], 16)
        if len(fields) < 5 or ":(.text" not in fields[-1]:
            continue
        obj = fields[-1]
        if "/k/" not in obj:
            continue
        kind = (
            "machine"
            if "machine__" in obj
            else "kernel" if "kernel__" in obj else "library"
        )
        totals[kind] += int(fields[2], 16)
    listing = (a.image_dir / "kos.lst").read_text()
    for name in (
        "k_syscall",
        "k_trap",
        "s_trap_entry",
        "m_trap_entry",
        "k_event_out",
        "k_user_pa",
        "k_poll",
    ):
        match = re.search(
            rf"^[0-9a-f]+ <{name}>:.*?(?=^[0-9a-f]+ <|\Z)",
            listing,
            re.MULTILINE | re.DOTALL,
        )
        if match:
            print("\n".join(match[0].splitlines()[:35]))
    print(json.dumps(dict(text=totals, functions=functions)))


if __name__ == "__main__":
    main()
