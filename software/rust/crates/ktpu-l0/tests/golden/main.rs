//! Every case of `software/rust/golden/l0/` (written by
//! `software/rust/golden/l0_dump.py` from the Python stack) rebuilt here and
//! matched byte for byte: package bytes, parsed and bound packages, lowered
//! packages, builder and program packages, boot blocks, mover writes.

#[path = "../common/mod.rs"]
mod common;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use common::*;
use ktpu_l0::boot::BootArgs;
use ktpu_l0::build::{AddrField, Addresses, Increment};
use ktpu_l0::mover::{self, Dim, Move, Walk};
use ktpu_l0::program::{BuildOpts, Patch, Program, Stmt};
use ktpu_l0::{
    engine, Alu, Cond, Facts, IsaAddresses, Kind, Package, PackageBuilder, Refusal, Word,
};
use serde_json::{json, Value};

fn cases() -> Vec<PathBuf> {
    let dir = root().join("software/rust/golden/l0");
    let mut out = Vec::new();
    let mut stack = vec![dir];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap_or_else(|e| panic!("{d:?}: {e}")) {
            let p = e.unwrap().path();
            if p.is_dir() {
                stack.push(p);
            } else if p.extension().is_some_and(|x| x == "json") {
                out.push(p);
            }
        }
    }
    out.sort();
    out
}

/// `{"bytes": hex}` of a package's bytes, or `{"refused": ...}`.
fn raw(f: &Facts, p: std::result::Result<Package, Refusal>) -> Value {
    match p.and_then(|p| p.to_bytes(f)) {
        Ok(b) => json!({ "bytes": hex_bytes(&b) }),
        Err(e) => json!({ "refused": e.to_string() }),
    }
}

/// Whether `got` agrees with the golden `want`: equal, or both refused.
fn agree(got: &Value, want: &Value) -> bool {
    match (got.get("refused"), want.get("refused")) {
        (Some(_), Some(_)) => true,
        (None, None) => got.get("bytes") == want.get("bytes"),
        _ => false,
    }
}

fn check_raw(errs: &mut Vec<String>, what: &str, got: Value, want: &Value) {
    if !agree(&got, want) {
        errs.push(format!("{what}: got {got}, want {want}"));
    }
}

/// A built package's outputs: bytes, lowered, bound then lowered, checksummed.
fn check_package_outputs(
    errs: &mut Vec<String>,
    f: &Facts,
    pkg: &Package,
    binds: &[u64],
    out: &Value,
) {
    if package_json(pkg) != out["package"] {
        errs.push(format!(
            "package: got {}, want {}",
            package_json(pkg),
            out["package"]
        ));
    }
    check_raw(errs, "bytes", raw(f, Ok(pkg.clone())), out);
    check_raw(
        errs,
        "lowered",
        raw(f, engine::lower(pkg, f)),
        &out["lowered"],
    );
    let bl = pkg.bind(binds).and_then(|b| engine::lower(&b, f));
    if let (Ok(b), Ok(low)) = (pkg.bind(binds), &bl) {
        if let Err(e) = ktpu_l0::replay::check(&b, low, f) {
            errs.push(format!("bound_lowered refused by its replay: {e}"));
        }
    }
    check_raw(errs, "bound_lowered", raw(f, bl), &out["bound_lowered"]);
    let ck = Package {
        checksum: true,
        ..pkg.clone()
    };
    check_raw(errs, "checksummed", raw(f, Ok(ck)), &out["checksummed"]);
}

// --------------------------------------------------------------- addresses
/// The runner's address function: each field a list of `[bit, width, shift]`
/// segments, its value the segments' bits put back at their shifts.
struct FieldList(Vec<Vec<(u32, u32, u32)>>);

impl FieldList {
    fn of(v: &Value) -> FieldList {
        FieldList(
            v.as_array()
                .unwrap()
                .iter()
                .map(|f| {
                    f.as_array()
                        .unwrap()
                        .iter()
                        .map(|s| (u(&s[0]) as u32, u(&s[1]) as u32, u(&s[2]) as u32))
                        .collect()
                })
                .collect(),
        )
    }
}

impl Addresses for FieldList {
    fn fields(&self, word: &Word, _: &str) -> Vec<AddrField> {
        self.0
            .iter()
            .map(|segs| AddrField {
                segments: segs.clone(),
                value: segs
                    .iter()
                    .fold(0u64, |v, &(b, w, s)| v | word.get(b, w).wrapping_shl(s)),
            })
            .collect()
    }
}

/// `{unit type: fields | "mover"}`.
struct PerType {
    f: Facts,
    types: BTreeMap<String, Option<FieldList>>,
}

impl Addresses for PerType {
    fn fields(&self, word: &Word, unit_type: &str) -> Vec<AddrField> {
        match self.types.get(unit_type) {
            Some(Some(fl)) => fl.fields(word, unit_type),
            Some(None) => mover::addresses(&self.f, word),
            None => Vec::new(),
        }
    }
}

// ------------------------------------------------------------- package ops
fn run_package(f: &Facts, case: &Value, errs: &mut Vec<String>) {
    let src = package(&case["package"]);
    let out = &case["out"];
    match case["do"].as_str().unwrap() {
        "bytes" => check_raw(errs, "bytes", raw(f, Ok(src)), out),
        "parse" => {
            let data = from_hex_bytes(case["raw"].as_str().unwrap());
            match Package::from_bytes(&data, f) {
                Ok(got) => {
                    if package_json(&got) != out["parsed"] {
                        errs.push(format!(
                            "parsed: got {}, want {}",
                            package_json(&got),
                            out["parsed"]
                        ));
                    }
                    check_raw(errs, "rebytes", raw(f, Ok(got)), out);
                }
                Err(e) => check_raw(errs, "parse", json!({ "refused": e.to_string() }), out),
            }
        }
        "bind" => {
            let binds: Vec<u64> = case["bindings"].as_array().unwrap().iter().map(u).collect();
            match src.bind(&binds) {
                Ok(got) => {
                    if package_json(&got) != out["package"] {
                        errs.push(format!("bound: got {}", package_json(&got)));
                    }
                    check_raw(errs, "bytes", raw(f, Ok(got)), out);
                }
                Err(e) => check_raw(errs, "bind", json!({ "refused": e.to_string() }), out),
            }
        }
        "lower" => match engine::lower(&src, f) {
            Ok(got) => {
                if package_json(&got) != out["package"] {
                    errs.push(format!(
                        "lowered: got {}, want {}",
                        package_json(&got),
                        out["package"]
                    ));
                }
                check_raw(errs, "bytes", raw(f, Ok(got)), out);
            }
            Err(e) => check_raw(errs, "lower", json!({ "refused": e.to_string() }), out),
        },
        other => panic!("do {other}"),
    }
}

// ------------------------------------------------------------------ builder
fn coord(v: &Value) -> (u32, u32) {
    (u(&v[0]) as u32, u(&v[1]) as u32)
}

fn pairs(v: &Value) -> Vec<(u64, u64)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| (u(&p[0]), u(&p[1])))
        .collect()
}

fn opt_u(v: &Value) -> Option<u64> {
    (!v.is_null()).then(|| u(v))
}

fn call(b: &mut PackageBuilder, name: &str, a: &Value) -> Result<Value, Refusal> {
    let fl = a
        .get("addresses")
        .filter(|v| !v.is_null())
        .map(|v| FieldList::of(&v["fields"]));
    let addrs = fl.as_ref().map(|x| x as &dyn Addresses);
    let ui = |k: &str| u(&a[k]) as usize;
    let bo = |k: &str| a[k].as_bool().unwrap();
    let hexv = |n: usize| json!(hx(n as i128));
    Ok(match name {
        "unit" => {
            let t = a["type_name"].as_str();
            hexv(b.unit(coord(&a["coord"]), t, opt_u(&a["mesh"]))?)
        }
        "buffer" => hexv(b.buffer(
            a["name"].as_str().unwrap(),
            u(&a["size"]),
            u(&a["default"]),
            Kind::from_code(u(&a["kind"])).unwrap(),
        )),
        "spans_from" => {
            let live = a["live"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| (u(&p[0]), u(&p[1])))
                .collect();
            b.spans_from(&live);
            Value::Null
        }
        "payload" => hexv(b.payload(word(&a["word"]), addrs, a["unit_type"].as_str().unwrap())),
        "dispatch" => {
            b.dispatch(ui("unit"), &words(&a["words"]), addrs)?;
            Value::Null
        }
        "repeat" => {
            let incs: Vec<Increment> = a["increments"]
                .as_array()
                .unwrap()
                .iter()
                .map(|i| {
                    (
                        u(&i[0]) as usize,
                        u(&i[1]) as u32,
                        u(&i[2]) as u32,
                        u(&i[3]),
                    )
                })
                .collect();
            b.repeat(
                ui("unit"),
                &words(&a["template"]),
                u(&a["repeats"]),
                &incs,
                addrs,
            )?;
            Value::Null
        }
        "dispatch_periodic" => {
            let fields: Vec<(u32, u32)> = a["fields"]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| (u(&x[0]) as u32, u(&x[1]) as u32))
                .collect();
            b.dispatch_periodic(ui("unit"), &words(&a["words"]), &fields, addrs)?;
            Value::Null
        }
        "await_" => {
            b.await_(ui("unit"), u(&a["count"]));
            Value::Null
        }
        "barrier" => {
            b.barrier();
            Value::Null
        }
        "signal" => {
            b.signal(u(&a["value32"]), u(&a["value64"]));
            Value::Null
        }
        "settle" => {
            b.settle(u(&a["cycles"]));
            Value::Null
        }
        "ring" => {
            b.ring(u(&a["mesh"]), u(&a["slot"]), u(&a["amount"]), bo("fence"))?;
            Value::Null
        }
        "wait_bell" => {
            b.wait_bell(u(&a["mesh"]), u(&a["count"]));
            Value::Null
        }
        "wait_slot" => {
            b.wait_slot(u(&a["slot"]), u(&a["count"]), bo("consume"))?;
            Value::Null
        }
        "store" => {
            b.store(&pairs(&a["pairs"]), bo("from_regs"), bo("relocate"));
            Value::Null
        }
        "ctrl" => {
            b.ctrl(&pairs(&a["pairs"]), bo("from_regs"));
            Value::Null
        }
        "load" => {
            b.load(
                u(&a["reg"]),
                u(&a["address"]),
                opt_u(&a["index"]),
                bo("ctrl"),
                bo("relocate"),
            );
            Value::Null
        }
        "alu" => {
            let op = Alu::from_code(u(&a["op"])).unwrap();
            b.alu(u(&a["rd"]), op, u(&a["ra"]), u(&a["b"]), bo("imm"))?;
            Value::Null
        }
        "jump" => {
            let c = Cond::from_code(u(&a["cond"])).unwrap();
            hexv(b.jump(u(&a["target"]), c, u(&a["ra"]), u(&a["b"]), bo("imm")))
        }
        "repoint" => {
            b.repoint(ui("start"), u(&a["index"]));
            Value::Null
        }
        "retarget" => {
            b.retarget(ui("step"), u(&a["target"]));
            Value::Null
        }
        "patch" => {
            b.patch(
                u(&a["reg"]),
                u(&a["index"]),
                u(&a["bit"]),
                u(&a["width"]),
                u(&a["shift"]),
                bo("steps"),
                bo("add"),
            )?;
            Value::Null
        }
        "send" => {
            b.send(
                ui("unit"),
                u(&a["flit_type"]),
                &words(&a["words"]),
                bo("ack"),
            );
            Value::Null
        }
        "push" => {
            b.push(
                ui("unit"),
                word(&a["descriptor"]),
                u(&a["address"]),
                u(&a["words"]),
                bo("ack"),
                bo("relocate"),
            );
            Value::Null
        }
        "recv" => {
            b.recv(
                u(&a["address"]),
                u(&a["count"]),
                u(&a["unit"]),
                bo("relocate"),
            );
            Value::Null
        }
        "mover" => {
            b.mover(&pairs(&a["writes"]), addrs, bo("posted"));
            Value::Null
        }
        "fetch_from" => {
            b.fetch_from(ui("unit"), u(&a["addr"]), u(&a["count"]))?;
            Value::Null
        }
        "mwait" => {
            b.mwait(u(&a["moves"]));
            Value::Null
        }
        "reserve_acks" => {
            b.reserve_acks(u(&a["count"]));
            Value::Null
        }
        other => panic!("no builder call {other}"),
    })
}

fn run_build(f: &Facts, case: &Value, errs: &mut Vec<String>) {
    let spec = &case["builder"];
    let types = spec["types"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                (u(&t[0]) as u32, u(&t[1]) as u32),
                t[2].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let mut b = PackageBuilder::new(f, types, u(&spec["credit"]));
    b.signature = u(&spec["signature"]);
    b.mesh = u(&spec["mesh"]);
    b.fetch = (!spec["fetch"].is_null()).then(|| coord(&spec["fetch"]));
    let out = &case["out"];
    let mut results = Vec::new();
    for c in case["calls"].as_array().unwrap() {
        match call(&mut b, c[0].as_str().unwrap(), &c[1]) {
            Ok(r) => results.push(r),
            Err(e) => {
                check_raw(errs, "call", json!({ "refused": e.to_string() }), out);
                return;
            }
        }
    }
    let opts = &case["build"];
    let pkg = b.build(
        opts.get("defaults")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        opts.get("checksum")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    );
    if out.get("refused").is_some() {
        check_raw(errs, "build", raw(f, Ok(pkg)), out);
        return;
    }
    if json!(results) != out["results"] {
        errs.push(format!("results: got {results:?}, want {}", out["results"]));
    }
    let binds: Vec<u64> = b.bindings();
    let want: Vec<u64> = out["bindings"].as_array().unwrap().iter().map(u).collect();
    if binds != want {
        errs.push(format!("bindings: got {binds:?}, want {want:?}"));
    }
    check_package_outputs(errs, f, &pkg, &binds, out);
}

// ------------------------------------------------------------------ program
fn patches(v: &Value) -> Vec<Patch> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| Patch {
            word: u(&p[0]) as usize,
            segments: p[1]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| (u(&s[0]) as u32, u(&s[1]) as u32, u(&s[2]) as u32))
                .collect(),
            reg: u(&p[2]),
            konst: u(&p[3]),
        })
        .collect()
}

fn stmt(s: &Value) -> Stmt {
    let bo = |i: usize| s[i].as_bool().unwrap();
    match s[0].as_str().unwrap() {
        "send" => Stmt::Send {
            coord: coord(&s[1]),
            words: words(&s[2]),
            patches: patches(&s[3]),
        },
        "ring" => Stmt::Ring {
            mesh: u(&s[1]),
            slot: u(&s[2]),
            amount: u(&s[3]),
            fence: bo(4),
        },
        "wait_bell" => Stmt::WaitBell {
            mesh: u(&s[1]),
            count: u(&s[2]),
        },
        "wait_slot" => Stmt::WaitSlot {
            slot: u(&s[1]),
            count: u(&s[2]),
            consume: bo(3),
        },
        "store" => Stmt::Store {
            pairs: pairs(&s[1]),
            from_regs: bo(2),
        },
        "ctrl" => Stmt::Ctrl {
            pairs: pairs(&s[1]),
            from_regs: bo(2),
        },
        "load" => Stmt::Load {
            reg: u(&s[1]),
            address: u(&s[2]),
            index: opt_u(&s[3]),
            ctrl: bo(4),
        },
        "alu" => Stmt::Alu {
            rd: u(&s[1]),
            op: Alu::from_code(u(&s[2])).unwrap(),
            ra: u(&s[3]),
            b: u(&s[4]),
            imm: bo(5),
        },
        "label" => Stmt::Label(s[1].as_str().unwrap().to_string()),
        "jump" => Stmt::Jump {
            name: s[1].as_str().unwrap().to_string(),
            cond: Cond::from_code(u(&s[2])).unwrap(),
            ra: u(&s[3]),
            b: u(&s[4]),
            imm: bo(5),
        },
        "signal" => Stmt::Signal {
            value32: u(&s[1]),
            value64: u(&s[2]),
        },
        "settle" => Stmt::Settle(u(&s[1])),
        "send_raw" => Stmt::SendRaw {
            coord: coord(&s[1]),
            flit_type: u(&s[2]),
            words: words(&s[3]),
            ack: bo(4),
        },
        "push" => Stmt::Push {
            coord: coord(&s[1]),
            descriptor: word(&s[2]),
            address: u(&s[3]),
            words: u(&s[4]),
            ack: bo(5),
        },
        "recv" => Stmt::Recv {
            address: u(&s[1]),
            count: u(&s[2]),
            coord: (!s[3].is_null()).then(|| coord(&s[3])),
        },
        "mark" => Stmt::Mark {
            coord: coord(&s[1]),
            token: u(&s[2]) as usize,
        },
        "wait" => Stmt::Wait {
            coord: coord(&s[1]),
            token: opt_u(&s[2]).map(|t| t as usize),
        },
        "barrier" => Stmt::Barrier,
        "move" => Stmt::Move {
            writes: pairs(&s[1]),
            patches: patches(&s[2]),
        },
        "post" => Stmt::Post {
            writes: pairs(&s[1]),
            token: u(&s[2]) as usize,
            patches: patches(&s[3]),
        },
        "wait_moves" => Stmt::WaitMoves(u(&s[1]) as usize),
        "fetch" => Stmt::Fetch {
            coord: coord(&s[1]),
            addr: u(&s[2]),
            count: u(&s[3]),
            patches: patches(&s[4]),
        },
        other => panic!("no statement {other}"),
    }
}

fn run_program(f: &Facts, case: &Value, errs: &mut Vec<String>, path: &Path) {
    let out = &case["out"];
    if case["program"].is_null() {
        assert!(out.get("refused").is_some());
        return;
    }
    let prog = Program {
        steps: case["program"]
            .as_array()
            .unwrap()
            .iter()
            .map(stmt)
            .collect(),
        spans: BTreeMap::new(),
    };
    let spans: BTreeMap<u64, u64> = case["spans"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|p| (u(&p[0]), u(&p[1])))
        .collect();
    let per = case["addresses"].as_object().map(|o| PerType {
        f: f.clone(),
        types: o
            .iter()
            .map(|(k, v)| (k.clone(), (v != "mover").then(|| FieldList::of(v))))
            .collect(),
    });
    let isa = (case["addresses"] == "isa").then(|| {
        let m = ktpu_machine::Machine::named(case["machine"].as_str().unwrap()).unwrap();
        IsaAddresses::new(&m).unwrap()
    });
    let addresses = match (&per, &isa) {
        (Some(p), _) => Some(p as &dyn Addresses),
        (_, Some(i)) => Some(i as &dyn Addresses),
        _ => None,
    };
    let opts = BuildOpts {
        fetch: (!case["fetch"].is_null()).then(|| coord(&case["fetch"])),
        resident: None,
        addresses,
        spans: Some(&spans),
    };
    let b = match prog.build(f, opts) {
        Ok(b) => b,
        Err(e) => {
            check_raw(errs, "program", json!({ "refused": e.to_string() }), out);
            return;
        }
    };
    let pkg = b.build(true, false);
    if out.get("refused").is_some() {
        check_raw(errs, "program", raw(f, Ok(pkg)), out);
        return;
    }
    let binds = b.bindings();
    let want: Vec<u64> = out["bindings"].as_array().unwrap().iter().map(u).collect();
    if binds != want {
        errs.push(format!("bindings: got {binds:?}, want {want:?}"));
    }
    if let Ok(mode) = std::env::var("KTPU_L0_LOOP_REGOLD") {
        assert!(matches!(mode.as_str(), "audit" | "write"));
        if package_json(&pkg) != out["package"] {
            let name = path.file_name().unwrap().to_str().unwrap();
            assert!(
                matches!(
                    name,
                    "loop_patch_fetch_memops.json" | "L0.STEP.054.patch_through_r15.json"
                ),
                "unreviewed {name}"
            );
            let actual = package_json(&pkg);
            for key in [
                "units",
                "buffers",
                "relocs",
                "signature",
                "ack_reserve",
                "names",
            ] {
                assert_eq!(actual[key], out["package"][key], "{name}: {key}");
            }
            let old_payload = out["package"]["payloads"].as_array().unwrap();
            assert_eq!(
                &actual["payloads"].as_array().unwrap()[..old_payload.len()],
                old_payload,
                "data-plane payloads changed"
            );
            for op in ["0x1", "0x4", "0xc"] {
                let select = |v: &Value| {
                    v.as_array()
                        .unwrap()
                        .iter()
                        .filter(|s| s[0] == op)
                        .cloned()
                        .collect::<Vec<_>>()
                };
                assert_eq!(
                    select(&actual["steps"]),
                    select(&out["package"]["steps"]),
                    "{name}: dispatch/mover/fetch changed"
                );
            }
            let mut updated = case.clone();
            let target = &mut updated["out"];
            target["package"] = actual;
            target["bytes"] = raw(f, Ok(pkg.clone()))["bytes"].clone();
            target["lowered"] = raw(f, engine::lower(&pkg, f));
            target["bound_lowered"] = raw(f, pkg.bind(&binds).and_then(|p| engine::lower(&p, f)));
            target["checksummed"] = raw(
                f,
                Ok(Package {
                    checksum: true,
                    ..pkg.clone()
                }),
            );
            check_package_outputs(errs, f, &pkg, &binds, target);
            assert!(errs.is_empty());
            eprintln!(
                "LOOP AUDIT {name}: {} -> {} steps; {} -> {} payloads",
                out["package"]["steps"].as_array().unwrap().len(),
                pkg.steps.len(),
                old_payload.len(),
                pkg.payloads.len()
            );
            if mode == "write" {
                let saved = std::path::PathBuf::from(
                    std::env::var("KTPU_L0_REGOLD_BACKUP")
                        .expect("KTPU_L0_REGOLD_BACKUP: directory receiving the original goldens"),
                )
                .join(name);
                assert!(!saved.exists(), "original snapshot exists");
                std::fs::create_dir_all(saved.parent().unwrap()).unwrap();
                std::fs::copy(path, saved).unwrap();
                std::fs::write(
                    path,
                    format!("{}\n", serde_json::to_string_pretty(&updated).unwrap()),
                )
                .unwrap();
            }
            return;
        }
    }
    check_package_outputs(errs, f, &pkg, &binds, out);
}

// --------------------------------------------------------------------- boot
fn run_boot(f: &Facts, case: &Value, errs: &mut Vec<String>) {
    let a = &case["args"];
    let mut args = BootArgs::new(u(&a["queue"]), u(&a["timeout"]));
    for (k, slot) in [
        ("mesh", &mut args.mesh),
        ("scan", &mut args.scan),
        ("cq_depth", &mut args.cq_depth),
        ("flags", &mut args.flags),
    ] {
        if let Some(v) = a.get(k) {
            *slot = u(v);
        }
    }
    if let Some(us) = a.get("units") {
        args.units = us.as_array().unwrap().iter().map(u).collect();
    }
    let got = match args.pack(f) {
        Ok(b) => json!({ "bytes": hex_bytes(&b) }),
        Err(e) => json!({ "refused": e.to_string() }),
    };
    check_raw(errs, "boot", got, &case["out"]);
}

// -------------------------------------------------------------------- mover
fn walk(v: &Value) -> Walk {
    let (base, dims, windows) = match v {
        Value::Object(o) => (&o["base"], &o["dims"], Some(&o["windows"])),
        _ => (&v[0], &v[1], None),
    };
    let mut w = Walk::new(
        num(base),
        dims.as_array()
            .unwrap()
            .iter()
            .map(|d| {
                let mut x = Dim::new(num(&d[0]), num(&d[1]));
                if d.as_array().unwrap().len() == 4 {
                    x.axis = u(&d[2]);
                    x.astep = num(&d[3]);
                }
                x
            })
            .collect(),
    );
    if let Some(ws) = windows {
        w.windows = ws
            .as_array()
            .unwrap()
            .iter()
            .map(|p| (num(&p[0]), num(&p[1])))
            .collect();
    }
    w
}

fn pair(v: &Value) -> (i128, i128) {
    (num(&v[0]), num(&v[1]))
}

fn mover_call(f: &Facts, name: &str, a: &[Value]) -> Result<Value, Refusal> {
    let at = |i: usize, d: i128| a.get(i).map_or(d, num);
    let flag = |i: usize, d: bool| a.get(i).and_then(Value::as_bool).unwrap_or(d);
    let writes = match name {
        "copy" => mover::copy(f, num(&a[0]), num(&a[1]), num(&a[2]))?,
        "convert" => mover::convert(
            f,
            num(&a[0]),
            num(&a[1]),
            num(&a[2]),
            at(3, 1),
            at(4, 0),
            at(5, 8),
            at(6, 4),
        )?,
        "convert_walk" => mover::convert_walk(f, walk(&a[0]), walk(&a[1]), at(2, 1), at(3, 0))?,
        "fill" => mover::fill(f, walk(&a[0]), at(1, 0), at(2, mover::W32 as i128) as u64)?,
        "generate" => {
            mover::generate(f, walk(&a[0]), num(&a[1]), at(2, mover::W32 as i128) as u64)?
        }
        "gather" => mover::gather(
            f,
            num(&a[0]),
            pair(&a[1]),
            walk(&a[2]),
            pair(&a[3]),
            flag(4, false),
        )?,
        "scatter" => mover::scatter(
            f,
            walk(&a[0]),
            num(&a[1]),
            pair(&a[2]),
            pair(&a[3]),
            flag(4, false),
        )?,
        "move" => {
            let mut m = Move::new(u(&a[0]));
            m.src = a.get(1).filter(|v| !v.is_null()).map(walk);
            m.dst = a.get(2).filter(|v| !v.is_null()).map(walk);
            m.writes(f)?
        }
        "flag_byte" => {
            return Ok(json!(
                hx(mover::flag_byte(flag(0, true), at(1, 0))? as i128)
            ))
        }
        other => panic!("no mover call {other}"),
    };
    Ok(json!(writes
        .iter()
        .map(|&(r, v)| json!([hx(r as i128), hx(v as i128)]))
        .collect::<Vec<_>>()))
}

fn run_mover(f: &Facts, case: &Value, errs: &mut Vec<String>) {
    let calls = case["calls"].as_array().unwrap();
    let outs = case["out"]["results"].as_array().unwrap();
    for (c, want) in calls.iter().zip(outs) {
        let name = c[0].as_str().unwrap();
        match mover_call(f, name, c[1].as_array().unwrap()) {
            Ok(got) => {
                if want.get("writes") != Some(&got) {
                    errs.push(format!("mover {name}: got {got}, want {want}"));
                }
            }
            Err(e) => {
                if want.get("refused").is_none() {
                    errs.push(format!("mover {name}: refused {e}, want {want}"));
                }
            }
        }
    }
}

/// A ruling case: the runner's port on the vector's input gives the ruled
/// output the case states.
fn run_vector(case: &Value, errs: &mut Vec<String>) {
    let runner = case["runner"].as_str().unwrap();
    let (_, port) = ktpu_l0::spec::ports()
        .into_iter()
        .find(|(n, _)| *n == runner)
        .unwrap_or_else(|| panic!("no port {runner}"));
    let m = ktpu_machine::Machine::named(case["machine"].as_str().unwrap()).unwrap();
    match port(&case["input"], Some(&m)) {
        Ok(got) if got == case["expect"] => {}
        other => errs.push(format!(
            "{:?}: got {other:?}, want {}",
            case["rulings"], case["expect"]
        )),
    }
}

fn run(path: &Path) -> Vec<String> {
    let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let f = facts(case["machine"].as_str().unwrap());
    let mut errs = Vec::new();
    match case["do"].as_str().unwrap() {
        "bytes" | "parse" | "bind" | "lower" => run_package(&f, &case, &mut errs),
        "build" => run_build(&f, &case, &mut errs),
        "program" => run_program(&f, &case, &mut errs, path),
        "boot" => run_boot(&f, &case, &mut errs),
        "mover" => run_mover(&f, &case, &mut errs),
        "vector" => run_vector(&case, &mut errs),
        other => panic!("do {other}"),
    }
    errs
}

#[test]
fn every_l0_golden_case_matches_python_byte_for_byte() {
    let all = cases();
    assert!(all.len() >= 90, "only {} golden cases", all.len());
    let mut failed = Vec::new();
    for p in &all {
        let errs = std::panic::catch_unwind(|| run(p))
            .unwrap_or_else(|e| vec![format!("panicked: {:?}", e.downcast_ref::<String>())]);
        if !errs.is_empty() {
            let name = p.file_name().unwrap().to_string_lossy().to_string();
            failed.push(format!("{name}:\n  {}", errs.join("\n  ")));
        }
    }
    assert!(
        failed.is_empty(),
        "{} of {} golden cases differ:\n{}",
        failed.len(),
        all.len(),
        failed.join("\n")
    );
}
