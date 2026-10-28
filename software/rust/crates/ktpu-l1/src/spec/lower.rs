//! `l1.image`, `l1.steps`, `l1.build`, `l1.package`.

use std::collections::BTreeMap;

use ktpu_l0::{engine, Coord, Facts, Package, Step};
use ktpu_machine::{Machine, UnitKind};
use serde_json::{json, Map, Value};

use super::json::{core_words, package_json, prog_steps, signed};
use super::text::{binds, key};
use super::Port;
use crate::build::{self, Opts};
use crate::lower::image::{self, reset, UNKNOWN};
use crate::lower::imem::Cores;
use crate::lower::node::NodeProgram;
use crate::mutant::on;
use crate::text::read;

fn err(e: crate::Error) -> String {
    e.to_string()
}

fn flag(inp: &Value, k: &str) -> bool {
    inp[k].as_bool() == Some(true)
}

/// Each node's program of the input's `fn` on `m` (a card of `nodes`).
fn progs(inp: &Value, m: &Machine) -> Result<Vec<NodeProgram>, String> {
    let module = read(inp["text"].as_str().ok_or("input has no text")?, "<ktpu>").map_err(err)?;
    let name = inp["fn"].as_str().ok_or("input has no fn")?;
    let nodes = inp["nodes"].as_u64().map(|n| n as u32);
    build::programs(&module, name, &binds(inp)?, m, nodes, None).map_err(err)
}

/// A coordinate's unit type tag on `m`.
fn kind_of(m: &Machine) -> impl Fn((u32, u32)) -> String + '_ {
    move |c| {
        [UnitKind::Vc, UnitKind::Mg, UnitKind::Sr]
            .into_iter()
            .find(|k| m.units(*k).iter().any(|u| (u.0, u.1) == c))
            .map_or_else(|| "?".into(), |k| k.tag().to_string())
    }
}

/// An image's IMEM words and the descriptors its leading `desc`s set.
pub fn image(inp: &Value, m: &Machine) -> Port {
    let module = read(inp["text"].as_str().ok_or("input has no text")?, "<ktpu>").map_err(err)?;
    let entry = if inp["entry"] == "reset" {
        reset(m.vc.vlmax)
    } else {
        UNKNOWN
    };
    let args: Vec<i128> = match inp["args"].as_array() {
        Some(a) => a
            .iter()
            .map(|v| {
                v.as_i64()
                    .map(|x| x as i128)
                    .ok_or("an argument is no integer")
            })
            .collect::<Result<_, _>>()?,
        None => Vec::new(),
    };
    let name = inp["image"].as_str().ok_or("input has no image")?;
    let (words, descs) = image::image(&module, m, name, &args, entry).map_err(err)?;
    let mut out = Map::new();
    out.insert("words".into(), json!(core_words(&words)));
    if !descs.is_empty() {
        let d = descs.iter().map(|((ad, fld), v)| {
            if *fld == 0 {
                json!([ad, fld, format!("{v:#x}")])
            } else {
                let v = *v as i128;
                json!([ad, fld, [signed(v >> 16, 18) as i64, (v & 0xFFFF) as i64]])
            }
        });
        out.insert("descs".into(), d.collect());
    }
    Ok(Value::Object(out))
}

/// Each node's `Program`: its steps, and with `spans` / `mesh` those.
pub fn steps(inp: &Value, m: &Machine) -> Port {
    let kind = kind_of(m);
    let mut out = Map::new();
    for np in progs(inp, m)? {
        let mut g = Map::new();
        g.insert("steps".into(), prog_steps(&kind, &np.program));
        if flag(inp, "spans") {
            let spans = np.program.spans.iter().map(|(a, z)| json!([a, z]));
            g.insert("spans".into(), spans.collect());
        }
        if flag(inp, "mesh") {
            g.insert("mesh".into(), json!(np.mesh));
        }
        out.insert(key(np.node), Value::Object(g));
    }
    Ok(Value::Object(out))
}

/// `Program.build` of each node: the L0 steps, payloads decoded. With
/// `resident`, the cores' state carried across `runs` builds (the last is
/// shown); with `relocate`, address fields in the bound buffers become
/// relocations.
pub fn build(inp: &Value, m: &Machine) -> Port {
    let builder = build::Builder::new(m).map_err(err)?;
    let relocate = flag(inp, "relocate");
    let mut out = Map::new();
    for np in progs(inp, m)? {
        let mut cores = match flag(inp, "resident") {
            true => Some(Cores::new(m).map_err(err)?),
            false => None,
        };
        let mut b = None;
        for _ in 0..inp["runs"].as_u64().unwrap_or(1) {
            let opts = Opts {
                fetch: None,
                cores: cores.as_mut(),
                relocate,
            };
            b = Some(builder.build(&np, opts).map_err(err)?);
        }
        let b = b.ok_or("runs is 0")?;
        let pkg = b.build(true, false);
        let mut steps: Vec<Value> = package_json(&pkg).as_array().cloned().unwrap_or_default();
        if flag(inp, "brief") {
            for s in steps.iter_mut().filter(|s| s[0] == "DISPATCH") {
                let n = s[2].as_array().map_or(0, Vec::len);
                *s = json!([s[0], s[1], n]);
            }
        }
        if let Some(only) = inp["only"].as_array() {
            steps = steps
                .iter()
                .filter(|s| s[0] == "DISPATCH")
                .flat_map(|s| s[2].as_array().cloned().unwrap_or_default())
                .filter(|w| only.contains(&w[0]))
                .collect();
        }
        let mut g = Map::new();
        g.insert("steps".into(), Value::Array(steps));
        if relocate {
            let relocs = pkg.relocs.iter().map(|r| {
                json!([
                    r.payload,
                    r.bit,
                    r.width,
                    r.buffer,
                    r.addend as i64,
                    r.shift
                ])
            });
            g.insert("relocs".into(), relocs.collect());
            let bindings: Vec<String> = b.bindings().iter().map(|a| format!("{a:#x}")).collect();
            g.insert("bindings".into(), json!(bindings));
        }
        out.insert(key(np.node), Value::Object(g));
    }
    Ok(Value::Object(out))
}

/// The dispatch scheduler `build.packages` orders a card's packages with:
/// each mesh's bound package in, each mesh's steps in the order it found out.
pub type Schedule<'a> =
    &'a dyn Fn(&BTreeMap<u32, Package>, &Machine) -> Result<BTreeMap<u32, Vec<Step>>, String>;

/// `l1.package` with `all` (`build.packages`): each node built against its
/// own cores with every buffer address a relocation, bound, ordered by
/// `schedule`, lowered for the dispatch engine; its bindings are empty.
pub fn package_all(inp: &Value, m: &Machine, schedule: Schedule<'_>) -> Port {
    let builder = build::Builder::new(m).map_err(err)?;
    let progs = progs(inp, m)?;
    let mut unbound = BTreeMap::new();
    let mut bound = BTreeMap::new();
    for np in &progs {
        let mut cores = Cores::new(m).map_err(err)?;
        let opts = Opts {
            fetch: None,
            cores: Some(&mut cores),
            relocate: true,
        };
        let b = builder.build(np, opts).map_err(err)?;
        let pkg = b.build(false, false);
        let mesh = np.mesh;
        bound.insert(mesh, pkg.bind(&b.bindings()).map_err(|e| e.to_string())?);
        unbound.insert(mesh, pkg);
    }
    let order = schedule(&bound, m)?;
    let mut out = Map::new();
    for np in &progs {
        let mesh = np.mesh;
        let mut pkg = match on("l1.lower/no-bind") {
            true => unbound.remove(&mesh),
            false => bound.remove(&mesh),
        }
        .ok_or("a mesh built twice")?;
        pkg.steps = order
            .get(&mesh)
            .cloned()
            .ok_or_else(|| format!("the schedule has no mesh {mesh}"))?;
        let f = Facts::of_mesh(m, mesh).map_err(|e| e.to_string())?;
        let pkg = engine::lower(&pkg, &f).map_err(|e| e.to_string())?;
        out.insert(
            key(np.node),
            json!({
                "ops": pkg.steps.iter().map(|s| s.op.name()).collect::<Vec<_>>(),
                "defaults": pkg.buffers.iter().map(|b| b.default).collect::<Vec<_>>(),
                "relocs": pkg.relocs.len(),
                "bindings": [],
            }),
        );
    }
    Ok(Value::Object(out))
}

/// What a mesh's vector cores hold before a run: per core, its coordinate,
/// IMEM words by address and descriptor fields (value and value_hi joined).
pub type Held = BTreeMap<u32, Vec<(Coord, BTreeMap<i64, u64>, BTreeMap<(i64, i64), i64>)>>;

/// The dispatch scheduler `verify.schedule` orders a run with: each mesh's
/// bound package and what its cores hold in, each mesh's steps out.
pub type OrderSchedule<'a> = &'a dyn Fn(
    &BTreeMap<u32, Package>,
    &Held,
    &Machine,
) -> Result<BTreeMap<u32, Vec<Step>>, String>;

fn held(cores: &BTreeMap<u32, Cores>) -> Held {
    let desc = |(v, hi): (i64, i64)| v | hi << ktpu_isa::unit::vector::DESC_VALUE_BITS;
    cores
        .iter()
        .map(|(mesh, c)| {
            let units = c.cores.iter().map(|(coord, r)| {
                let mem = r.mem.iter().map(|(a, w)| (*a as i64, *w)).collect();
                let d = r.desc.iter().map(|(k, v)| (*k, desc(*v))).collect();
                (*coord, mem, d)
            });
            (*mesh, units.collect())
        })
        .collect()
}

/// `l1.order` (`verify.schedule`): each node built against its mesh's
/// cores (carried across `runs` schedules) with every buffer address a
/// relocation, bound, ordered by `schedule` given what the cores held
/// before the build; the last run's packages, bound.
pub fn order_with(inp: &Value, m: &Machine, schedule: OrderSchedule<'_>) -> Port {
    let builder = build::Builder::new(m).map_err(err)?;
    let progs = progs(inp, m)?;
    let mut cores: BTreeMap<u32, Cores> = BTreeMap::new();
    for np in &progs {
        cores.insert(np.mesh, Cores::new(m).map_err(err)?);
    }
    let mut out = Map::new();
    for _ in 0..inp["runs"].as_u64().unwrap_or(1) {
        let before = held(&cores);
        let mut bound = BTreeMap::new();
        for np in &progs {
            let mine = cores.get_mut(&np.mesh).ok_or("a mesh with no cores")?;
            let opts = Opts {
                fetch: None,
                cores: (!on("l1.verify/no-residents")).then_some(mine),
                relocate: true,
            };
            let b = builder.build(np, opts).map_err(err)?;
            let pkg = b.build(false, false);
            bound.insert(np.mesh, pkg.bind(&b.bindings()).map_err(|e| e.to_string())?);
        }
        let order = schedule(&bound, &before, m)?;
        out = Map::new();
        for (mesh, mut pkg) in bound {
            pkg.steps = order
                .get(&mesh)
                .cloned()
                .ok_or_else(|| format!("the schedule has no mesh {mesh}"))?;
            out.insert(mesh.to_string(), package_json(&pkg));
        }
    }
    Ok(Value::Object(out))
}

/// `l1.order` schedules through ktpu-verify (`order_with`).
pub fn order(_: &Value, _: &Machine) -> Port {
    Err("l1.order: verify.schedule is ktpu-verify's".into())
}

/// `build.package` of each node: its steps' op names, its buffers'
/// defaults, its relocation count and bindings. `all` (`build.packages`,
/// bound through the verifier's schedule) is ktpu-verify's.
pub fn package(inp: &Value, m: &Machine) -> Port {
    if flag(inp, "all") {
        return Err("l1.package all: build.packages schedules through ktpu-verify".into());
    }
    let builder = build::Builder::new(m).map_err(err)?;
    let mut out = Map::new();
    for np in progs(inp, m)? {
        let (pkg, bindings) = builder.package(&np, Opts::default()).map_err(err)?;
        out.insert(
            key(np.node),
            json!({
                "ops": pkg.steps.iter().map(|s| s.op.name()).collect::<Vec<_>>(),
                "defaults": pkg.buffers.iter().map(|b| b.default).collect::<Vec<_>>(),
                "relocs": pkg.relocs.len(),
                "bindings": bindings,
            }),
        );
    }
    Ok(Value::Object(out))
}
