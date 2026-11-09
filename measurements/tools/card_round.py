"""One bounded attempt per matrix point, with durable stop/resume semantics.

No build, bitstream load or clock change is performed. The caller selects the
attached board and supplies its read-clocks readback and synced-tree identity.
Run --help; PLAN.md describes the matrix schema and the complete round.
"""

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PERF = REPO / "software/tests/perf"
sys.path[:0] = [
    str(PERF),
    str(REPO / "software/kohakutpu/application"),
    str(REPO / "software/kohakutpu/os/tools"),
    str(REPO / "software/kohakuaccel/os/tools"),
]

import klog
import record
from boot import load, regions
from kohakuaccel.driver.device import rv64load
from kohakutpu.application.runtime import offline as O
from kohakutpu.application.runtime.graph import split_recorded
from kohakutpu.application.runtime.graph.split import PreparedSplitGraph
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph, task_capacity
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart
from run_card import Card

AXES = ("workload", "board", "sysnodes", "batch", "path", "inputs", "blocks")
CARD_SIGNATURE = re.compile(
    r"TimeoutError|FirmwareFatal|timed out|\bBUSY\b|RecordSkew|NodeError|ClockError|actual card clocks differ|"
    r"DE.feed.*(?:failed|-7)|KDS.*(?:fail|halt|exit)|xdma.*(?:timeout|error)|"
    r"(?<![a-z])(?:AER:|PCIe Bus Error)",
    re.IGNORECASE,
)


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def configurations(spec):
    rows = (
        spec
        if isinstance(spec, list)
        else [
            dict(zip(AXES, values))
            for values in itertools.product(
                *(spec[k] if isinstance(spec[k], list) else [spec[k]] for k in AXES)
            )
        ]
    )
    result, seen = [], set()
    for row in rows:
        point = {k: row[k] for k in AXES}
        if set(row) != set(AXES):
            raise ValueError(f"each point must have exactly {AXES}")
        if point["board"] not in ("multimesh_v9t6", "multimesh_v9t8"):
            raise ValueError("unsupported board")
        if point["path"] not in ("firmware", "eager", "kaos"):
            raise ValueError("unknown execution path")
        if point["sysnodes"] not in (1, 2, 4):
            raise ValueError("sysnodes must be 1, 2 or 4")
        for key in ("batch", "inputs", "blocks"):
            if not isinstance(point[key], int) or point[key] < 1:
                raise ValueError(f"{key} must be a positive integer")
        if not re.fullmatch(r"[a-z0-9-]+", point["workload"]):
            raise ValueError("invalid workload name")
        identity = json.dumps(point, sort_keys=True)
        if identity in seen:
            raise ValueError("duplicate matrix point")
        seen.add(identity)
        result.append(point)
    return result


def validate_clocks(clocks, board, profile):
    if not isinstance(clocks, dict) or set(clocks) != {str(i) for i in range(4)}:
        raise ValueError("read-clocks must name exactly dies 0..3")
    rates = board["profiles"][profile]
    expected = {
        k: board["vco_mhz"] / math.ceil(board["vco_mhz"] / rate)
        for k, rate in rates.items()
    }
    for node, readback in clocks.items():
        if not isinstance(readback, dict) or readback.get("locked") is not True:
            raise ValueError(f"die {node} lacks a locked read-clocks readback")
        if any(abs(readback.get(k, 0) - rate) > 1e-6 for k, rate in expected.items()):
            raise ValueError(
                f"die {node} readback differs from recorder profile {profile}"
            )
    if len({json.dumps(v, sort_keys=True) for v in clocks.values()}) != 1:
        raise ValueError("all four die readbacks must agree")


def name(point, round_name):
    return (
        f"{point['workload']}.{round_name}.{point['path']}.b{point['batch']}."
        f"i{point['inputs']}.k{point['blocks']}.{point['board']}.{point['sysnodes']}n"
    )


def classify(returncode, text, *, timed_out=False):
    # Signatures stop even if a wrapper accidentally returned zero.
    if timed_out or CARD_SIGNATURE.search(text):
        return "card_failure"
    return "software_failure" if returncode else "complete"


def run_matrix(points, root, round_name, execute):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".driver-lock"
    try:
        lock.mkdir()
    except FileExistsError:
        dump(
            root / "STOP.json",
            {"reason": "concurrent or interrupted driver; not rerun"},
        )
        return 2
    try:
        return _run_matrix(points, root, round_name, execute)
    finally:
        lock.rmdir()


def _run_matrix(points, root, round_name, execute):
    """Pure scheduling policy; execute(point, directory) returns an outcome dict.

    A claimed but unfinished attempt is never repeated. Its uncertain card
    state stops the round, including after restarting this process.
    """
    root.mkdir(parents=True, exist_ok=True)
    stop = root / "STOP.json"
    if stop.exists():
        return 2
    software_failed = False
    for point in points:
        if stop.exists():
            return 2
        directory = root / name(point, round_name)
        if directory.exists():
            receipt = directory / "attempt.json"
            state = json.loads(receipt.read_text("utf-8")) if receipt.exists() else {}
            status = state.get("status")
            if status in ("complete", "software_failure"):
                software_failed |= status == "software_failure"
                continue
            dump(
                stop,
                {
                    "trace": directory.name,
                    "reason": "unfinished or card-failed attempt; not rerun",
                },
            )
            return 2
        # Atomic claim protects against a second driver using the same round.
        directory.mkdir()
        dump(
            directory / "attempt.json",
            {"point": point, "status": "started", "started": time.time()},
        )
        try:
            outcome = execute(point, directory)
        # Any failure, an interrupt included, leaves the card state unknown: it is
        # written to disk and stops the round as a card failure.
        except BaseException:  # noqa: BLE001
            failure = traceback.format_exc()
            # This file is owned by the driver even if the privileged child
            # died before handing ownership of its diagnostics back.
            (directory / "driver-failure.txt").write_text(failure, encoding="utf-8")
            if not (directory / "failure.txt").exists():
                (directory / "failure.txt").write_text(failure, encoding="utf-8")
            outcome = {
                "status": "card_failure",
                "reason": "driver interrupted; card state unknown",
            }
        dump(
            directory / "attempt.json", dict(point=point, ended=time.time(), **outcome)
        )
        if outcome["status"] == "card_failure":
            dump(stop, dict(trace=directory.name, **outcome))
            return 2
        software_failed |= outcome["status"] == "software_failure"
    return 1 if software_failed else 0


def bounded(command, log, env, seconds, grace):
    """TERM the entire Linux process group, then KILL; retain untruncated output."""
    with log.open("wb") as stream:
        process = subprocess.Popen(
            command,
            cwd=REPO,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=seconds), False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=grace)
            return process.returncode or 124, True
        finally:
            # Only an interrupted wait leaves the group running.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=grace)


def supervise(payload):
    """Run INSIDE the privilege boundary, so TERM/KILL reaches card children."""
    out = Path(payload["out"])
    receipt = Path(payload["receipt"])
    outcome = {"returncode": 125, "timed_out": True}
    try:
        code, timeout = bounded(
            payload["command"],
            Path(payload["log"]),
            dict(os.environ),
            payload["seconds"],
            payload["grace"],
        )
        outcome = {"returncode": code, "timed_out": timeout}
    # Any failure is written to disk and reported through the receipt's default
    # outcome (timed out), which the driver classifies as a card failure.
    except BaseException:  # noqa: BLE001
        (out / "supervisor-failure.txt").write_text(traceback.format_exc(), "utf-8")
    finally:
        dump(receipt, outcome)
        # Host-specific account names never appear here. Restore the invoking
        # IDs after success, TERM or KILL, before the host-side pack touches files.
        if os.geteuid() == 0:
            for path in [*out.rglob("*"), out]:
                if not path.is_symlink():
                    os.chown(path, payload["uid"], payload["gid"])
    return 0


def supervised(command, log, env, seconds, grace, prefix, python, directory):
    receipt = directory / (log.stem + ".process.json")
    payload = {
        "command": command,
        "log": str(log),
        "receipt": str(receipt),
        "out": str(directory),
        "seconds": seconds,
        "grace": grace,
        "uid": os.getuid(),
        "gid": os.getgid(),
    }
    wrapper = [
        *prefix,
        python,
        str(Path(__file__).resolve()),
        "_supervise",
        json.dumps(payload),
    ]
    # The root supervisor bounds the actual recorder; this outer bound also
    # catches sudo/authentication/supervisor failure, conservatively stopping.
    got = subprocess.run(
        wrapper, cwd=REPO, env=env, check=False, timeout=seconds + 3 * grace + 30
    )
    if got.returncode or not receipt.exists():
        raise RuntimeError("privileged supervisor did not finalize; card state unknown")
    result = json.loads(receipt.read_text("utf-8"))
    return result["returncode"], result["timed_out"]


def preflight(record_args, destination, nodes, capacity):
    """Offline translate every launch and every retained bound region; fail closed."""
    cap = task_capacity(capacity)
    destination.mkdir(parents=True, exist_ok=True)
    stream = (destination / "preflight.jsonl").open("w", encoding="utf-8")
    counts = {"launches": 0, "retained": 0, "refused": 0}

    def checked(artifact, machine, kind):
        units, types = {}, {}
        for mesh in machine.meshes:
            if mesh.index >= nodes:
                continue
            for unit_kind, coords in mesh.units.items():
                for coord in coords:
                    key = (mesh.index, *coord)
                    units[key] = len(units)
                    types[key] = int.from_bytes(unit_kind.encode(), "big")
        row = {"kind": kind, "nodes": [p.node for p in artifact.parts]}
        try:
            wait_window = json.loads(machine.machine_json)["machine"]["flaws"]["F-004"][
                "present"
            ]
            row["wait_window"] = wait_window
            row["package_sha256"] = {
                p.node: hashlib.sha256(p.package).hexdigest() for p in artifact.parts
            }
            graphs = Graph.package_parts(
                artifact,
                units,
                node_ids={p.node: p.node for p in artifact.parts},
                unit_types=types,
                wait_window=wait_window,
            )
            row["parts"] = {}
            for node, graph in graphs.items():
                raw = graph.pack(0)
                row["parts"][node] = {"tasks": len(graph.tasks), "bytes": len(raw)}
                if (
                    not 0 < len(graph.tasks) <= cap
                    or len(raw) > cap * A.OSTPU_TASK_BYTES
                ):
                    raise ValueError(f"node {node}: task capacity {cap} exceeded")
            counts[kind] += 1
        except BaseException as error:
            counts["refused"] += 1
            row["refused"] = repr(error)
            raise
        finally:
            stream.write(json.dumps(row) + "\n")
            stream.flush()

    built = O.B.built

    def on_built(progs, machine, *args, **kwargs):
        both = built(progs, machine, *args, **kwargs)
        artifact = BoundGraphArtifact(
            "preflight",
            tuple(NodePart(n, pkg.to_bytes()) for n, (_, pkg, _) in both.items()),
        )
        checked(artifact, machine, "launches")
        return both

    O.B.built = on_built
    graph_submit = PreparedSplitGraph.submit

    def on_graph(self):
        checked(self.bind_artifact(), self.device.machine, "retained")
        return graph_submit(self)

    PreparedSplitGraph.submit = on_graph

    # Offline runs use no real backend, but prepare the SAME split regions even
    # at P=1. The artifact is bound and translated without uploading or issuing it.
    def prepare(self, log):
        if log.split is None:
            log.split = split_recorded.prepare(log)

    record.D.Device.prepare_recorded = prepare
    replay = record.D.Device.replay

    def on_replay(self, log, *args, **kwargs):
        self.prepare_recorded(log)
        return replay(self, log, *args, **kwargs)

    record.D.Device.replay = on_replay
    sys.argv = ["record.py", *record_args, "--jit", "--out", str(destination)]
    try:
        code = record.main()
        return code or int(
            not counts["launches"] or not counts["retained"] or counts["refused"] > 0
        )
    finally:
        stream.close()
        dump(destination / "preflight-summary.json", counts)


def klog_read(board_name, nodes, out):
    """Read logs only; never load, boot, stop, recover or rerun a failed card."""
    board = load(board_name)
    card = Card(board, None, out / "klog-read.log", xdma=True)
    rows = []
    try:
        for node in range(nodes):
            base, size = regions(board, node)[0][:2]
            window = rv64load.LoadWindow(
                card.t, card.m["ctrl"][board["mesh_of"][node]] + rv64load.WINDOW_OFFSET
            )
            rows.append(
                dict(
                    node=node,
                    status=window.status(),
                    **klog.parse(card.read(base + klog.offset(size), klog.LOG)),
                )
            )
            dump(out / "klog-read.json", rows)
    finally:
        card.t.close()


def main():
    # Internal subprocess steps share this tool; no host-only companion scripts.
    if len(sys.argv) > 1 and sys.argv[1] == "_supervise":
        return supervise(json.loads(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1] == "_preflight":
        payload = json.loads(sys.argv[2])
        return preflight(
            payload["args"], Path(payload["out"]), payload["nodes"], payload["capacity"]
        )
    if len(sys.argv) > 1 and sys.argv[1] == "_klog":
        klog_read(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("round")
    ap.add_argument("matrix", type=Path)
    ap.add_argument(
        "--board", required=True, choices=("multimesh_v9t6", "multimesh_v9t8")
    )
    ap.add_argument(
        "--clocks", type=Path, help="bringup read-clocks JSON for the attached board"
    )
    ap.add_argument("--commit", default=os.environ.get("PERF_COMMIT"))
    ap.add_argument("--python", default=os.environ.get("PERF_PYTHON", sys.executable))
    ap.add_argument(
        "--sudo",
        default=os.environ.get("PERF_SUDO", ""),
        help="e.g. 'sudo -n'; empty when already authorized",
    )
    ap.add_argument("--xdma-ko", default=os.environ.get("XDMA_KO"))
    ap.add_argument(
        "--xdma-params",
        default=os.environ.get("XDMA_PARAMS", "c2h_timeout=3 h2c_timeout=3"),
    )
    ap.add_argument("--profile", default=os.environ.get("KOHAKUTPU_PROFILE", "mid"))
    ap.add_argument("--library", type=Path)
    ap.add_argument(
        "--kos-image",
        type=Path,
        default=Path(os.environ.get("KOS_IMAGE", REPO / "build/os/kos.elf")),
    )
    ap.add_argument("--os-board", help="OS board spec matching the attached board")
    ap.add_argument("--os-capacity", type=int, default=65534)
    ap.add_argument("--seconds", type=float, default=3600)
    ap.add_argument("--grace", type=float, default=20)
    ap.add_argument("--min-replays", type=int, default=8)
    ap.add_argument("--traces", type=Path, default=REPO / "build/perf/traces")
    ap.add_argument("--measurements", type=Path, default=REPO / "measurements")
    ap.add_argument("--post-only", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]+", a.round):
        ap.error("round must be a simple tag")
    points = configurations(json.loads(a.matrix.read_text("utf-8")))
    points = [p for p in points if p["board"] == a.board]
    if not points:
        ap.error("matrix has no point for the attached board")
    if a.seconds <= 0 or a.grace <= 0 or a.min_replays < 1:
        ap.error("positive bounds and minimum replay count required")
    root = a.traces.resolve() / a.round
    if not a.post_only:
        clocks = json.loads(
            a.clocks.read_text("utf-8")
            if a.clocks
            else os.environ.get("PERF_CLOCKS", "null")
        )
        if not a.commit:
            ap.error("PERF_COMMIT must name the synced tree")
        try:
            board_spec = json.loads(
                (REPO / "boards" / f"{a.board}.json").read_text("utf-8")
            )
            validate_clocks(clocks, board_spec, a.profile)
        except (ValueError, KeyError) as error:
            ap.error(str(error))
        if any(p["path"] == "kaos" for p in points) and not a.os_board:
            ap.error("--os-board is required for KAOS")
        if any(
            p["path"] != "eager" and p["inputs"] < a.min_replays + 4 for p in points
        ):
            ap.error(
                "retained points require inputs >= min-replays + 4; observed count is also checked"
            )
        env = dict(
            os.environ,
            PERF_COMMIT=a.commit,
            PERF_CLOCKS=json.dumps(clocks),
            PYTHONUNBUFFERED="1",
            KOHAKUTPU_PROFILE=a.profile,
            XDMA_PARAMS=a.xdma_params,
            RAYON_NUM_THREADS="4",
        )
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(REPO / "software/kohakutpu/application"),
                str(PERF),
                env.get("PYTHONPATH", ""),
            ]
        )
        if a.xdma_ko:
            env["XDMA_KO"] = a.xdma_ko
        # sudo may strip env. Pass only needed/perf runtime variables explicitly.
        passed = {
            k: v
            for k, v in env.items()
            if k.startswith(("PERF_", "KTPU_", "KOHAKUTPU_", "XDMA_", "KOS_"))
            or k
            in ("PYTHONPATH", "PYTHONUNBUFFERED", "RAYON_NUM_THREADS", "PATH", "HOME")
        }
        prefix = shlex.split(a.sudo)
        privileged = (
            prefix + ["env", *(f"{k}={v}" for k, v in passed.items())] if prefix else []
        )
        provenance = {
            "commit": a.commit,
            "clocks": clocks,
            "profile": a.profile,
            "matrix_sha256": hashlib.sha256(a.matrix.read_bytes()).hexdigest(),
        }
        root.mkdir(parents=True, exist_ok=True)
        provenance_file = root / f"provenance.{a.board}.json"
        if (
            provenance_file.exists()
            and json.loads(provenance_file.read_text("utf-8")) != provenance
        ):
            ap.error(
                "round provenance changed; use a new round, never overwrite an attempt"
            )
        dump(provenance_file, provenance)

        def execute(point, directory):
            common = [
                point["workload"],
                "--board",
                point["board"],
                "--nodes",
                str(point["sysnodes"]),
                "--batch",
                str(point["batch"]),
                "--steps",
                str(point["inputs"]),
                "--blocks",
                str(point["blocks"]),
            ]
            if a.library:
                common += ["--library", str(a.library.resolve())]
            fallback = dict(
                schema=2,
                round=a.round,
                workload=point["workload"],
                board=point["board"],
                nodes=point["sysnodes"],
                batch=point["batch"],
                blocks=point["blocks"],
                path=point["path"],
                args=common,
                done="started",
                **provenance,
            )
            dump(directory / "provenance.json", fallback)
            if point["path"] == "kaos":
                payload = {
                    "args": common,
                    "out": str(directory / "preflight"),
                    "nodes": point["sysnodes"],
                    "capacity": a.os_capacity,
                }
                code, timeout = bounded(
                    [
                        a.python,
                        str(Path(__file__).resolve()),
                        "_preflight",
                        json.dumps(payload),
                    ],
                    directory / "preflight.log",
                    env,
                    a.seconds,
                    a.grace,
                )
                if code or timeout:
                    # This step is offline: even a timeout is a software failure.
                    reason = "offline KAOS translation failed or exceeded its bound; card not opened"
                    (directory / "failure.txt").write_text(reason, "utf-8")
                    dump(
                        directory / "klog.json",
                        {"status": "not opened", "reason": reason},
                    )
                    dump(directory / "meta.json", dict(fallback, done=reason))
                    return {
                        "status": "software_failure",
                        "returncode": code,
                        "step": "preflight",
                    }
            command = [
                a.python,
                str(PERF / "record.py"),
                *common,
                "--card",
                "--measure",
                "--out",
                str(directory),
                "--round",
                a.round,
                "--min-replays",
                str(a.min_replays),
            ]
            if point["path"] != "eager":
                command += ["--backend", point["path"]]
            if point["path"] == "kaos":
                command += [
                    "--os-board",
                    a.os_board,
                    "--kos-image",
                    str(a.kos_image.resolve()),
                    "--os-capacity",
                    str(a.os_capacity),
                ]
            dump(directory / "command.json", command)
            code, timeout = supervised(
                command,
                directory / "run.log",
                env,
                a.seconds,
                a.grace,
                privileged,
                a.python,
                directory,
            )
            text = (directory / "run.log").read_text("utf-8", errors="replace")
            failure = directory / "failure.txt"
            if failure.exists():
                text += failure.read_text("utf-8", errors="replace")
                code = code or 1
            metadata_path = directory / "meta.json"
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text("utf-8"))
                if metadata.get("done") != "exited":
                    code = code or 1
                text += str(metadata.get("done", ""))
            else:
                code = code or 1
                dump(metadata_path, dict(fallback, done="recorder did not finalize"))
            for diagnostic in [
                directory / "klog.json",
                *directory.glob("kds-failures*.jsonl"),
            ]:
                if diagnostic.exists():
                    text += diagnostic.read_text("utf-8", errors="replace")
            status = classify(code, text, timed_out=timeout)
            if status != "complete":
                with failure.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"\ndriver: {status}, returncode={code}, timeout={timeout}\n"
                    )
                if point["path"] == "kaos" and not (directory / "klog.json").exists():
                    kcode, ktimeout = supervised(
                        [
                            a.python,
                            str(Path(__file__).resolve()),
                            "_klog",
                            a.os_board,
                            str(point["sysnodes"]),
                            str(directory),
                        ],
                        directory / "klog-read.stderr",
                        env,
                        min(a.seconds, 15),
                        a.grace,
                        privileged,
                        a.python,
                        directory,
                    )
                    dump(
                        directory / "klog-status.json",
                        {"returncode": kcode, "timed_out": ktimeout},
                    )
                    diagnostics = "".join(
                        p.read_text("utf-8", errors="replace")
                        for p in (
                            directory / "klog-read.json",
                            directory / "klog-read.stderr",
                        )
                        if p.exists()
                    )
                    if (
                        classify(kcode, diagnostics, timed_out=ktimeout)
                        == "card_failure"
                    ):
                        status = "card_failure"
                elif not (directory / "klog.json").exists():
                    dump(
                        directory / "klog.json",
                        {
                            "status": "not applicable: firmware console is in run.log/failure.txt"
                        },
                    )
            return {"status": status, "returncode": code, "timed_out": timeout}

        code = run_matrix(points, root, a.round, execute)
    else:
        code = 0
    # Offline postprocessing only, including failed traces. A stop never starts
    # another card job. Post can be resumed independently without any card run.
    post = subprocess.run(
        [
            a.python,
            str(Path(__file__).with_name("host_round.py")),
            a.round,
            str(a.measurements.resolve()),
            "--traces",
            str(root),
        ],
        cwd=REPO,
        check=False,
    )
    report_env = dict(
        os.environ, MEASURE_ROUND=a.round, MEASURE_ROOT=str(a.measurements.resolve())
    )
    report = subprocess.run(
        [a.python, str(Path(__file__).with_name("build_round.py"))],
        cwd=REPO,
        env=report_env,
        check=False,
    )
    return code or post.returncode or report.returncode


if __name__ == "__main__":
    sys.exit(main())
