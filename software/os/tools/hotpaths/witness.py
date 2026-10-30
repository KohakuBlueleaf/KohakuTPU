"""One card-model semantic mutation witness; consumes exactly one model run."""

import argparse
import json
import pathlib
import subprocess
import sys

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from build import build
from measure import KNOBS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=("batch", "event"))
    ap.add_argument("--model", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    mutant = 7 if a.kind == "batch" else 9
    defines = [f"-D{k}" for k in KNOBS] + [f"-DKOS_HOT_MUT={mutant}"]
    sizes = build("kos", a.out, defines)
    result_path = a.out / "result.json"
    command = "mg 4" if a.kind == "batch" else "mg 0; kevent"
    board = "card_v9t6_2n" if a.kind == "batch" else "card_v9t6_1n"
    p = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "run_card.py"),
            "--board",
            board,
            "--model",
            str(a.model),
            "--image",
            str(a.out / "kos.elf"),
            "--cmd",
            command,
            "--flags",
            "engine",
            "--timeout",
            "360",
            "--out",
            str(a.out / "run"),
            "--json",
            str(result_path),
        ],
        check=False,
    )
    result = json.loads(result_path.read_text())
    text = result["nodes"][0]["log"]
    if a.kind == "batch":
        killed = "FAIL mg: unit 4 engine round" in text and "retired 3 -> 5" in text
    else:
        killed = (
            "FAIL kevent: unit 0" in text
            and result["measures"]["0"]["kevent.failures"] > 0
        )
    killed = (
        killed and p.returncode == 1 and all(nd["status"] & 8 for nd in result["nodes"])
    )
    result.update(mutant=mutant, killed=bool(killed), sizes=sizes, defines=defines)
    result_path.write_text(json.dumps(result, indent=2))
    print(f"mutation {mutant}: {'KILLED' if killed else 'NOT WITNESSED'}")
    return 0 if killed else 1


if __name__ == "__main__":
    sys.exit(main())
