//! Complete band differentials use actual template emission, rotation and allocation.

use super::*;
use crate::lower::vector::{assign_typed, banks, encode, modulo::Core, pat};
use crate::{Program, Search, Strategy, PASSES};
use ktpu_machine::Machine;
use std::collections::BTreeMap;
use std::sync::Mutex;

static COVERAGE: Mutex<BTreeMap<String, usize>> = Mutex::new(BTreeMap::new());
static GOLDEN_RUN: Mutex<()> = Mutex::new(());

fn count(key: impl Into<String>) {
    *COVERAGE.lock().unwrap().entry(key.into()).or_default() += 1;
}

// Injective names isolate pre-bank emission; postbank separately checks real allocation.
fn resolve(t: &mut Term) {
    match t {
        Term::Name(n) if n.starts_with('%') => *n = format!("v{}", &n[1..]),
        Term::View(n, args) | Term::Call(n, args) => {
            if n.starts_with('%') {
                *n = format!("v{}", &n[1..]);
            }
            for a in args {
                resolve(a);
            }
        }
        Term::Assign(a, b) | Term::AddAssign(a, b) => {
            resolve(a);
            resolve(b);
        }
        Term::Neg(a) | Term::Kw(_, a) => resolve(a),
        Term::Slice(a, b) => {
            for t in a.iter_mut().chain(b.iter_mut()) {
                resolve(t);
            }
        }
        _ => (),
    }
}

fn paired(a: &Gen, z: &Emitter) -> (Vec<String>, Vec<ir::Event>) {
    assert_eq!(a.n, z.n as usize);
    assert_eq!(a.code.len(), z.code.len());
    let mut resolved = z.code.clone();
    let mut lines = vec![];
    for (a, z) in a.code.iter().zip(&mut resolved) {
        let line = match (a, z) {
            (Code::Op(tmpl, old), ir::Event::Op { stmt, syms }) => {
                assert!(stmt.body.is_empty(), "emission must remain flat");
                assert_eq!(*old, syms.iter().map(ir::Symbol::name).collect::<Vec<_>>());
                for a in &mut stmt.args {
                    resolve(a);
                }
                let line = pat::format(
                    tmpl,
                    &old.iter()
                        .map(|s| format!("v{}", &s[1..]))
                        .collect::<Vec<_>>(),
                );
                assert_eq!(
                    print::emit(std::slice::from_mut(stmt)).unwrap(),
                    format!("{line}\n")
                );
                line
            }
            (Code::Raw(raw), ir::Event::Fixed(stmt)) => {
                assert!(stmt.body.is_empty(), "emission must remain flat");
                assert_eq!(
                    print::emit(std::slice::from_mut(stmt)).unwrap(),
                    format!("{raw}\n")
                );
                count(&stmt.op);
                raw.clone()
            }
            (Code::Raw(raw), ir::Event::Open) => {
                assert_eq!(raw, "{");
                raw.clone()
            }
            (Code::Raw(raw), ir::Event::Close) => {
                assert_eq!(raw, "}");
                raw.clone()
            }
            _ => panic!("event kind mismatch: {a:?}"),
        };
        lines.push(line);
    }
    (lines, resolved)
}

#[allow(clippy::too_many_arguments)]
pub(in crate::lower::vector::ctx::band) fn compare(
    old: &Ctx,
    a: &Gen,
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    t: &Target,
    band: usize,
    remat: bool,
    ahead: bool,
    plan: &Plan,
    land: Option<i64>,
    hold: bool,
    pin: bool,
    serial: bool,
) {
    let (new, z) = flat_band(
        body, par, env, t, band, remat, ahead, plan, land, hold, pin, serial,
    )
    .unwrap_or_else(|e| panic!("typed band refused legacy success {name}: {e:?}"));
    postbank(old, &new, a, &z, name, t);
    let (lines, events) = paired(a, &z);
    let (expected, params, accesses) = image_text(old, name, &lines);
    let (module, got_params, got_accesses) = image(&new, name, &events).unwrap();
    assert_eq!(module.source, expected, "complete pre-bank image {name}");
    assert_eq!(got_params, params);
    assert_eq!(got_accesses, accesses);
    let parsed = encode::read_image(&expected, name, body.vregs).unwrap();
    assert_eq!(module, parsed, "nested blocks and source maps {name}");
    assert_eq!(
        format!("{module:?}"),
        format!("{parsed:?}"),
        "float spellings"
    );
    assert_eq!(format!("{:?}", old.vals), format!("{:?}", new.vals));
    assert_eq!(old.scalars, new.scalars);
    assert_eq!(old.held, new.held);
    assert_eq!(old.pinned, new.pinned);
    assert_eq!(old.staged, new.staged);
    assert_eq!(format!("{:?}", old.descs), format!("{:?}", new.descs));
    assert_eq!(old.of, new.of);
    assert_eq!(old.rel, new.rel);
    assert_eq!(
        (old.tile, old.per_band, old.inplace),
        (new.tile, new.per_band, new.inplace)
    );
    count("images");
    count(format!("band={band}"));
    count(format!("remat={remat}"));
    count(format!("ahead={ahead}"));
    count(format!("hold={hold}"));
    count(format!("pin={pin}"));
    count(format!("trips={}", old.trips.len().min(5)));
    if old.descs.iter().any(|d| !d.to.is_empty()) {
        count("peer");
    }
    if old.descs.iter().any(|d| d.count) {
        count("peer-count");
    }
    if old.descs.iter().any(|d| d.inc < 0) {
        count("negative-inc");
    }
    if old.rel.is_some() {
        count("relative");
    }
}

fn postbank(old: &Ctx, new: &Ctx, a: &Gen, z: &Emitter, name: &str, t: &Target) {
    let core = Core::of(t);
    let rho = banks::choose(&a.code, old.body.vcopies, &core);
    assert_eq!(
        banks::typed::choose(&z.code, old.body.vcopies, &core),
        rho,
        "rho {name}"
    );
    if !rho.is_empty() {
        count("bank-rotated");
    }
    let legacy = banks::rotate(a.code.clone(), old.body.vcopies, &core);
    let typed = banks::typed::rotate(z.code.clone(), old.body.vcopies, &core);
    let (expected, old_trace) = assign_typed::reference(&legacy, &old.pinned, old.body.vregs);
    let (got, new_trace) = assign_typed::assign(&typed, &new.pinned, old.body.vregs);
    assert_eq!(
        new_trace, old_trace,
        "every symbol allocation, including partial failures {name}"
    );
    count("bank-assigned");
    match (expected, got) {
        (Ok(lines), Ok(events)) => {
            let (text, params, accesses) = image_text(old, name, &lines);
            let (module, gp, ga) = image(new, name, &events).unwrap();
            assert_eq!(module.source, text, "complete post-bank image {name}");
            assert_eq!(gp, params);
            assert_eq!(ga, accesses);
            count("bank-images");
        }
        (Err(a), Err(z)) => {
            assert_eq!(
                format!("{a:?}"),
                format!("{z:?}"),
                "assignment refusal {name}"
            );
            count("bank-refusals");
        }
        (a, z) => panic!("assignment outcome mismatch {name}: {a:?} / {z:?}"),
    }
}

#[test]
fn header_descriptors_and_source_maps() {
    let b = Body {
        name: "k".into(),
        params: vec![],
        scratch: IndexMap::new(),
        stmts: vec![],
        reader: Default::default(),
        macros: IndexMap::new(),
        buffers: IndexMap::new(),
        vregs: 64,
        vcopies: 2,
        fill: (0, 1),
    };
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::of(&m);
    for counted in [false, true] {
        let mut c = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        c.unit = "vc[0]".into();
        c.plan.insert(
            0,
            PlanX::T(crate::lower::links::Transfer {
                src: c.unit.clone(),
                dst: "mg[3]".into(),
                side: "A".into(),
                at: 19,
                size: 8,
                shape: vec![4, 32],
                flow: counted,
                slots: vec![],
                per: 0,
            }),
        );
        c.descs = vec![
            Desc {
                ad: 7,
                buffer: Some("x".into()),
                off: 64,
                inc: -256,
                walk: Walk(vec![(32, 8), (-256, 2)]),
                ..Default::default()
            },
            Desc {
                ad: 2,
                to: "mg[3]".into(),
                off: 19,
                count: counted,
                walk: Walk::flat(32, 8),
                ..Default::default()
            },
            Desc {
                ad: 4,
                buffer: Some("y".into()),
                off: -32,
                inc: 256,
                walk: Walk::flat(32, 4),
                ..Default::default()
            },
        ];
        let events = vec![
            ir::Event::Fixed(ir::seti("s15", Term::int(2))),
            ir::Event::Fixed(ir::instruction("loop", vec![Term::name("s15")], vec![])),
            ir::Event::Open,
            ir::Event::Fixed(ir::instruction("skipz", vec![Term::name("s14")], vec![])),
            ir::Event::Open,
            ir::Event::Fixed(ir::instruction("vbar", vec![], vec![])),
            ir::Event::Close,
            ir::Event::Close,
            ir::Event::Fixed(ir::instruction("halt", vec![], vec![])),
        ];
        let lines = [
            "seti s15 = 2",
            "loop s15",
            "{",
            "skipz s14",
            "{",
            "vbar",
            "}",
            "}",
            "halt",
        ]
        .map(str::to_string);
        let (expected, params, accesses) = image_text(&c, "k.nested", &lines);
        let (got, gp, ga) = image(&c, "k.nested", &events).unwrap();
        assert_eq!(got.source, expected);
        assert_eq!(gp, params);
        assert_eq!(ga, accesses);
        assert_eq!(
            got,
            encode::read_image(&expected, "k.nested", b.vregs).unwrap()
        );
        assert_eq!(got.images["k.nested"].params, ["p_a7", "p_a4"]);
        let leaf = &got.images["k.nested"].stmts[6].body[0].body[0];
        assert_eq!(
            (&leaf.op, leaf.line, leaf.col),
            (&"vbar".to_string(), 10, 13)
        );
    }
}

fn control_edges() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::of(&m);
    for trips in 1..=5 {
        let text = format!("target ktpu.v9\n\nfn edges.l2(x: f16[40, 128] @dram, y: f16[40, 128] @dram)\n    par c in 0..2 on vc[c]\n        when c == 0\n            for t in 0..{trips}\n                xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n                v = unpack xl : f32[8, 128] @reg\n                p = mul v, 2.5 : f32[8, 128] @reg\n                if sticky\n                    p = mul p, 3.5 : f32[8, 128] @reg\n                ol = pack p : f16[8, 128] @l1\n                store y[t*8 : +8, :] <- ol\n");
        let p = Program::parse(&text, "<edges>", None).unwrap();
        let search = Search::new(Strategy::Band).forced(Some(false), Some(false));
        crate::compile_with(&p, &t, &[], false, &search).unwrap();
    }
}

#[test]
fn golden_prebank_bands() {
    golden_bands();
}

#[cfg(feature = "mutants")]
#[test]
#[ignore = "exhaustive pre-bank witness: run explicitly; normal test covers every golden with normal caches"]
fn golden_prebank_exhaustive() {
    crate::mutant::scoped(
        crate::mutant::State {
            active: Some("l2.vector/search-again"),
            ..Default::default()
        },
        golden_bands,
    );
}

fn golden_bands() {
    let _guard = GOLDEN_RUN.lock().unwrap();
    COVERAGE.lock().unwrap().clear();
    crate::lower::vector::search::modulo_typed::reset();
    control_edges();
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/l2");
    let mut paths: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|p| p.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let mut cases = 0;
    for path in paths {
        let case: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let Some(text) = case["l2"].as_str() else {
            continue;
        };
        let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
        let t = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
        let search = Search::new(Strategy::Auto);
        let result = Program::parse(text, "<golden.l2>", case["name"].as_str())
            .and_then(|p| crate::compile_with(&p, &t, &PASSES, false, &search));
        match result {
            Ok(got) => assert_eq!(
                got.l1_text,
                case["l1"].as_str().unwrap(),
                "{}",
                path.display()
            ),
            Err(e) if e.message.contains(crate::lower::vector::RUL_030) => {
                // A RUL.030 refusal is right only where the golden records a refusal.
                assert!(
                    !case["refusal"].is_null(),
                    "{}: RUL.030 refusal where the golden compiles",
                    path.display()
                );
            }
            Err(e) => {
                let r = &case["refusal"];
                assert_eq!(
                    (&e.message, e.line as i64, e.col as i64),
                    (
                        &r["message"].as_str().unwrap().to_string(),
                        r["line"].as_i64().unwrap(),
                        r["col"].as_i64().unwrap()
                    ),
                    "{}",
                    path.display()
                );
            }
        }
        cases += 1;
    }
    assert_eq!(cases, 192);
    crate::lower::vector::search::modulo_typed::report(cases);
    let coverage = COVERAGE.lock().unwrap();
    eprintln!("pre-bank witness: {cases} cases; {coverage:?}");
    eprintln!(
        "bank witness: {cases} cases; assigned={} images={} refusals={} rotated={}; mismatches=0",
        coverage["bank-assigned"],
        coverage["bank-images"],
        coverage["bank-refusals"],
        coverage["bank-rotated"]
    );
    assert_eq!(coverage["bank-assigned"], coverage["images"]);
    assert_eq!(
        coverage["bank-images"] + coverage["bank-refusals"],
        coverage["images"]
    );
    for key in [
        "bank-assigned",
        "bank-images",
        "bank-refusals",
        "bank-rotated",
        "images",
        "loop",
        "skipz",
        "getstk",
        "vwait",
        "vpost",
        "uptr",
        "uinc",
        "peer",
        "peer-count",
        "relative",
        "band=1",
        "band=2",
        "band=4",
        "band=8",
        "band=16",
        "remat=false",
        "remat=true",
        "ahead=false",
        "ahead=true",
        "hold=false",
        "hold=true",
        "pin=false",
        "pin=true",
    ] {
        assert!(coverage.get(key).copied().unwrap_or(0) > 0, "missing {key}");
    }
}
