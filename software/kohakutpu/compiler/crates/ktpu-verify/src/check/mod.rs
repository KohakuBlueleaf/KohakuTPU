//! The dispatch scheduler and the static check over bound packages
//! (l1/program.md s15, L1.VER.001-L1.VER.004).

use std::collections::BTreeMap;

use rayon::prelude::*;

use crate::adapter::l0::{from_l0, to_l0};
use crate::adapter::package::Package;
use crate::model::finding::{Finding, Kind};
use crate::mutant::on;
use crate::sim::symbolic::Symbolic;
use crate::sim::{Decision, Kinded, Sim, EVENT_LIMIT};
use crate::target::Target;

pub use crate::sim::Totals;

pub mod gate;

/// One node's bound package per mesh.
pub type Nodes = BTreeMap<u32, ktpu_l0::Package>;

/// What one vector core holds between runs: IMEM words by address and
/// descriptor fields by (descriptor, field).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HeldCore {
    pub coord: (u8, u8),
    pub imem: BTreeMap<i64, u64>,
    pub desc: BTreeMap<(i64, i64), i64>,
    /// Scalar control values retained across RUNs; None means unknown.
    pub scalars: BTreeMap<i64, Option<i64>>,
}

/// Per mesh, its vector cores' state, in the order the cores were named.
pub type Held = BTreeMap<u32, Vec<HeldCore>>;

/// No dispatch order completes (L1.VER.016): the deadlock findings.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("L1.VER.016: no dispatch order completes ({} findings)", .findings.len())]
pub struct ScheduleError {
    pub findings: Vec<Finding>,
}

/// The packages in the order the scheduler sent them (each the input with
/// its steps reordered), what the vector cores hold after, and the run's
/// counter increments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Scheduled {
    pub packages: Nodes,
    continuations: Vec<Continuation>,
}

impl Scheduled {
    /// Correlated core states after every feasible forward path.
    pub fn held_states(&self) -> impl Iterator<Item = &Held> {
        self.continuations.iter().map(|c| &c.held)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Continuation {
    held: Held,
    totals: Totals,
    path: Vec<Decision>,
    symbolic: Symbolic,
}

/// Each node's package (bound) run on the abstract machine with each node
/// free, its vector cores holding `held` first: a segment runs in package
/// order, and only when no process can act does a node send the earliest
/// item heading its unit's queue that can move (L1.VER.001). A segment the
/// program already orders well is left as written (L1.VER.002).
pub fn schedule(pkgs: &Nodes, held: &Held, t: &Target) -> Result<Scheduled, ScheduleError> {
    let own: BTreeMap<u32, Package> = pkgs.iter().map(|(&m, p)| (m, from_l0(p))).collect();
    let (ordered, continuations) = order(&own, held, &Symbolic::default(), t)?;
    let packages = pkgs
        .iter()
        .map(|(&m, p)| {
            let mut p = p.clone();
            p.steps = ordered[&m].steps.iter().map(to_l0).collect();
            (m, p)
        })
        .collect();
    Ok(Scheduled {
        packages,
        continuations,
    })
}

fn order(
    pkgs: &BTreeMap<u32, Package>,
    held: &Held,
    symbolic: &Symbolic,
    t: &Target,
) -> Result<(BTreeMap<u32, Package>, Vec<Continuation>), ScheduleError> {
    let mut sim = Sim::new(t, None);
    sim.symbolic = symbolic.clone();
    for (&mesh, cores) in held {
        for core in cores {
            let Some(u) = sim.unit((mesh, core.coord.0, core.coord.1)) else {
                continue;
            };
            if let Kinded::Vc(v) = &mut sim.units[u].kind {
                v.imem.extend(core.imem.iter().map(|(&a, &w)| (a, w)));
                v.desc.extend(core.desc.iter().map(|(&k, &w)| (k, w)));
                v.scalars = core.scalars.clone();
            }
        }
    }
    let mut nodes = Vec::new();
    for (&mesh, pkg) in pkgs {
        nodes.push((mesh, sim.add_node(mesh, vec![pkg.clone()], true)));
    }
    let mut leaves = sim.run(EVENT_LIMIT);
    for sim in &leaves {
        let invalid_cluster = sim.findings.iter().any(|f| f.kind == Kind::Cluster);
        let mut stuck: Vec<Finding> = sim
            .findings
            .iter()
            .filter(|f| {
                matches!(
                    f.kind,
                    Kind::Deadlock | Kind::Cluster | Kind::Vector | Kind::Loop | Kind::Model
                ) || (invalid_cluster && f.kind == Kind::Bounds)
            })
            .cloned()
            .collect();
        if !stuck.is_empty() {
            sim.witness(&mut stuck);
            return Err(ScheduleError { findings: stuck });
        }
    }
    let continuations = leaves
        .iter()
        .map(|sim| Continuation {
            held: held_after(sim),
            totals: sim.tally(),
            path: sim.path.clone(),
            symbolic: sim.symbolic.clone(),
        })
        .collect();
    let mut sim = leaves.remove(0);
    for leaf in leaves {
        for (node, other) in sim.nodes.iter_mut().zip(leaf.nodes) {
            for (segment, other) in node.segments.iter_mut().zip(other.segments) {
                for log in other.logs {
                    if !segment.logs.contains(&log) {
                        segment.logs.push(log);
                    }
                }
            }
        }
    }
    let mut packages = BTreeMap::new();
    for &(mesh, i) in &nodes {
        let got = sim.node_packages(i).map_err(|text| ScheduleError {
            findings: vec![Finding::new(Kind::Model, format!("node {mesh}"), text)],
        })?;
        packages.insert(mesh, got.into_iter().next().expect("one package"));
    }
    Ok((packages, continuations))
}

/// The vector cores' state at the run's end, per mesh.
fn held_after(sim: &Sim) -> Held {
    let mut out = Held::new();
    for unit in &sim.units {
        let Kinded::Vc(v) = &unit.kind else {
            continue;
        };
        out.entry(unit.key.0).or_default().push(HeldCore {
            coord: (unit.key.1, unit.key.2),
            imem: v.imem.iter().map(|(&a, &w)| (a, w)).collect(),
            desc: v.desc.iter().map(|(&k, &w)| (k, w)).collect(),
            scalars: v.scalars.clone(),
        });
    }
    out
}

/// One run of every node's packages, back to back, on a fresh machine.
/// Each mesh's ordered packages, run after run.
type Queued = BTreeMap<u32, Vec<Package>>;

fn run<'t>(
    pkgs: &Queued,
    t: &'t Target,
    totals: Option<Totals>,
    choices: Vec<Decision>,
) -> Vec<Sim<'t>> {
    let mut sim = Sim::new(t, totals);
    sim.choices = choices;
    for (&mesh, ps) in pkgs {
        sim.add_node(mesh, ps.clone(), false);
    }
    sim.run(EVENT_LIMIT)
}

/// Every finding for `runs` (each run's bound package per mesh, as built,
/// unordered): each run scheduled against the vector cores the runs before
/// left, then all run back to back on a fresh machine with the totals of a
/// first run (L1.VER.004). Empty means none of the finding kinds can happen.
pub fn check(runs: &[Nodes], t: &Target) -> Vec<Finding> {
    let initial = Continuation {
        held: Held::new(),
        totals: Totals::new(),
        path: Vec::new(),
        symbolic: Symbolic::default(),
    };
    let mut out = checked(vec![(BTreeMap::new(), initial)], runs, t);
    line_findings(runs, t, &mut out);
    out
}

fn line_findings(runs: &[Nodes], t: &Target, out: &mut Vec<Finding>) {
    for nodes in runs {
        for (&mesh, pkg) in nodes {
            for f in crate::lines::check(pkg, mesh, t) {
                if !out.contains(&f) {
                    out.push(f);
                }
            }
        }
    }
}

/// `check` of `[first's input run] ++ rest`, run 1's order and what the
/// vector cores hold after it taken from `first` (`schedule` of that run on
/// empty cores) instead of ordered again.
pub fn check_scheduled(first: &Scheduled, rest: &[Nodes], t: &Target) -> Vec<Finding> {
    let pkgs: Queued = first
        .packages
        .iter()
        .map(|(&m, p)| (m, vec![from_l0(p)]))
        .collect();
    let states = if first.continuations.len() == 1 {
        vec![(pkgs, first.continuations[0].clone())]
    } else {
        first
            .continuations
            .iter()
            .cloned()
            .map(|c| (pkgs.clone(), c))
            .collect()
    };
    let mut out = checked(states, rest, t);
    line_findings(std::slice::from_ref(&first.packages), t, &mut out);
    line_findings(rest, t, &mut out);
    out
}

/// Every retained state travels with its dispatch history and counter totals.
fn checked(states: Vec<(Queued, Continuation)>, runs: &[Nodes], t: &Target) -> Vec<Finding> {
    let states = match ordered(states, runs, t) {
        Err(findings) => return findings,
        Ok(got) => got,
    };
    let mut findings = Vec::new();
    for (pkgs, mut continuation) in states {
        if on("verify.check/totals-prepass") {
            let first = run(&pkgs, t, None, continuation.path.clone());
            continuation.totals = first[0].tally();
        }
        for sim in run(&pkgs, t, Some(continuation.totals), continuation.path) {
            let mut out = sim.findings.clone();
            if !out.iter().any(|f| f.kind == Kind::Deadlock) {
                out.extend(sim.rest());
                out.extend(sim.port_findings());
                // Primary counter/routing findings take precedence over initialization.
                if !out.iter().any(|f| f.kind.refuses()) {
                    out.extend(sim.sequence_findings.clone());
                }
            }
            sim.witness(&mut out);
            for finding in out {
                if !findings.contains(&finding) {
                    findings.push(finding);
                }
            }
        }
    }
    findings
}

/// Schedule each run against every correlated retained state.
fn ordered(
    mut states: Vec<(Queued, Continuation)>,
    runs: &[Nodes],
    t: &Target,
) -> Result<Vec<(Queued, Continuation)>, Vec<Finding>> {
    for nodes in runs {
        let own: BTreeMap<u32, Package> = nodes.iter().map(|(&m, p)| (m, from_l0(p))).collect();
        let mut next = Vec::new();
        for (pkgs, mut before) in states {
            let offsets: BTreeMap<u32, usize> =
                pkgs.iter().map(|(&mesh, ps)| (mesh, ps.len())).collect();
            let (ordered, after) =
                order(&own, &before.held, &before.symbolic, t).map_err(|mut e| {
                    if !before.path.is_empty() {
                        let path = format!("prior path: {:?}", before.path);
                        for finding in &mut e.findings {
                            finding.text = format!("{} [{path}]", finding.text);
                        }
                    }
                    e.findings
                })?;
            let mut pkgs = pkgs;
            for (mesh, p) in ordered {
                pkgs.entry(mesh).or_default().push(p);
            }
            let mut after = after.into_iter().peekable();
            while let Some(mut continuation) = after.next() {
                let (packages, mut totals, mut path) = if after.peek().is_none() {
                    (
                        std::mem::take(&mut pkgs),
                        std::mem::take(&mut before.totals),
                        std::mem::take(&mut before.path),
                    )
                } else {
                    (pkgs.clone(), before.totals.clone(), before.path.clone())
                };
                join(&mut totals, continuation.totals);
                continuation.totals = totals;
                for decision in &mut continuation.path {
                    decision.run += offsets.get(&decision.mesh).copied().unwrap_or(0);
                }
                path.extend(continuation.path);
                continuation.path = path;
                let state = (packages, continuation);
                if !next.contains(&state) {
                    next.push(state);
                }
            }
        }
        states = next;
    }
    Ok(states)
}

/// `run`'s increments added to `totals`, the runs' before it: its `host:k`
/// (the host after every node's k-th package end) follows theirs.
fn join(totals: &mut Totals, run: Totals) {
    let host = |c: &str| c.strip_prefix("host:")?.parse::<usize>().ok();
    let ends = totals.keys().filter_map(|c| host(c)).max().unwrap_or(0);
    for (counter, owners) in run {
        let counter = match host(&counter) {
            Some(k) => format!("host:{}", k + ends),
            None => counter,
        };
        let into = totals.entry(counter).or_default();
        for (owner, n) in owners {
            *into.entry(owner).or_default() += n;
        }
    }
}

/// `check` of each kernel, in parallel; the results in input order.
pub fn check_many(kernels: &[Vec<Nodes>], t: &Target) -> Vec<Vec<Finding>> {
    kernels.par_iter().map(|runs| check(runs, t)).collect()
}

/// The L1 words each vector core read since its last write and wrote, per
/// unit name, after one plain run of `pkgs` (scheduled packages).
pub fn l1_touched(pkgs: &Nodes, t: &Target) -> Vec<(String, Vec<i64>, Vec<i64>)> {
    let own = pkgs.iter().map(|(&m, p)| (m, vec![from_l0(p)])).collect();
    let sims = run(&own, t, None, Vec::new());
    if sims.len() == 1 {
        return sims[0]
            .units
            .iter()
            .filter(|u| matches!(u.kind, Kinded::Vc(_)))
            .map(|u| {
                let (r, w) = u.mem.touched(crate::sim::memory::Space::L1);
                (u.name.clone(), r, w)
            })
            .collect();
    }
    let mut touched: BTreeMap<String, (Vec<i64>, Vec<i64>)> = BTreeMap::new();
    for sim in sims {
        for unit in sim.units.iter().filter(|u| matches!(u.kind, Kinded::Vc(_))) {
            let (reads, writes) = unit.mem.touched(crate::sim::memory::Space::L1);
            let (r, w) = touched.entry(unit.name.clone()).or_default();
            r.extend(reads);
            w.extend(writes);
        }
    }
    touched
        .into_iter()
        .map(|(name, (mut r, mut w))| {
            r.sort_unstable();
            r.dedup();
            w.sort_unstable();
            w.dedup();
            (name, r, w)
        })
        .collect()
}
