//! Rust ports of the tier-0 L2 runners (`software/tests/spec/runners/l2.py`).
//!
//! A port takes a vector's `input` and its machine and returns what the
//! runner returns; a refusal is `{"error": message}`. Under a mutant
//! ([`crate::mutant`], the `mutants` feature) the whole run sees it.

use regex::Regex;
use serde_json::{json, Map, Value};

use indexmap::IndexMap;
use ktpu_l1::text::{fmt, Stmt, Val};
use ktpu_machine::Machine;

use crate::ir::read::instances;
use crate::ir::{Body, Buffer, LayoutVal, Node, Program, StageX};
use crate::lower::links::Links;
use crate::lower::sync::{self, Access, Stage};
use crate::lower::vector::search::Strategy;
use crate::lower::vector::{encode, Search};
use crate::mutant::State;
use crate::passes;
use crate::target::Target;

const FILE: &str = "<spec>";

/// Every mutant ktpu-l2 carries.
pub fn mutants() -> Vec<&'static str> {
    let mut out = crate::mutant::NAMES.to_vec();
    out.extend(crate::mutant::l1_knobs());
    out.extend(ktpu_machine::mutant::NAMES);
    out
}

/// Mutants an L2 vector names whose mutated point is another crate's
/// without a knob there, or a runner not ported.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    let knobs = crate::mutant::l1_knobs();
    let mut out: Vec<(&str, &str)> = crate::mutant::L1_TIMING
        .iter()
        .filter(|n| !knobs.contains(n))
        .map(|n| {
            (
                *n,
                "the core's timing (ktpu-l1 sched::timing), no knob there yet",
            )
        })
        .collect();
    out.extend([
        (
            "l2.lanes/rot-negated",
            "the lane relayout planner (ktpu-kernels gen::lanes)",
        ),
        (
            "l2.lanes/keep-ignored",
            "the lane relayout planner (ktpu-kernels gen::lanes)",
        ),
        (
            "l2.lanes/masks-inverted",
            "the lane relayout planner (ktpu-kernels gen::lanes)",
        ),
    ]);
    out
}

/// A frozen vector an accepted ruling supersedes. Its input as written is
/// refused under the ruling (the ruling's own vector witnesses that); it runs
/// with the ruled input instead (`set`: JSON pointer -> JSON value), so its
/// mutants still reach the code they mutate, and must give `expect` (JSON
/// text), or the vector's own expect when none.
pub struct Superseded {
    pub vector: &'static str,
    pub ruling: &'static str,
    pub set: &'static [(&'static str, &'static str)],
    pub expect: Option<&'static str>,
}

/// The vector files change when the Python harness retires (2p.7).
pub const SUPERSEDED: &[Superseded] = &[
    // The band pack of a statistic is gone: the same rows store an
    // elementwise result through the same row walk.
    Superseded {
        vector: "L2.STAT.004.band_subtiles",
        ruling: "RUL.030",
        set: &[(
            "/text/7",
            r#""            m = exp2 v                               : f32[16, 32] @reg(gt4)""#,
        )],
        expect: None,
    },
];

/// `(input, expect)` vector `name` runs with: the ruled ones when a ruling
/// supersedes it, else its own.
pub fn ruled(name: &str, input: &Value, expect: &Value) -> (Value, Value) {
    let Some(s) = SUPERSEDED.iter().find(|s| s.vector == name) else {
        return (input.clone(), expect.clone());
    };
    let mut input = input.clone();
    for (pointer, value) in s.set {
        let v: Value = serde_json::from_str(value).expect("ruled value is JSON");
        *input.pointer_mut(pointer).expect("the ruled field exists") = v;
    }
    let expect = s.expect.map_or(expect.clone(), |e| {
        serde_json::from_str(e).expect("ruled output is JSON")
    });
    (input, expect)
}

type Port = fn(&Value, &Machine) -> Result<Value, String>;

const PORTS: &[(&str, Port)] = &[
    ("l2.compile", run_compile),
    ("l2.deterministic", run_deterministic),
    ("l2.passes", run_passes),
    ("l2.body", run_body),
    ("l2.sync", run_sync),
    ("l2.timing", run_timing),
    ("l2.pipeline", run_pipeline),
    ("l2.retile", run_retile),
    ("l2.search", run_search),
    ("l2.banks", run_banks),
    ("l2.banks.choose", run_banks_choose),
    ("l2.target", run_target),
];

/// `banks::choose` over `code` (`[template, [symbols]]` entries) on a
/// `copies`-copy register file: the symbols it rotates.
fn run_banks_choose(inp: &Value, m: &Machine) -> Result<Value, String> {
    use crate::lower::vector::{banks::typed as banks, modulo::Core};
    let code: Vec<_> = inp["code"]
        .as_array()
        .ok_or("l2.banks.choose takes code")?
        .iter()
        .map(|e| {
            let syms: Vec<String> = e[1]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|s| s.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            bank_event(e[0].as_str().unwrap_or(""), &syms)
        })
        .collect::<Result<_, _>>()?;
    let copies = inp["copies"]
        .as_u64()
        .ok_or("l2.banks.choose takes copies")? as u32;
    let mut got: Vec<String> = banks::choose(&code, copies, &Core::of(&Target::of(m)))
        .into_keys()
        .collect();
    got.sort();
    Ok(json!({ "rotated": got }))
}

/// Decode a spec fixture's symbolic instruction, not a generated candidate.
fn bank_event(
    template: &str,
    names: &[String],
) -> Result<crate::lower::vector::gen::typed::Event, String> {
    use crate::lower::vector::gen::typed::{Event, Symbol};
    use ktpu_l1::text::Term;
    let syms: Vec<Symbol> = names
        .iter()
        .map(|n| {
            n.strip_prefix('%')
                .ok_or("symbol prefix")?
                .parse()
                .map(Symbol)
                .map_err(|_| "symbol number")
        })
        .collect::<Result<_, _>>()?;
    let slots = Regex::new(r"\{([0-9]+)\}").unwrap();
    let source = slots.replace_all(template, |c: &regex::Captures| format!("v{}", &c[1]));
    let mut st = ktpu_l1::text::parse(&format!("{source}\n"), FILE)
        .map_err(|e| e.to_string())?
        .remove(0);
    fn resolve(t: &mut Term, syms: &[Symbol]) {
        match t {
            Term::Name(n) | Term::View(n, _) if n.starts_with('v') => {
                if let Ok(i) = n[1..].parse::<usize>() {
                    *n = syms[i].name();
                }
            }
            Term::Call(_, args) => {
                for a in args {
                    resolve(a, syms);
                }
            }
            _ => {}
        }
    }
    for a in &mut st.args {
        resolve(a, &syms);
    }
    Ok(Event::Op { stmt: st, syms })
}

/// Every ported runner's name.
pub fn runners() -> Vec<&'static str> {
    PORTS.iter().map(|(n, _)| *n).collect()
}

/// The stack's value of parameter `name` that ktpu-l2 owns (the
/// `isa.vector.params` runner's `pool`, ISA.VC.183), under the active
/// mutant; None for a parameter it does not own.
pub fn param(name: &str, machine: &Machine) -> Option<Value> {
    match name {
        "pool" => Some(json!(Target::of(machine).pool())),
        _ => None,
    }
}

/// [`param`] under `mutant` when given (one of [`mutants`]).
pub fn run_param(
    name: &str,
    machine: &Machine,
    mutant: Option<&str>,
) -> Result<Option<Value>, String> {
    let Some(mu) = mutant else {
        return Ok(crate::mutant::scoped(State::default(), || {
            param(name, machine)
        }));
    };
    let mu = crate::mutant::NAMES
        .iter()
        .copied()
        .find(|n| *n == mu)
        .ok_or_else(|| format!("unknown mutant {mu}"))?;
    let state = State {
        active: Some(mu),
        ..State::default()
    };
    Ok(crate::mutant::scoped(state, || param(name, machine)))
}

/// Where the lowering lands successive transfers of `sends` words from a
/// cluster into a core's L1 (`Links::send` on the landing pool; the
/// `isa.vector.links` runner, ISA.VC.183): each one's `at`, or
/// `{"refused": "ValueError"}` when a send is past the pool. Under `mutant`
/// when given (one of [`crate::mutant::NAMES`]).
pub fn run_links(sends: &[i64], machine: &Machine, mutant: Option<&str>) -> Result<Value, String> {
    let mu = match mutant {
        None => None,
        Some(m) => Some(
            crate::mutant::NAMES
                .iter()
                .copied()
                .find(|n| *n == m)
                .ok_or_else(|| format!("unknown mutant {m}"))?,
        ),
    };
    let state = State {
        active: mu,
        ..State::default()
    };
    let p = Program::parse("fn k.l2(y: f16[16] @dram)\n    copy y <- y\n", FILE, None)
        .map_err(|e| e.to_string())?;
    let body = p.body().map_err(|e| e.to_string())?;
    let st = Stmt::new("send", vec![]);
    crate::mutant::scoped(state, || {
        let mut links = Links::new(&IndexMap::new(), &Target::of(machine));
        let mut at = Vec::new();
        for &n in sends {
            match links.send(&body, &st, "mg0", "vc0", "l1", n, &[]) {
                Ok(x) => at.push(x.at),
                Err(_) => return Ok(json!({ "refused": "ValueError" })),
            }
        }
        Ok(json!(at))
    })
}

/// Run runner `runner` on `input` against `machine`, under `mutant` when
/// given (one of [`mutants`]).
pub fn run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    #[cfg(feature = "mutants")]
    if let Some(name) = mutant.and_then(|mu| {
        ktpu_machine::mutant::NAMES
            .iter()
            .copied()
            .find(|n| *n == mu)
    }) {
        return ktpu_machine::mutant::with(name, || run(runner, input, machine, None));
    }
    let port = PORTS
        .iter()
        .find(|(n, _)| *n == runner)
        .map(|(_, p)| *p)
        .ok_or_else(|| format!("runner {runner} has no Rust port"))?;
    let m = machine.ok_or_else(|| format!("{runner} runs against a machine"))?;
    let go = || {
        sync::reset_marks();
        port(input, m)
    };
    let Some(name) = mutant else {
        return crate::mutant::scoped(State::default(), go);
    };
    let Some(name) = mutants().into_iter().find(|n| *n == name) else {
        return Err(match skipped_mutants().iter().find(|(n, _)| *n == name) {
            Some((_, owner)) => format!("mutant {name} mutates {owner}"),
            None => format!("unknown mutant {name}"),
        });
    };
    if !cfg!(feature = "mutants") {
        return Err("ktpu-l2 is built without the `mutants` feature".into());
    }
    let state = if crate::mutant::L1_TIMING.contains(&name) {
        State {
            l1: Some(name),
            ..State::default()
        }
    } else {
        State {
            active: Some(name),
            ..State::default()
        }
    };
    crate::mutant::scoped(state, go)
}

// ------------------------------------------------------------------ helpers

fn text_of(inp: &Value) -> String {
    match &inp["text"] {
        Value::Array(lines) => {
            lines
                .iter()
                .map(|l| l.as_str().unwrap_or(""))
                .collect::<Vec<_>>()
                .join("\n")
                + "\n"
        }
        Value::String(s) => format!("{s}\n"),
        _ => "\n".into(),
    }
}

fn program(inp: &Value) -> Result<Program, ktpu_l1::Error> {
    Program::parse(&text_of(inp), FILE, inp["name"].as_str())
}

fn target(m: &Machine, inp: &Value) -> Target {
    let t = Target::of(m);
    match inp["nodes"].as_i64() {
        Some(n) => t.with_nodes(n),
        None => t,
    }
}

fn refusal(e: &ktpu_l1::Error) -> Value {
    json!({ "error": e.message })
}

fn passes_of(inp: &Value) -> Vec<String> {
    match inp["passes"].as_array() {
        Some(ps) => ps
            .iter()
            .map(|p| p.as_str().unwrap_or("").to_string())
            .collect(),
        None => passes::pipeline_passes()
            .iter()
            .map(|p| p.to_string())
            .collect(),
    }
}

fn strategy(inp: &Value) -> Strategy {
    inp["strategy"]
        .as_str()
        .and_then(Strategy::parse)
        .unwrap_or(Strategy::Auto)
}

/// The L1 text `compile_l1` makes of the input's L2 text.
fn compile_text(inp: &Value, m: &Machine) -> Result<String, ktpu_l1::Error> {
    let p = program(inp)?;
    let ps = passes_of(inp);
    let ps: Vec<&str> = ps.iter().map(String::as_str).collect();
    let schedule = inp["schedule"].as_bool().unwrap_or(true);
    let c = crate::compile_with(
        &p,
        &target(m, inp),
        &ps,
        schedule,
        &Search::new(strategy(inp)),
    )?;
    Ok(c.l1.source)
}

/// The lines of `text` in `where_` ("fn", "images", "all"), stripped, runs of
/// spaces one.
fn section(text: &str, where_: &str) -> Vec<String> {
    let spaces = Regex::new("  +").unwrap();
    let (mut out, mut part) = (Vec::new(), "");
    for ln in text.lines() {
        if ln.starts_with("image ") {
            part = "images";
        } else if ln.starts_with("fn ") {
            part = "fn";
        } else if !ln.is_empty() && !ln.starts_with(' ') {
            part = "";
        }
        if !ln.trim().is_empty() && (where_ == "all" || where_ == part) {
            out.push(spaces.replace_all(ln.trim(), " ").into_owned());
        }
    }
    out
}

/// `{key: fact}` of `text` (runners/l2.py `facts`).
fn facts(text: &str, want: &Value) -> Result<Value, String> {
    let mut out = Map::new();
    let Some(want) = want.as_object() else {
        return Ok(Value::Object(out));
    };
    for (key, sel) in want {
        let lines = section(text, sel["in"].as_str().unwrap_or("all"));
        let re = |k: &str| -> Result<Regex, String> {
            Regex::new(sel[k].as_str().unwrap_or("")).map_err(|e| format!("{key}: {e}"))
        };
        let v = if sel.get("lines").is_some() {
            let r = re("lines")?;
            json!(lines.iter().filter(|l| r.is_match(l)).collect::<Vec<_>>())
        } else if sel.get("count").is_some() {
            let r = re("count")?;
            json!(lines.iter().filter(|l| r.is_match(l)).count())
        } else if sel.get("set").is_some() {
            let r = re("set")?;
            let mut got: Vec<String> = lines
                .iter()
                .filter_map(|l| {
                    r.captures(l)
                        .and_then(|c| c.get(1))
                        .map(|m| m.as_str().to_string())
                })
                .collect();
            got.sort();
            got.dedup();
            json!(got)
        } else if sel.get("has").is_some() {
            let r = re("has")?;
            json!(lines.iter().any(|l| r.is_match(l)))
        } else {
            return Err(format!("fact {key}: {sel}"));
        };
        out.insert(key.clone(), v);
    }
    Ok(Value::Object(out))
}

// ------------------------------------------------------------------ runners

/// The compiler's machine for machine file `board` with the facts `drop`
/// names removed: its vector register count and read copies, or the refusal
/// (RUL.001: the core comes from the machine, a machine without one is
/// refused).
fn run_target(inp: &Value, _m: &Machine) -> Result<Value, String> {
    let board = inp["board"].as_str().ok_or("l2.target names a board")?;
    let path = ktpu_machine::machines_dir().join(format!("{board}.json"));
    let text = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut doc: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    if let Some(facts) = doc["machine"].as_object_mut() {
        for key in inp["drop"].as_array().into_iter().flatten() {
            facts.remove(key.as_str().unwrap_or(""));
        }
    }
    let only = inp["refused_only"].as_bool().unwrap_or(false);
    match Machine::from_json(&doc.to_string()) {
        Err(e) => Ok(if only {
            json!({"refused": true})
        } else {
            json!({"error": e.to_string()})
        }),
        Ok(_) if only => Ok(json!({"refused": false})),
        Ok(m) => {
            let t = Target::of(&m);
            Ok(json!({"vregs": t.vregs, "vcopies": t.vcopies}))
        }
    }
}

fn run_compile(inp: &Value, m: &Machine) -> Result<Value, String> {
    let only = inp["refused_only"].as_bool().unwrap_or(false);
    match compile_text(inp, m) {
        Err(e) => Ok(if only {
            json!({"refused": true})
        } else {
            refusal(&e)
        }),
        Ok(_) if only => Ok(json!({"refused": false})),
        Ok(text) => facts(&text, &inp["facts"]),
    }
}

fn run_deterministic(inp: &Value, m: &Machine) -> Result<Value, String> {
    let p = program(inp).map_err(|e| e.to_string())?;
    let ps = passes_of(inp);
    let ps: Vec<&str> = ps.iter().map(String::as_str).collect();
    let t = target(m, inp);
    let search = Search::new(strategy(inp));
    let first = crate::compile_with(&p, &t, &ps, true, &search).map_err(|e| e.to_string())?;
    // The second compile is at the default strategy, the search's choices kept.
    let fresh = Search::default();
    let warm = if search.strategy == Strategy::Auto {
        &search
    } else {
        &fresh
    };
    let again = crate::compile_with(&p, &t, &ps, true, warm).map_err(|e| e.to_string())?;
    let mut out = facts(&first.l1.source, &inp["facts"])?;
    out["same"] = json!(first.l1.source == again.l1.source);
    Ok(out)
}

fn run_passes(inp: &Value, m: &Machine) -> Result<Value, String> {
    let p = program(inp).map_err(|e| e.to_string())?;
    let ps = passes_of(inp);
    let ps: Vec<&str> = ps.iter().map(String::as_str).collect();
    match passes::run(&p, &ps, &target(m, inp), &Search::default()) {
        Ok(q) => facts(q.text(), &inp["facts"]),
        Err(e) => Ok(refusal(&e)),
    }
}

fn tree(stmts: &[std::sync::Arc<Node>]) -> Value {
    Value::Array(
        stmts
            .iter()
            .map(|s| match s.as_ref() {
                Node::Par(p) => json!({
                    "par": p.var,
                    "span": format!("{}..{}", fmt(&p.lo), fmt(&p.hi)),
                    "on": format!("{}[{}]", p.kind, p.index.as_ref().map(fmt).unwrap_or_else(|| "any".into())),
                    "body": tree(&p.body),
                }),
                Node::For(f) => json!({
                    "for": f.var,
                    "span": format!("{}..{}", fmt(&f.lo), fmt(&f.hi)),
                    "pipe": f.pipe,
                    "body": tree(&f.body),
                }),
                Node::When(w) => json!({"when": true, "body": tree(&w.body)}),
                Node::If(i) => json!({"if": "sticky", "body": tree(&i.body)}),
                Node::Op(o) => json!({
                    "op": o.op,
                    "target": o.target,
                    "type": o.ty.as_ref().map(|t| t.to_string()),
                }),
            })
            .collect(),
    )
}

fn buf_json(p: &str, b: &Buffer) -> Value {
    let layout: Map<String, Value> = b
        .layout
        .iter()
        .map(|(k, v)| {
            let v = match v {
                LayoutVal::Int(i) => json!(i),
                LayoutVal::Float(f) => json!(f),
                LayoutVal::Dims(d) => json!(d),
            };
            (k.clone(), v)
        })
        .collect();
    json!([p, b.dtype, b.shape, b.space, layout])
}

fn env_json(env: &ktpu_l1::text::Env) -> Value {
    let mut keys: Vec<&String> = env.keys().collect();
    keys.sort();
    let mut out = Map::new();
    for k in keys {
        let v = match &env[k] {
            Val::Int(i) => json!(*i as i64),
            Val::Float(f) => json!(f),
            Val::Term(t) => json!(fmt(t)),
        };
        out.insert(k.clone(), v);
    }
    Value::Object(out)
}

fn run_body(inp: &Value, _m: &Machine) -> Result<Value, String> {
    let p = program(inp).map_err(|e| e.to_string())?;
    let body: Body = match p.body() {
        Ok(b) => b,
        Err(e) => return Ok(refusal(&e)),
    };
    let stages = match instances(&body) {
        Ok(s) => s,
        Err(e) => return Ok(refusal(&e)),
    };
    let mut out = Vec::new();
    for s in &stages {
        let mut st = Map::new();
        st.insert("node".into(), json!(s.node));
        st.insert("phase".into(), json!(s.phase));
        match &s.x {
            StageX::Par(x) => {
                st.insert("kind".into(), json!("par"));
                let units: Vec<String> = x
                    .iter()
                    .map(|i| {
                        format!(
                            "{}[{}]",
                            i.kind,
                            i.index
                                .map(|v| v.to_string())
                                .unwrap_or_else(|| "None".into())
                        )
                    })
                    .collect();
                st.insert("units".into(), json!(units));
                let items: Vec<Vec<String>> = x
                    .iter()
                    .map(|i| {
                        i.items
                            .iter()
                            .map(|(n, _)| match n.as_ref() {
                                Node::Op(o) => o.op.clone(),
                                _ => "if".into(),
                            })
                            .collect()
                    })
                    .collect();
                st.insert("items".into(), json!(items));
                st.insert(
                    "env".into(),
                    Value::Array(x.iter().map(|i| env_json(&i.env)).collect()),
                );
            }
            StageX::Op(n, env) => {
                st.insert("kind".into(), json!("op"));
                st.insert("op".into(), json!(n.op_name()));
                st.insert("env".into(), env_json(env));
            }
        }
        st.insert(
            "group".into(),
            match s.tag {
                None => Value::Null,
                Some((_, it, k)) => json!([it, k]),
            },
        );
        out.push(Value::Object(st));
    }
    let got = json!({
        "params": body.params.iter().map(|(p, b)| buf_json(p, b)).collect::<Vec<_>>(),
        "scratch": body.scratch.iter().map(|(p, b)| buf_json(p, b)).collect::<Vec<_>>(),
        "tree": tree(&body.stmts),
        "stages": out,
    });
    let want: Vec<String> = match inp["want"].as_array() {
        Some(w) => w
            .iter()
            .filter_map(|x| x.as_str().map(str::to_string))
            .collect(),
        None => ["params", "scratch", "tree", "stages"]
            .iter()
            .map(|s| s.to_string())
            .collect(),
    };
    let mut res = Map::new();
    for k in want {
        res.insert(k.clone(), got[&k].clone());
    }
    Ok(Value::Object(res))
}

fn access(a: &Value) -> Access {
    let v = a.as_array().cloned().unwrap_or_default();
    let key: Vec<String> = v[2]
        .as_array()
        .map(|k| {
            k.iter()
                .map(|x| x.as_str().unwrap_or("").to_string())
                .collect()
        })
        .unwrap_or_default();
    Access::new(
        v[0].as_str().unwrap_or(""),
        v[1].as_bool()
            .unwrap_or_else(|| v[1].as_i64().unwrap_or(0) != 0),
        (&key[0], &key[1]),
        v[3].as_i64().unwrap_or(0),
        v[4].as_i64().unwrap_or(0),
    )
    .flowing(
        v.get(5)
            .map(|f| f.as_bool().unwrap_or_else(|| f.as_i64().unwrap_or(0) != 0))
            .unwrap_or(false),
    )
}

fn run_sync(inp: &Value, _m: &Machine) -> Result<Value, String> {
    if let Some(pairs) = inp["clash"].as_array() {
        return Ok(Value::Array(
            pairs
                .iter()
                .map(|p| json!(sync::clash(&access(&p[0]), &access(&p[1]))))
                .collect(),
        ));
    }
    let mut loops: Vec<Value> = Vec::new();
    let mut stages = Vec::new();
    for s in inp["stages"].as_array().cloned().unwrap_or_default() {
        let strs = |k: &str| -> Vec<String> {
            s[k].as_array()
                .map(|a| {
                    a.iter()
                        .map(|x| x.as_str().unwrap_or("").to_string())
                        .collect()
                })
                .unwrap_or_default()
        };
        let acc = s["accesses"]
            .as_array()
            .map(|a| a.iter().map(access).collect())
            .unwrap_or_default();
        let mut st = Stage::new(strs("lines"), strs("units"), acc);
        if let Some(g) = s["group"].as_array().filter(|g| g.len() == 3) {
            let id = match loops.iter().position(|l| *l == g[0]) {
                Some(i) => i,
                None => {
                    loops.push(g[0].clone());
                    loops.len() - 1
                }
            };
            st.group = Some((
                id,
                g[1].as_i64().unwrap_or(0) as usize,
                g[2].as_i64().unwrap_or(0),
            ));
        }
        st.post = s["post"].as_str().map(str::to_string);
        st.closes = s["closes"].as_bool().unwrap_or(false);
        stages.push(st);
    }
    Ok(json!(sync::schedule(&stages)))
}

fn run_timing(inp: &Value, m: &Machine) -> Result<Value, String> {
    let fill = if inp["shared"].as_bool().unwrap_or(false) {
        m.vc.fill_shared
    } else {
        m.vc.fill_alone
    };
    let fill = (fill.0 as i64, fill.1 as i64);
    let text = text_of(inp);
    let name = inp["name"].as_str().unwrap_or("");
    let got = ktpu_l1::sched::timing::cycles(&text, name, m.vc.regs, m.vc.rf_copies, fill, m)
        .map_err(|e| e.to_string())?;
    if !inp["compare"].as_bool().unwrap_or(false) {
        return Ok(json!(got));
    }
    let module = ktpu_l1::text::read(&text, FILE).map_err(|e| e.to_string())?;
    let img = &module.images[name];
    let run: Vec<ktpu_l1::text::Stmt> = img
        .stmts
        .iter()
        .filter(|s| s.op != "halt")
        .cloned()
        .collect();
    let r = module.reader();
    let ruled =
        ktpu_l1::sched::Ruled::new(&r, m, m.vc.regs, m.vc.rf_copies).map_err(|e| e.to_string())?;
    let order: Vec<usize> = (0..run.len()).collect();
    let cost = ruled.cycles(&run, &order).map_err(|e| e.to_string())?;
    Ok(json!({"same": got == cost}))
}

fn run_pipeline(_inp: &Value, _m: &Machine) -> Result<Value, String> {
    Ok(json!(passes::pipeline_passes()))
}

fn run_retile(inp: &Value, m: &Machine) -> Result<Value, String> {
    let p = program(inp).map_err(|e| e.to_string())?;
    let out = passes::run(&p, &["retile"], &target(m, inp), &Search::default())
        .map_err(|e| e.to_string())?;
    let text = out.text();
    let lp = Regex::new(r"for (\w+) in (\d+)\.\.(\d+)").unwrap();
    let c = lp.captures(text).ok_or("no loop after retile")?;
    let (var, lo, hi): (String, i64, i64) = (
        c[1].to_string(),
        c[2].parse().unwrap(),
        c[3].parse().unwrap(),
    );
    let views = Regex::new(&format!(r"\[{var} ?\* ?(\d+) : \+(\d+), :\]")).unwrap();
    let mut tiles: Vec<i64> = Vec::new();
    for c in views.captures_iter(text) {
        for g in [1, 2] {
            let v: i64 = c[g].parse().unwrap();
            if !tiles.contains(&v) {
                tiles.push(v);
            }
        }
    }
    let tile = if tiles.len() == 1 {
        Some(tiles[0])
    } else {
        None
    };
    let r = inp["written"].as_i64().unwrap_or(0);
    let band = inp["band"].as_i64().unwrap_or(1);
    let mut allowed: Vec<i64> = vec![r, r / 4, r / 2, 2 * r, 4 * r];
    allowed.extend((band..=r).step_by(band.max(1) as usize));
    let ok = tile
        .map(|t| allowed.iter().any(|a| *a != 0 && a % band == 0 && *a == t))
        .unwrap_or(false);
    Ok(json!({
        "rows": tile.map(|t| t * (hi - lo)),
        "one_tile": tile.is_some(),
        "allowed": ok,
    }))
}

fn run_search(inp: &Value, m: &Machine) -> Result<Value, String> {
    let fill = (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64);
    let img = Regex::new(r"(?m)^image (\w+)\(").unwrap();
    let mut got: Vec<(String, i64)> = Vec::new();
    for s in ["auto", "band", "modulo"] {
        let mut i = inp.clone();
        i["strategy"] = json!(s);
        i["schedule"] = json!(false);
        let Ok(text) = compile_text(&i, m) else {
            continue;
        };
        let name = img.captures(&text).ok_or("no image")?[1].to_string();
        let module = encode::read_image(&text, FILE, m.vc.regs).map_err(|e| e.to_string())?;
        let c =
            ktpu_l1::sched::timing::cycles_of(&module, &name, m.vc.regs, m.vc.rf_copies, fill, m)
                .map_err(|e| e.to_string())?;
        got.push((s.to_string(), c));
    }
    let auto = got
        .iter()
        .find(|(s, _)| s == "auto")
        .map(|x| x.1)
        .ok_or("auto did not compile")?;
    let min = got.iter().map(|x| x.1).min().unwrap();
    Ok(json!({"fastest": auto <= min}))
}

fn run_banks(inp: &Value, m: &Machine) -> Result<Value, String> {
    let chunk = Regex::new(r"\[\d+ ?:\]").unwrap();
    let walk_n = Regex::new(r"^(vunpk .*) n=\d+$").unwrap();
    let reg = Regex::new(r"\bv\d+\b").unwrap();
    let ops = |text: &str| -> Vec<String> {
        section(text, "images")
            .iter()
            .map(|ln| {
                let ln = chunk.replace_all(ln, "").into_owned();
                let ln = walk_n.replace(&ln, "$1").into_owned();
                reg.replace_all(&ln, "v").into_owned()
            })
            .collect()
    };
    let mut i = inp.clone();
    i["schedule"] = json!(false);
    let placed = compile_text(&i, m).map_err(|e| e.to_string())?;
    let off = State {
        banks_off: true,
        ..crate::mutant::current()
    };
    let plain = crate::mutant::scoped(off, || compile_text(&i, m)).map_err(|e| e.to_string())?;
    Ok(json!({"same": ops(&placed) == ops(&plain), "placed": placed != plain}))
}
