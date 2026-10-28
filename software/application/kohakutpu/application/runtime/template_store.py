"""Certified class artifacts beside the exact-graph library.

Only the Rust binder writes package bytes. JSON storage here carries coverage,
content identity and stage versions; it never compiles or interprets instructions.
"""

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path

from kohakutpu.stack import rs

STORE_KEPT = 512
_STORE_CACHE: OrderedDict = OrderedDict()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TemplateStore:
    def __init__(self, root, machine_json, verification=None):
        self.root = Path(root)
        self.machine = machine_json
        self.verification = verification or os.environ.get(
            "KOHAKUTPU_TEMPLATE_VERIFICATION", "sample"
        )
        if self.verification not in ("sample", "proof", "both"):
            raise ValueError("template verification: sample, proof or both")
        self.proof_cap = int(os.environ.get("KOHAKUTPU_TEMPLATE_PROOF_CAP", "2048"))

    def identity(self, source, table, environment=None):
        metadata = json.loads(table)
        embedded = metadata.pop("environment", [])
        key = json.loads(rs._rs.library_key(source, self.machine))
        key["template_table"] = digest(metadata)
        key["environment"] = embedded if environment is None else environment
        return key

    def admit(self, source, package, table, bindings):
        assumed = json.loads(table)
        assumed["environment"] = json.loads(
            rs._rs.template_environment(package, bindings, self.machine)
        )
        key = self.identity(source, table, assumed["environment"])
        identifier = digest(key)
        table = json.dumps(assumed, separators=(",", ":"))
        certificate = json.loads(
            rs._rs.template_certificate(package, table, self.machine)
        )
        proof = None
        measured = {}
        if self.verification in ("proof", "both"):
            started = time.perf_counter()
            proof = json.loads(
                rs._rs.template_verify(
                    {0: package},
                    {0: table},
                    {0: bindings},
                    self.machine,
                    identifier,
                    mode="proof",
                    cap=self.proof_cap,
                )
            )
            measured["proof"] = time.perf_counter() - started
        coverage_kind = "sample"
        if self.verification == "proof" and proof["status"] == "PROVEN":
            coverage_kind = "proof"
            coverage = [{n: b["lo"] for n, b in assumed["parameters"].items()}]
        else:
            started = time.perf_counter()
            coverage = json.loads(
                rs._rs.template_verify(
                    {0: package},
                    {0: table},
                    {0: bindings},
                    self.machine,
                    identifier,
                    confined=True,
                )
            )
            measured["sample"] = time.perf_counter() - started
        entry = {
            "proof": proof,
            "verification_seconds": measured,
            "key": key,
            "package": package.hex(),
            "table": table,
            "certificate": certificate,
            "coverage": coverage,
            "coverage_kind": coverage_kind,
            "bindings": bindings,
        }
        entry["integrity"] = digest(entry)
        directory = self.root / key["machine"] / "classes"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{identifier}.json"
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if (
                json.loads(previous["table"]).get("environment")
                != assumed["environment"]
            ):
                raise ValueError(
                    "template identity already has a different translation environment"
                )
            previous_package, previous_table = self.load_id(
                identifier, expected_key=key
            )
            if previous_package != package or json.loads(previous_table) != assumed:
                raise ValueError(
                    "template identity already has different certified content"
                )
            return identifier
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
        # An index of content identities, not a replacement for certificate checks.
        index_path = directory / "index.json"
        index = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.exists()
            else {}
        )
        index[identifier] = key
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
        return identifier

    def load(self, source, table, environment=None):
        key = self.identity(source, table, environment)
        identifier = digest(key)
        return self.load_id(identifier, expected_key=key)

    def load_id(self, identifier, expected_key=None):
        machine_digest = (
            expected_key["machine"]
            if expected_key is not None
            else hashlib.sha256(self.machine.encode()).hexdigest()
        )
        path = self.root / machine_digest / "classes" / f"{identifier}.json"
        raw_file = path.read_bytes()
        no_cache = rs._MUTANT.get() is not None
        ckey = (str(path), raw_file, identifier, self.machine)
        if not no_cache and ckey in _STORE_CACHE:
            _STORE_CACHE.move_to_end(ckey)
            entry_key, result = _STORE_CACHE[ckey]
            if expected_key is not None and entry_key != expected_key:
                raise ValueError(
                    f"template certificate integrity/key mismatch: {identifier}"
                )
            return result
        entry = json.loads(raw_file.decode("utf-8"))
        checksum = entry.pop("integrity")
        if checksum != digest(entry) or (
            expected_key is not None and entry["key"] != expected_key
        ):
            raise ValueError(
                f"template certificate integrity/key mismatch: {identifier}"
            )
        package = bytes.fromhex(entry["package"])
        assumed = json.loads(entry["table"])
        environment = json.loads(
            rs._rs.template_environment(package, entry["bindings"], self.machine)
        )
        if assumed.get("environment") != environment or (
            "environment" in entry["key"]
            and (
                entry["key"]["environment"] != environment
                or digest(entry["key"]) != identifier
            )
        ):
            raise ValueError(f"missing or stale translation environment: {identifier}")
        actual = json.loads(
            rs._rs.template_certificate(package, entry["table"], self.machine)
        )
        if actual != entry["certificate"] or not entry["coverage"]:
            raise ValueError(f"missing or stale confinement certificate: {identifier}")
        result = (package, entry["table"])
        if not no_cache:
            _STORE_CACHE[ckey] = (entry["key"], result)
            while len(_STORE_CACHE) > STORE_KEPT:
                _STORE_CACHE.popitem(last=False)
        return result

    def proven(self, identifier, bindings, cap=65536):
        """Admit an explicit L0 box once; returned capability binds any in-box point.

        Addresses/extents are pinned. Changed translations need a new proof. This
        does not replace ClassLibrary's graph-to-package equivalence obligation.
        """
        package, table = self.load_id(identifier)
        return rs._rs.template_certify(
            package, table, list(bindings), self.machine, cap
        )

    def bind_handles(self, source, table, params, handles, translations):
        package, table = self.load(source, table)
        return rs._rs.bind(
            package,
            handles,
            self.machine,
            params=params,
            template_json=table,
            translations=translations,
        )
