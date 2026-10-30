//! Every launch's ports lie in the storages bound to them: each f16 or f32
//! port's extent from its bound offset ends within its storage's words, for every
//! recorded plan run and `kp` log, under the default contraction knobs and
//! under every knob on, and for padded products of the tensor session.

mod common;

use std::collections::{BTreeMap, HashMap};

use common::goldens;
use ktpu_kernels::gen::matmul::{defaults, Knobs};
use ktpu_machine::Machine;
use ktpu_plan::input::Digest;
use ktpu_plan::tensor::{Arg, Session};
use ktpu_plan::{lower, run, Case, Step};

/// Every knob away from its default.
fn every_knob() -> Knobs {
    Knobs {
        pad_m: true,
        n_tiles: 2,
        dense_n: true,
        pad_k: true,
    }
}

/// The ports of `steps`' launches that end past their storage's last whole
/// word (`sizes`: each storage's bytes; RUL.020: an allocation holds its
/// last word whole).
fn overruns(steps: &[Step], sizes: &BTreeMap<String, i64>, m: &Machine) -> Vec<String> {
    let word = ktpu_plan::hw::Hw::of(m).word;
    let mut out = Vec::new();
    for s in steps {
        let Step::Launch(l) = s else { continue };
        let Some(call) = &l.call else { continue };
        let k = ktpu_kernels::call::call(&call.gen, &call.args, m)
            .unwrap_or_else(|e| panic!("{}: {e}", call.gen));
        for p in &k.graph.ports {
            let item = match p.spec.dtype.name() {
                "f16" => 2,
                "f32" => 4,
                _ => continue,
            };
            let Some(at) = l.binds.get(&p.name) else {
                continue;
            };
            if at.form != ktpu_plan::exec::flat() {
                continue;
            }
            let end = at.offset + item * p.spec.shape.iter().product::<i64>();
            let bytes = *sizes
                .get(&at.buf)
                .unwrap_or_else(|| panic!("{}: no storage {}", call.gen, at.buf));
            let have = (bytes + word - 1) / word * word;
            if end > have {
                out.push(format!(
                    "{} port {} {:?}: ends at {end} of {} ({have} bytes)",
                    call.gen, p.name, p.spec.shape, at.buf
                ));
            }
        }
    }
    out
}

/// Each storage's bytes, a constant table's its array's.
fn sizes(x: &ktpu_plan::exec::Exec) -> BTreeMap<String, i64> {
    let mut out: BTreeMap<String, i64> = x
        .stores
        .iter()
        .map(|(k, s)| (k.clone(), s.nbytes))
        .collect();
    for s in &x.steps {
        if let Step::Table { at, array } = s {
            let item = match array.dtype.as_str() {
                "float16" | "int16" | "uint16" => 2,
                "float32" | "int32" | "uint32" => 4,
                "float64" | "int64" | "uint64" => 8,
                _ => 1,
            };
            out.insert(
                at.buf.clone(),
                at.offset + item * array.shape.iter().product::<i64>(),
            );
        }
    }
    out
}

/// Every recorded run and `kp` log under `knobs`: its overruns.
fn recorded(knobs: Knobs) -> Vec<String> {
    let mut bad = Vec::new();
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let m = case.machine();
        for r in &case.runs {
            let Ok(l) = lower::plan(&case.plans[r.plan].uops) else {
                continue;
            };
            let x = match run::execute_under(l, &r.state, &m, knobs) {
                Ok(x) | Err((_, x)) => x,
            };
            for o in overruns(&x.steps, &sizes(&x), &m) {
                bad.push(format!("{}: {o}", case.case));
            }
        }
        if case.kp.is_empty() {
            continue;
        }
        let mut s = Session::new(&m);
        s.r.knobs = knobs;
        let mut names: HashMap<String, usize> = HashMap::new();
        for k in &case.kp {
            if let (Ok(r), Some(name)) = (s.replay(&k.op, &k.args, &names), &k.out) {
                if let Some(t) = r.out {
                    names.entry(name.clone()).or_insert(t);
                }
            }
        }
        for o in overruns(&s.r.x.steps, &sizes(&s.r.x), &m) {
            bad.push(format!("{} (kp): {o}", case.case));
        }
    }
    bad
}

#[test]
fn every_recorded_launch_lies_in_its_storages() {
    let bad = recorded(defaults());
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

#[test]
fn every_recorded_launch_lies_in_its_storages_under_every_knob() {
    let bad = recorded(every_knob());
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

/// `bt` [m, k] activations times a [n, k] weight plus a [bt, m, n]
/// residual, realised in a session under `knobs`.
fn product(m: &Machine, knobs: Knobs, (bt, rows, n, k): (i64, i64, i64, i64)) -> Session<'_> {
    let mut s = Session::new(m);
    s.r.knobs = knobs;
    let host = |bytes: i64| Digest {
        bytes,
        sha256: format!("{bytes:064x}"),
    };
    let a = s
        .tensor(&[bt, rows, k], "float16", host(2 * bt * rows * k))
        .unwrap();
    let w = s.tensor(&[n, k], "float16", host(2 * n * k)).unwrap();
    let r = s
        .tensor(&[bt, rows, n], "float16", host(2 * bt * rows * n + 2))
        .unwrap();
    let wt = s.transpose(w).unwrap();
    let p = s.matmul(a, wt).unwrap();
    let y = s.ew("add", &[Arg::T(p), Arg::T(r)]).unwrap();
    s.numpy(y).unwrap();
    s
}

#[test]
fn a_padded_product_lies_in_its_storages() {
    let m = Machine::named("multimesh_v9t6")
        .and_then(|m| m.with_nodes(1))
        .unwrap();
    for (knobs, shape) in [
        (defaults(), (2, 6, 64, 64)),
        (every_knob(), (1, 1576, 768, 768)),
        (every_knob(), (3, 197, 192, 64)),
    ] {
        let s = product(&m, knobs, shape);
        let mm =
            s.r.x
                .steps
                .iter()
                .filter_map(|st| match st {
                    Step::Launch(l) => l.call.as_ref().filter(|c| c.gen == "matmul.mm_kernel"),
                    _ => None,
                })
                .count();
        assert_eq!(mm, 1, "{shape:?}: one product");
        let bad = overruns(&s.r.x.steps, &sizes(&s.r.x), &m);
        assert!(bad.is_empty(), "{shape:?}: {}", bad.join("\n"));
    }
}

/// Under `memory/unsafe-bounds` the product binds its unpadded storages and
/// the kernel's ports run past them: the output always, a residual where a
/// later batch's rows start past its storage's.
#[cfg(feature = "mutants")]
#[test]
fn an_unpadded_product_runs_past_its_storages() {
    let m = Machine::named("multimesh_v9t6")
        .and_then(|m| m.with_nodes(1))
        .unwrap();
    for (knobs, shape, ports_past) in [
        (defaults(), (2, 6, 64, 64), &["in1", "out"][..]),
        (every_knob(), (1, 1576, 768, 768), &["out"][..]),
    ] {
        let bad = ktpu_plan::mutant::with("memory/unsafe-bounds", || {
            let s = product(&m, knobs, shape);
            overruns(&s.r.x.steps, &sizes(&s.r.x), &m)
        });
        let ports: Vec<&str> = bad
            .iter()
            .filter_map(|o| o.strip_prefix("matmul.mm_kernel port "))
            .map(|o| &o[..o.find(' ').unwrap()])
            .collect();
        assert_eq!(ports, ports_past, "{shape:?}: {bad:?}");
    }
}
