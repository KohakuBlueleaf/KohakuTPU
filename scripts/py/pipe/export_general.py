"""Lower the 24 exact captured quant geometries before/after general padding.

Before reproduces merged loop-2 scheduling (batch4/overlap, narrow tail rule).
After uses the production generator. This is a measurement exporter, not a gate.
"""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import matmul as M
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    ap.add_argument(
        "--manifest", type=Path, required=True, help="captured kernel manifest JSON"
    )
    args = ap.parse_args()
    manifest = json.loads(args.manifest.read_text())
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    args.output.mkdir(parents=True, exist_ok=True)
    for row in manifest:
        if row["kind"] != "quant":
            continue
        graph = json.loads(row["source"]["source"])
        x, q = [p["spec"] for p in graph["ports"]]
        rp, cp = q["shape"]
        side = q["dtype"][-1]
        layout = {k: v["int"] for k, v in q["layout"]}
        g, nk = layout["gm" if side == "a" else "gn"], layout["nk"]
        copies = [op for op in graph["body"] if op.get("op") == "copy"]
        if copies:
            src = copies[0]["src"]["axes"]
            r0, rows = src[0]["run"]
            c0, cols = src[1]["run"]
        else:
            r0 = c0 = 0
            rows, cols = x["shape"]
        pitch = x["shape"][1]
        header = (
            f"target ktpu.v9\nfn q.l2(x: f16[{x['shape'][0]},{pitch}] @dram, "
            f"q: mx7{side}[{rp},{cp}] @dram(g{'m' if side == 'a' else 'n'}={g},nk={nk}))\n"
        )
        attrs = "finite_batch=4 overlap=1"
        if rows == rp and nk == 1 and cols % 32 == 16 and cp == cols + 16:
            full = cols - 16
            body = (
                f"    quant q[0 : +{rp},0 : +{full}] <- x[{r0} : +{rows},{c0} : +{full}] {attrs}\n"
                if full
                else ""
            )
            body += f"    quant q[0 : +{rp},{full} : +32] <- x[{r0} : +{rows},{c0+full} : +16] pad_cols=16 {attrs}\n"
        elif rows == rp and cols == cp and pitch == cp and not (r0 or c0):
            body = f"    quant q <- x {attrs}\n"
        else:
            body = (
                f"    z = buffer : f16[{rp},{cp}] @dram\n    fill z value=0\n"
                f"    copy z[0 : +{rows},0 : +{cols}] <- x[{r0} : +{rows},{c0} : +{cols}]\n"
                f"    quant q <- z {attrs}\n"
            )
        before = K.compile(header + body, name="q", machine=machine)
        kernel, given = M.quant_kernel(
            Operand("full", pitch=pitch, offset=r0 * pitch + c0),
            rows,
            cols,
            rp,
            cp,
            side,
            g,
            nk,
        )
        after = K.specialise(kernel, given, machine=machine)
        for label, compiled in (("before", before), ("after", after)):
            record = dict(
                key=row["key"],
                label=label,
                name=compiled.name,
                l1=compiled.l1_text,
                verdict=compiled.verdict,
                ports=graph["ports"],
                rows=rows,
                cols=cols,
                rp=rp,
                cp=cp,
                g=g,
                nk=nk,
                launches=row["launches"],
            )
            (args.output / f"{row['key']}-{label}.json").write_text(json.dumps(record))
        print(
            row["key"][:10],
            rows,
            cols,
            rp,
            cp,
            before.verdict["refused"],
            after.verdict["refused"],
        )


if __name__ == "__main__":
    main()
