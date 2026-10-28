"""Finite launch-domain dispatch into shared, certified class templates.

Each admitted launch keeps its allocation/L1 metadata but references shared template
bytes. Admission requires a Rust byte proof against the directly compiled launch;
lookup never invokes a compiler. Unknown launch keys fall through to exact entries.
"""

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from kohakutpu.application.runtime.template_store import TemplateStore, digest
from kohakutpu.language.dsl import TensorSpec
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import fmt
from kohakutpu.stack import rs

DISPATCH_KEPT = 512
_DISPATCH_CACHE: OrderedDict = OrderedDict()
_EXACT_BYTES_CACHE: OrderedDict = OrderedDict()


def structure(value):
    """Graph topology key; numeric launch values belong to the admitted domain."""
    if isinstance(value, dict):
        return {k: structure(v) for k, v in value.items() if k != "where"}
    if isinstance(value, list):
        return [structure(v) for v in value]
    if type(value) is int:
        return {"class_parameter": "integer"}
    return value


def _check_launch_ports(source, output, member):
    graph = json.loads(source)
    name = graph.get("name", "")
    if output.get("name") not in (name, f"t_{member}"):
        raise ValueError(
            f"class member output name {output.get('name')!r} does not match launch {member}"
        )
    expected = [
        (
            p["name"],
            TensorSpec.make(
                p["spec"]["shape"],
                p["spec"]["dtype"],
                {
                    k: (v["int"] if isinstance(v, dict) and "int" in v else v)
                    for k, v in p["spec"].get("layout", ())
                },
                p["spec"].get("space", "dram"),
            ).text(),
        )
        for p in graph.get("ports", ())
    ]
    actual = [
        (pname, fmt(ptype))
        for pname, ptype in M.signature(output["l2"], output["name"], "l2")
    ]
    if expected != actual:
        raise ValueError(
            f"class member port signature {actual} does not match launch ports {expected}"
        )


class ClassLibrary:
    def __init__(self, root, machine):
        self.root = Path(root)
        self.machine = machine
        self.store = TemplateStore(root, machine)

    def _key_path(self, source):
        key = json.loads(rs._rs.library_key(source, self.machine))
        member = key["class"]
        graph = json.loads(source)
        graph.pop("name", None)
        key["class"] = digest(structure(graph))
        key["dispatch_schema"] = 1
        return (
            key,
            member,
            self.root / key["machine"] / "dispatch" / f"{key['class']}.json",
        )

    def admit(self, source, output, templates, bindings):
        """Register a byte-proved launch; templates maps mesh to source/table/params.

        Templates must already have been admitted by TemplateStore. Metadata is
        taken from the direct compilation, not inferred from the template's shape.
        """
        if output["bound"] is not None or output["verdict"]["refused"]:
            raise ValueError("class launch must be verified and relocatable")
        if set(output["packages"]) != {0}:
            raise ValueError(
                "class launch admission currently requires a single mesh 0 package"
            )
        if set(templates) != set(output["packages"]):
            raise ValueError("one class template is required per launch mesh")
        key, member, path = self._key_path(source)
        check_equiv = rs._MUTANT.get() != "l0.template/misadmit"
        if check_equiv:
            _check_launch_ports(source, output, member)
        packages = {}
        for mesh, direct in output["packages"].items():
            ref = dict(templates[mesh])
            environment = json.loads(
                rs._rs.template_environment(
                    direct["bytes"], bindings[mesh], self.machine
                )
            )
            raw, table = self.store.load(ref["source"], ref["table"], environment)
            ref["environment"] = environment
            direct_buffers = rs._rs.package_read(
                direct["bytes"], self.machine, int(mesh)
            )["buffers"]
            buffer_sizes = [b["size"] for b in direct_buffers]
            table_with_sizes = dict(json.loads(table), buffer_sizes=buffer_sizes)
            if check_equiv:
                rs._rs.template_prove(
                    raw,
                    json.dumps(table_with_sizes),
                    direct["bytes"],
                    json.dumps(ref["params"]),
                    bindings[mesh],
                    self.machine,
                    "production launch admission",
                    int(mesh),
                )
            bound_direct = rs._rs.bind(
                direct["bytes"], bindings[mesh], self.machine, int(mesh), False
            )
            packages[str(mesh)] = {
                **{
                    k: v for k, v in direct.items() if k not in ("bytes", "images_json")
                },
                "template": ref,
                "bindings": list(bindings[mesh]),
                "bound_sha256": hashlib.sha256(bound_direct).hexdigest(),
                "buffer_sizes": buffer_sizes,
            }
        entry = dict(self._load(path)) if path.exists() else None
        if entry is None or entry.get("key") != key:
            entry = {"key": key, "members": {}}
        else:
            entry.pop("integrity", None)
            entry["members"] = dict(entry.get("members", {}))
        for pkg_meta in packages.values():
            ref = pkg_meta["template"]
            if "source" in ref and "table" in ref:
                tid = digest(
                    self.store.identity(ref["source"], ref["table"], ref["environment"])
                )
                pkg_meta["template"] = {"id": tid, "params": ref["params"]}
        previous = entry["members"].get(member)
        alternatives = (
            []
            if previous is None
            else [
                {k: v for k, v in previous.items() if k != "alternatives"},
                *previous.get("alternatives", []),
            ]
        )
        alternatives = [
            v
            for v in alternatives
            if v["packages"]["0"]["template"]["id"] != packages["0"]["template"]["id"]
        ]
        entry["members"][member] = {
            "alternatives": alternatives,
            "output": {k: v for k, v in output.items() if k != "packages"},
            "packages": packages,
        }
        entry["integrity"] = digest(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _load(path):
        raw = path.read_bytes()
        no_cache = rs._MUTANT.get() is not None
        ckey = (str(path), raw)
        if not no_cache and ckey in _DISPATCH_CACHE:
            _DISPATCH_CACHE.move_to_end(ckey)
            return _DISPATCH_CACHE[ckey]
        entry = json.loads(raw.decode("utf-8"))
        checksum = entry.pop("integrity")
        if checksum != digest(entry):
            raise ValueError("class launch integrity mismatch")
        if not no_cache:
            _DISPATCH_CACHE[ckey] = entry
            while len(_DISPATCH_CACHE) > DISPATCH_KEPT:
                _DISPATCH_CACHE.popitem(last=False)
        return entry

    def _exact_package_bytes(self, machine_digest, member, mesh):
        exact_path = self.root / machine_digest / f"{member}.json"
        if not exact_path.exists():
            return None
        raw = exact_path.read_bytes()
        ckey = (str(exact_path), raw, int(mesh))
        if ckey in _EXACT_BYTES_CACHE:
            _EXACT_BYTES_CACHE.move_to_end(ckey)
            return _EXACT_BYTES_CACHE[ckey]
        doc = json.loads(raw.decode("utf-8"))
        pkg = doc.get("compiled", {}).get("packages", {}).get(str(mesh))
        if pkg is None:
            return None
        b = pkg["bytes"]
        result = bytes.fromhex(b) if isinstance(b, str) else bytes(b)
        _EXACT_BYTES_CACHE[ckey] = result
        while len(_EXACT_BYTES_CACHE) > DISPATCH_KEPT:
            _EXACT_BYTES_CACHE.popitem(last=False)
        return result

    def lookup(self, source, variant=0):
        key, member, path = self._key_path(source)
        if not path.exists():
            return None
        entry = self._load(path)
        if entry["key"] != key:
            return None
        # The finite admitted domain is deliberate: no extrapolation of allocation
        # metadata or output decomposition from a matching topology alone.
        member_entry = entry["members"].get(member)
        if member_entry is None:
            return None
        if variant:
            alternatives = member_entry.get("alternatives", [])
            if variant > len(alternatives):
                return None
            member_entry = alternatives[variant - 1]
        output = dict(member_entry["output"])
        check_equiv = rs._MUTANT.get() != "l0.template/misadmit"
        if check_equiv:
            _check_launch_ports(source, output, member)
        output["packages"] = {}
        for mesh, package in member_entry["packages"].items():
            ref = package["template"]
            if "id" in ref:
                raw, table = self.store.load_id(ref["id"])
            else:
                raw, table = self.store.load(ref["source"], ref["table"])
            table_obj = json.loads(table)
            if "buffer_sizes" in package:
                table_obj["buffer_sizes"] = package["buffer_sizes"]
            table_json = json.dumps(table_obj)
            if check_equiv and "bindings" in package and "bound_sha256" in package:
                bound = rs._rs.bind(
                    raw,
                    package["bindings"],
                    self.machine,
                    int(mesh),
                    False,
                    params=ref["params"],
                    template_json=table_json,
                )
                if hashlib.sha256(bound).hexdigest() != package["bound_sha256"]:
                    raise ValueError(
                        "class launch bound package differs from admitted direct package"
                    )
            output["packages"][int(mesh)] = {
                **{
                    k: v
                    for k, v in package.items()
                    if k
                    not in (
                        "template",
                        "bindings",
                        "bound_sha256",
                        "buffer_sizes",
                    )
                },
                "bytes": raw,
                "images_json": json.dumps(table_obj.get("images")),
                "template_json": table_json,
                "template_params": dict(ref["params"]),
            }
        return output
