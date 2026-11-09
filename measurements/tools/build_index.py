"""The zoo's front page: every figure under its perspective, as one HTML page with the
images embedded, and README.md beside it with the same order."""

import base64
import csv
import html

from style import FIGURES, HOME, ROOT, ROUND, TABLES

#: (anchor, title, lead, ((figure, caption, wide), ...)); a wide figure takes the row.
SECTIONS = (
    (
        "path",
        "The whole path",
        "One warm step from tinygrad to the units, in one picture.",
        (
            (
                "runs/lenet.zp/diagram",
                "Each stage of a LeNet step, darkest where the time goes.",
                True,
            ),
            (
                "runs/lenet.zp/path",
                "LeNet: the same stages on one time axis. Host rows are under cProfile.",
                False,
            ),
            (
                "runs/vit.z/path",
                "ViT: the same axis. The node waits on the host for most of the step.",
                False,
            ),
        ),
    ),
    (
        "compare",
        "Against the baseline",
        "This round against the zoo at commit 56021fd4, one row a like configuration.",
        (
            (
                "compare/cycles",
                "A warm step by kernel kind: the baseline's bar, then this round's.",
                True,
            ),
            (
                "compare/launches",
                "Launches and span of a warm step against the baseline's.",
                True,
            ),
        ),
    ),
    (
        "zoo",
        "Models against each other",
        "Every run on the same axes. Eager is z, TinyJit zj, low zl, ship zs.",
        (
            (
                "zoo/wall",
                "Wall time of a warm step. Grey is the node waiting for the host.",
                False,
            ),
            (
                "zoo/shares",
                "The node's time: a unit busy, firmware, no unit busy, or idle.",
                False,
            ),
            (
                "zoo/kinds",
                "Run cycles by kernel kind: what each architecture spends them on.",
                False,
            ),
            (
                "zoo/unit_busy",
                "Busy cycles of each unit over the span; 4.0 is all four always busy.",
                False,
            ),
            (
                "zoo/mfu",
                "MFU: math-only floor over the span, for clusters and vector cores.",
                False,
            ),
            (
                "zoo/knobs",
                "Batch size and stack knobs, one change at a time.",
                False,
            ),
            (
                "zoo/boards",
                "2+2 against 4+2 on the card, with the timed model's step beside each.",
                True,
            ),
            (
                "zoo/wall_matrix",
                "Whole-run wall seconds, every model by clock and mode, on both boards.",
                True,
            ),
            (
                "zoo/clocks22",
                "2+2 at three clocks: cycles a warm step and device time.",
                True,
            ),
            (
                "zoo/clocks42",
                "4+2 at three clocks: cycles a warm step and device time.",
                True,
            ),
            (
                "zoo/unit_share",
                "How much of a warm step each unit is busy, on both boards at mid.",
                True,
            ),
            (
                "zoo/idle_dies",
                "Node 0 at mid against the clock of the dies that run nothing.",
                False,
            ),
            (
                "zoo/clock",
                "Three clocks: cycles rise because memory stays in nanoseconds.",
                True,
            ),
        ),
    ),
    (
        "memory",
        "Memory",
        "A vector core or the mover walking DRAM, on the card at three clocks.",
        (
            (
                "memory/grid_warm",
                "Node cycles a 32-byte word, second pass. Contiguous is the top row.",
                True,
            ),
            (
                "memory/grid_cold",
                "The same grid on the first pass: the gap to warm is the cache.",
                True,
            ),
            (
                "memory/grid_42",
                "The same walks on 4+2, at low, mid and ship.",
                True,
            ),
            (
                "memory/clock_scaling",
                "Contiguous walks: small footprints cost more, then the curve is flat.",
                False,
            ),
            (
                "memory/homes",
                "One line every 64 KB from four start offsets: cost differs by offset.",
                False,
            ),
        ),
    ),
    (
        "firmware",
        "Firmware and dispatch",
        "What a launch waits on. OS rows are this zoo's silicon runs on one die.",
        (
            (
                "firmware/steps_lenet",
                "LeNet on the debug image: step opcodes by kind, largest launches.",
                True,
            ),
            (
                "firmware/steps_vit",
                "ViT on the debug image: the mover drives transpose, dispatch the rest.",
                True,
            ),
            (
                "firmware/steps_lenet_passes",
                "LeNet's largest transposes and gathers, pass by pass, on one axis.",
                True,
            ),
            (
                "firmware/steps_vit_passes",
                "ViT's largest transposes and gathers, pass by pass, on one axis.",
                True,
            ),
            (
                "firmware/dispatch_paths",
                "One launch on each path, firmware against the OS kernel and dispatcher.",
                False,
            ),
            (
                "firmware/replay_paths_22",
                "The same steps on 2+2, at low, mid and ship.",
                False,
            ),
            (
                "firmware/replay_paths_42",
                "The same steps on 4+2, at low, mid and ship.",
                False,
            ),
            (
                "firmware/replay_paths",
                "One tinygrad step on every path on silicon, against firmware.",
                False,
            ),
            (
                "firmware/replay_seam",
                "ViT on the OS path, launch by launch. Each stamp adds about 11 k.",
                False,
            ),
            (
                "firmware/os_four_dies",
                "The OS across four dies at mid; four-worker speedup at low and mid.",
                False,
            ),
            (
                "firmware/os_clock",
                "OS operations at 200 against 100 MHz: above 1 means DDR4.",
                False,
            ),
            (
                "firmware/os_kernel_stages",
                "OS kernel on silicon: a system call's cycles by stage of the trap path.",
                False,
            ),
            (
                "firmware/os_primitives",
                "OS on silicon: what each kernel primitive costs, log scale.",
                False,
            ),
            (
                "firmware/dispatch_projection",
                "Launches a step times each path's cost, over run cycles. A projection.",
                False,
            ),
        ),
    ),
    (
        "waste",
        "Waste",
        "What a step holds beyond the math, from the data flow of each trace.",
        (
            (
                "waste/ladder",
                "A step against its serial floors; grey has no floor that explains it.",
                False,
            ),
            (
                "waste/recoverable",
                "Share of a step each detector names as removable.",
                False,
            ),
            (
                "waste/chains",
                "Form-change sequences between two computations, by cost.",
                True,
            ),
        ),
    ),
    (
        "units",
        "Inside the units",
        "Traced Verilator model over small kernels only, never a card run.",
        (
            (
                "units/traced",
                "Vector dispatch stalls, vector DMA states, cluster sequencer states.",
                True,
            ),
        ),
    ),
    (
        "compiler",
        "Compiler",
        "Where a compile's time goes.",
        (("compiler/stages", "Compile time by stage over the corpus.", True),),
    ),
    (
        "model",
        "Timed model",
        "The model against the card, kernel by kernel.",
        (
            ("model/lenet.z", "LeNet: each dot a kernel; bars by kind.", False),
            ("model/vit.z", "ViT: each dot a kernel; bars by kind.", False),
            ("model/mixer.z", "Mixer: each dot a kernel; bars by kind.", False),
            ("model/gru.z", "GRU: each dot a kernel; bars by kind.", False),
        ),
    ),
)
RUNS = (
    "lenet.z",
    "lenet.zj",
    "lenet-bn.z",
    "lenet-nonorm.z",
    "vit.z",
    "mlp.z",
    "mlp.zj",
    "mixer.z",
    "mixer.zj",
    "gpt.z",
    "gpt.zj",
    "gru.z",
    "gru.zj",
    "wide.z",
)
RUN_FIGURES = (
    (
        "gantt",
        "One warm step on the node's time axis. Gaps are the node waiting.",
        True,
    ),
    ("kinds", "Cycles by kernel kind, and how many had a unit busy.", False),
    ("seam", "What sits between kernels, and every warm step of the run.", False),
    ("pareto", "Largest launches; pale is no unit busy.", True),
)

STYLE = """
:root { color-scheme: light; --surface:#fcfcfb; --ink:#0b0b0b; --second:#52514e; --line:#e1e0d9; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --surface:#1a1a19; --ink:#f0efec; --second:#c3c2b7; --line:#2c2c2a; } }
:root[data-theme="dark"] { color-scheme: dark; --surface:#1a1a19; --ink:#f0efec;
  --second:#c3c2b7; --line:#2c2c2a; }
body { margin:0; background:var(--surface); color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1500px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:26px; margin:0 0 6px; }
h2 { font-size:20px; margin:40px 0 4px; border-top:1px solid var(--line); padding-top:20px; }
h3 { font-size:15px; margin:24px 0 8px; color:var(--second); }
p { color:var(--second); margin:4px 0 12px; }
img { width:100%; height:auto; display:block; background:#fcfcfb; border-radius:4px; cursor:zoom-in; }
nav { display:flex; flex-wrap:wrap; gap:4px 14px; }
nav a { color:inherit; white-space:nowrap; }
.report td code { overflow-wrap:anywhere; }
.grid { display:grid; gap:24px 20px; align-items:start;
  grid-template-columns:repeat(auto-fit,minmax(min(100%,560px),1fr)); }
.grid.four { grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr)); }
.wide { grid-column:1/-1; }
figure { margin:0; min-width:0; }
figcaption { color:var(--second); font-size:13px; margin-top:6px; }
details { border-top:1px solid var(--line); padding:10px 0; }
summary { cursor:pointer; font-weight:600; }
details .grid { margin-top:12px; }
#zoom { display:none; position:fixed; inset:0; background:rgba(0,0,0,.8); padding:16px;
  box-sizing:border-box; cursor:zoom-out; overflow:auto; z-index:1; }
#zoom.on { display:block; }
#zoom img { background:#fcfcfb; max-width:none; width:100%; cursor:zoom-out; }
table { border-collapse:collapse; font-size:12px; width:100%; display:block; overflow-x:auto; }
th,td { border-bottom:1px solid var(--line); padding:3px 8px; text-align:right; white-space:nowrap; }
th:first-child,td:first-child { text-align:left; }
.report table { display:table; font-size:13px; }
.report th,.report td { text-align:left; white-space:normal; vertical-align:top; }
code { font-size:12px; }
"""


ZOOM = """
const zoom = document.getElementById('zoom');
document.querySelectorAll('figure img').forEach(i => i.addEventListener('click', () => {
  zoom.firstChild.src = i.src; zoom.classList.add('on');
}));
zoom.addEventListener('click', () => zoom.classList.remove('on'));
"""


def table() -> str:
    path = TABLES / "zoo.csv"
    if not path.exists():
        return ""
    with path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    keep = [i for i, k in enumerate(rows[0]) if k != "run"]
    head = "".join(f"<th>{html.escape(rows[0][i])}</th>" for i in keep)
    body = ""
    for r in rows[1:]:
        cells = ""
        for i in keep:
            try:
                v = float(r[i])
                text = f"{v:,.3g}" if abs(v) < 1 else f"{v:,.0f}"
            except ValueError:
                text = r[i]
            cells += f"<td>{html.escape(text)}</td>"
        body += f"<tr>{cells}</tr>"
    return f"<h2 id='raw'>Raw table</h2><table><tr>{head}</tr>{body}</table>"


def report() -> str:
    """`REPORT.md`'s headings, paragraphs and tables as the page's first section."""
    out, rows = "<div class='report'>", []
    lines = (ROOT / "REPORT.md").read_text("utf-8").splitlines()
    for line in [*lines, ""]:
        if line.startswith("|"):
            if "---" not in line:
                rows.append([c.strip() for c in line.strip("|").split("|")])
            continue
        if rows:
            cells = ["".join(f"<td>{code(c)}</td>" for c in r) for r in rows[1:]]
            head = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
            body = "".join(f"<tr>{c}</tr>" for c in cells)
            out += f"<table><tr>{head}</tr>{body}</table>"
            rows = []
        if line.startswith("# "):
            out += f"<h2 id='report'>{html.escape(line[2:])}</h2>"
        elif line.startswith("## "):
            out += f"<h3>{html.escape(line[3:])}</h3>"
        elif line:
            out += f"<p>{code(line)}</p>"
    return out + "</div>"


def code(text: str) -> str:
    parts = html.escape(text).split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def round_figure(name: str) -> str:
    """A figure's path in this round: a run's folder and name carry the round's tag."""
    return name.replace(".z", f".{ROUND}") if ROUND != "z" else name


def figure(name: str, caption: str, wide: bool = False) -> str:
    path = FIGURES / f"{round_figure(name)}.png"
    if not path.exists():
        print("no figure", path)
        return ""
    data = base64.b64encode(path.read_bytes()).decode()
    kind = " class='wide'" if wide else ""
    text = html.escape(caption)
    return (
        f"<figure{kind}><img loading='lazy' alt='{text}' "
        f"src='data:image/png;base64,{data}'><figcaption>{text}</figcaption></figure>"
    )


def note(name: str, caption: str) -> list[str]:
    path = FIGURES / f"{round_figure(name)}.png"
    if not path.exists():
        return []
    return [f"![{caption}]({path.relative_to(HOME).as_posix()})", "", caption, ""]


def main():
    page, readme = (
        "",
        [
            "# Measurements",
            "",
            "Open `index.html`. Findings: `REPORT.md`. Plan: `PLAN.md`. Issues: `ISSUES.md`.",
            "",
        ],
    )
    nav = ""
    for anchor, title, what, figures in SECTIONS:
        nav += f"<a href='#{anchor}'>{html.escape(title)}</a>"
        page += f"<h2 id='{anchor}'>{html.escape(title)}</h2><p>{html.escape(what)}</p>"
        page += "<div class='grid'>"
        readme += [f"## {title}", "", what, ""]
        for name, caption, wide in figures:
            page += figure(name, caption, wide)
            readme += note(name, caption)
        page += "</div>"
    nav += "<a href='#runs'>Each run</a>"
    page += (
        "<h2 id='runs'>Each run</h2><p>One warm step of each model at mid, "
        "eager and TinyJit. Open a run for its four charts.</p>"
    )
    readme += ["## Each run", ""]
    for at, run in enumerate(RUNS):
        run = round_figure(run)
        cards = ""
        readme += [f"### {run}", ""]
        for stem, caption, wide in RUN_FIGURES:
            cards += figure(f"runs/{run}/{stem}", caption, wide)
            readme += note(f"runs/{run}/{stem}", caption)
        mark = " open" if at == 0 else ""
        page += (
            f"<details{mark}><summary>{html.escape(run)}</summary>"
            f"<div class='grid'>{cards}</div></details>"
        )
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>KohakuTPU Measurements</title><style>{STYLE}</style></head><body><main>"
        "<h1>KohakuTPU measurements</h1><p>Every measurement of the path from tinygrad "
        f"and the DSL to the card units. Click a chart to enlarge it.</p>"
        f"<nav><a href='#report'>Findings</a>{nav}"
        f"<a href='#raw'>Raw table</a></nav>{report()}{page}{table()}"
        f"</main><div id='zoom'><img alt=''></div><script>{ZOOM}</script></body></html>"
    )
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "index.html").write_text(doc, "utf-8")
    (HOME / "README.md").write_text("\n".join(readme) + "\n", "utf-8")


def matrix_index(rows):
    """Current-schema matrix index: explicit metrics, failures and raw trace links."""
    HOME.mkdir(parents=True, exist_ok=True)
    lead = (
        "Only complete eager warm inputs or observed retained-replay inputs are scored. "
        "The launch-max sum is NOT a global interval. Global bounds enclose input "
        "upload, all participating nodes and the workload's output readback; they "
        "include host work. HR_CYCLES bounds use one reference node, never subtract "
        "different boot epochs. Retained combined regions are measured as kind graph; "
        "a constituent-kernel breakdown is unavailable, not borrowed from capture. "
        "Model cycles and waste are estimates over the actual selected compiled regions. "
        "P1 speedup holds path fixed; eager speedup holds P fixed. Global speedups use "
        "host bounds, separately from the cycle-sum speedups. Unscored attempts remain linked."
    )
    md = [
        f"# Round {ROUND}",
        "",
        lead,
        "",
        "[Matrix table](data/matrix.csv) · [Per-kind table](data/matrix-kinds.csv)",
        "",
    ]
    for figure in ("intervals", "kinds"):
        if (FIGURES / "matrix" / f"{figure}.png").exists():
            md += [f"![{figure}](figures/matrix/{figure}.png)", ""]
    md += [
        "| Run / input group | Status | Launch-max sum | Global host bound (ms) |",
        "|---|---|---:|---:|",
    ]
    for row in rows:
        target = "../../data/" + row["run"]
        wall = row["global_host_bound_ns"]
        md.append(
            f"| [{row['run']}]({target}/meta.json) / {row['group']} | {row['status']} | {row['launch_max_sum'] or '—'} | {wall / 1e6 if wall is not None else '—'} |"
        )
        if row["reason"]:
            md += ["", f"{row['run']}: `{row['reason'].replace('`', chr(39))}`", ""]
    text = "\n".join(md) + "\n"
    (HOME / "README.md").write_text(text, "utf-8")
    # HTML shares the same source text and figure links, no legacy zoo assumptions.
    images = "".join(
        f'<img style="max-width:100%" src="figures/matrix/{name}.png" alt="{name}">'
        for name in ("intervals", "kinds")
        if (FIGURES / "matrix" / f"{name}.png").exists()
    )
    links = '<p><a href="data/matrix.csv">Matrix CSV</a> · <a href="data/matrix-kinds.csv">Kinds CSV</a></p>'
    (HOME / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Round '
        + html.escape(ROUND)
        + "</title>"
        + links
        + images
        + '<pre style="white-space:pre-wrap">'
        + html.escape(text)
        + "</pre>",
        "utf-8",
    )


if __name__ == "__main__":
    main()
