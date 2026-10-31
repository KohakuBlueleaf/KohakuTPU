"""Generate software/application/kohakutpu/application/runtime/osabi.py from C headers.

python software/os/tools/ostpu/gen_abi.py [--check]
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
ABI_H = ROOT / "software/os/include/kos/abi.h"
KDS_H = ROOT / "software/os/include/kos/kds.h"
FMT_H = ROOT / "software/firmware/kohakuaccel/include/ka/package/format.h"
INTERP_C = ROOT / "software/firmware/kohakuaccel/package/interp.c"
OUT = ROOT / "software/application/kohakutpu/application/runtime/osabi.py"


def parse_defines(text: str, prefix_re: str, env: dict) -> dict:
    out = {}
    for k, expr in re.findall(
        rf"^\s*#define\s+({prefix_re})\s+([^\n/]+)",
        text,
        re.MULTILINE,
    ):
        clean = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", expr.strip())
        val = int(eval(clean, {"__builtins__": {}}, {**env, **out}))
        out[k] = val
    return out


def parse_enum(text: str, prefix: str) -> dict:
    out = {}
    for m in re.finditer(r"enum\s*(?:\w+)?\s*\{([^}]+)\}", text):
        body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.DOTALL)
        val = 0
        for raw_item in body.split(","):
            item = raw_item.strip()
            if not item:
                continue
            if "=" in item:
                name, expr = [x.strip() for x in item.split("=", 1)]
                val = int(re.sub(r"[uUlL]+$", "", expr), 0)
            else:
                name = item
            if name.startswith(prefix):
                out[name] = val
            val += 1
    return out


def parse_struct_fields(text: str, struct_name: str) -> tuple[str, ...]:
    body = re.search(rf"struct\s+{struct_name}\s*\{{(.*?)\}};", text, re.DOTALL).group(
        1
    )
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    fields = []
    for stmt in body.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        decl = re.sub(
            r"^(?:struct\s+\w+|(?:u?int(?:8|16|32|64)_t|unsigned|int))\s+", "", stmt
        )
        for part in decl.split(","):
            part = part.strip()
            m = re.match(r"(\w+)(?:\[(\w+)\])?$", part)
            if m:
                fields.append(m.group(1))
    return tuple(fields)


def load_from_headers(
    abi_path=ABI_H, kds_path=KDS_H, fmt_path=FMT_H, interp_path=INTERP_C
) -> dict:
    abi_text = pathlib.Path(abi_path).read_text(encoding="utf-8")
    kds_text = pathlib.Path(kds_path).read_text(encoding="utf-8")
    fmt_text = pathlib.Path(fmt_path).read_text(encoding="utf-8")
    interp_text = pathlib.Path(interp_path).read_text(encoding="utf-8")

    consts = {}
    consts.update(
        parse_defines(
            abi_text,
            r"(?:KOS_DE_(?!CMD_BYTES)[A-Z0-9_]+|KOS_MV_[A-Z0-9_]+|KOS_MOVE_[A-Z0-9_]+|KOS_LAND_SIZE|KDS_LZ_[A-Z0-9_]+|KOS_LRING_[A-Z0-9_]+)",
            consts,
        )
    )
    consts.update(parse_enum(kds_text, "KDS_"))
    consts.update(
        parse_defines(
            kds_text,
            r"(?:KDS_MAX[A-Z0-9_]+|KDS_TASK_OFF_[A-Z0-9_]+|KDS_PATCH_BYTES|OSTPU_[A-Z0-9_]+)",
            consts,
        )
    )
    consts.update(
        parse_defines(
            fmt_text,
            r"(?:KA_PKG_(?:MAGIC|VERSION|HEADER|REGS)|KA_STEP_F_[A-Z0-9_]+|KA_MOVER_SKIP)",
            consts,
        )
    )
    consts.update(parse_enum(fmt_text, "KA_PH_"))
    consts.update(parse_enum(fmt_text, "KA_OP_"))
    consts.update(parse_enum(fmt_text, "KA_ALU_"))
    consts.update(parse_defines(interp_text, r"KA_LRING_[A-Z0-9_]+", consts))

    m_arg2 = re.search(
        r"de_mb\(\s*KA_NM_ARG2\s*,\s*\(\(uint64_t\)\(\(s->y\s*<<\s*(\d+)\)\s*\|\s*s->x\)\s*<<\s*(\d+)\)\s*\|\s*\(1UL\s*<<\s*(\d+)\)\)",
        interp_text,
    )
    m_arg3 = re.search(
        r"de_mb\(\s*KA_NM_ARG3\s*,\s*\(at\s*<<\s*(\d+)\)\s*\|\s*\((0x[0-9a-fA-F]+|\d+)UL\s*<<\s*(\d+)\)\s*\|\s*n\)",
        interp_text,
    )
    m_rel = re.search(
        r"rel\s*=\s*\(\(pk\.base\s*&\s*~KA_UNCACHED\)\s*\+\s*pk\.opay\)\s*<<\s*(\d+)",
        interp_text,
    )
    if (
        not m_arg2
        or not m_arg3
        or not m_rel
        or int(m_arg2.group(1), 0) != consts["KOS_DE_FETCH_Y_SHIFT"]
        or int(m_arg2.group(2), 0) != consts["KOS_DE_FETCH_COORD_SHIFT"]
        or (1 << int(m_arg2.group(3), 0)) != consts["KOS_DE_FETCH_REPLY"]
        or int(m_arg3.group(1), 0) != consts["KOS_DE_FETCH_SHIFT"]
        or (int(m_arg3.group(2), 0) << int(m_arg3.group(3), 0))
        != consts["KOS_DE_FETCH_TAG"]
        or int(m_rel.group(1), 0) != consts["KOS_DE_FETCH_SHIFT"]
    ):
        raise ValueError("interp.c fetch/relocation encoding drifted from abi.h")

    stat_names = parse_struct_fields(kds_text, "kds_server_stat")
    launch_rec_fields = parse_struct_fields(abi_text, "kos_launch_rec")
    mailbox_fields = parse_struct_fields(kds_text, "ostpu_mailbox")
    task_fields = parse_struct_fields(kds_text, "kds_task")
    return dict(
        consts=consts,
        stat_names=stat_names,
        launch_rec_fields=launch_rec_fields,
        mailbox_fields=mailbox_fields,
        task_fields=task_fields,
    )


def render(data: dict | None = None) -> str:
    d = data or load_from_headers()
    consts = d["consts"]
    lines = [
        '"""KohakuAccel OS and KAPK ABI constants generated from C headers',
        "by software/os/tools/ostpu/gen_abi.py; do not edit.",
        '"""',
        "",
    ]
    for k, v in consts.items():
        rep = (
            f"0x{v:X}"
            if v >= 1024
            or "MAGIC" in k
            or "RELOC" in k
            or "MASK" in k
            or "OFF" in k
            or "SIZE" in k
            or "SLICE" in k
            or "USER" in k
            or "SKIP" in k
            else str(v)
        )
        lines.append(f"{k} = {rep}")
    lines += [
        "",
        "STAT_NAMES = (",
        *[f'    "{name}",' for name in d["stat_names"]],
        ")",
        f'STAT_FMT = "<{len(d["stat_names"])}Q"',
        "",
        "LAUNCH_REC_FIELDS = (",
        *[f'    "{name}",' for name in d["launch_rec_fields"]],
        ")",
        f'LAUNCH_REC_FMT = "<{len(d["launch_rec_fields"])}Q"',
        "",
        "MAILBOX_FIELDS = (",
        *[f'    "{name}",' for name in d["mailbox_fields"]],
        ")",
        "",
        "TASK_FIELDS = (",
        *[f'    "{name}",' for name in d["task_fields"]],
        ")",
        "",
        "PKG_HDR_WORDS = KA_PKG_HEADER // 8",
        'PKG_HDR_FMT = f"<{PKG_HDR_WORDS}Q"',
        "",
        "C_ABI = {",
        *[f'    "{k}": {k},' for k in consts],
        "}",
        "",
        "",
        "def de_cmd_bytes(entry_count: int) -> int:",
        "    return (",
        "        (entry_count + KOS_DE_GROUP_ENTRIES - 1) // KOS_DE_GROUP_ENTRIES",
        "    ) * KOS_DE_GROUP_BYTES",
        "",
        "",
        "def de_fetch_dst(fetch: int) -> int:",
        "    return KOS_DE_FETCH_TYPED | (fetch & 0xFFFF)",
        "",
        "",
        "def de_fetch_arg2(x: int, y: int) -> int:",
        "    return (",
        "        ((y << KOS_DE_FETCH_Y_SHIFT) | x) << KOS_DE_FETCH_COORD_SHIFT",
        "    ) | KOS_DE_FETCH_REPLY",
        "",
        "",
        "def de_fetch_arg3(addr_or_off: int, n: int) -> int:",
        "    return ((addr_or_off << KOS_DE_FETCH_SHIFT) | KOS_DE_FETCH_TAG | (n & 0xFF)) & (",
        "        (1 << 64) - 1",
        "    )",
        "",
        "",
        "def mv_hdr_word(pa_or_off: int, sel: int) -> int:",
        "    return (KOS_MV_HDR_VALID | (pa_or_off << KOS_DE_MOVE_SHIFT) | sel) & ((1 << 64) - 1)",
        "",
        "",
        "def mv_dim_word(words: int, sel: int) -> int:",
        "    return (KOS_MV_DIM_STRIDE | (words << KOS_MV_DIM_WORDS_SHIFT) | sel) & (",
        "        (1 << 64) - 1",
        "    )",
        "",
        "",
        "def mv_1d_entries(",
        "    src_code: int, src_pa_or_off: int, dst_pa: int, words: int",
        ") -> tuple[tuple[int, int], ...]:",
        "    return (",
        "        (src_code, mv_hdr_word(src_pa_or_off, 0)),",
        "        (KOS_DE_MV_DIM, mv_dim_word(words, 0)),",
        "        (KOS_DE_MV_AXIS, 0),",
        "        (KOS_DE_MV_WINDOW, 0),",
        "        (KOS_DE_MV_WINDOW, 2),",
        "        (KOS_DE_MV_HDR, mv_hdr_word(dst_pa, 1)),",
        "        (KOS_DE_MV_DIM, mv_dim_word(words, 1)),",
        "        (KOS_DE_MV_AXIS, 0),",
        "        (KOS_DE_MV_WINDOW, 1),",
        "        (KOS_DE_MV_WINDOW, 3),",
        "        (KOS_DE_MV_CTRL, KOS_MV_CTRL_1D),",
        "    )",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = render()
    if a.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print(f"{OUT.name}: {'matches' if same else 'differs from C headers'}")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
