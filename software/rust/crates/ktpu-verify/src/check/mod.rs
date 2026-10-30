//! The dispatch scheduler and the static check over bound packages
//! (l1/program.md s15, L1.VER.001-L1.VER.004).

use std::collections::BTreeMap;

use rayon::prelude::*;

use crate::adapter::l0::{from_l0, to_l0};
use crate::adapter::package::Package;
use crate::model::finding::{Finding, Kind};
use crate::mutant::on;
use crate::sim::{Kinded, Sim, EVENT_LIMIT};
use crate::target::Target;

pub use crate::sim::Totals;

pub mod gate;
#[cfg(test)]
mod tests;

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
    pub held: Held,
    pub totals: Totals,
}

/// Each node's package (bound) run on the abstract machine with each node
/// free, its vector cores holding `held` first: a segment runs in package
/// order, and only when no process can act does a node send the earliest
/// item heading its unit's queue that can move (L1.VER.001). A segment the
/// program already orders well is left as written (L1.VER.002).
pub fn schedule(pkgs: &Nodes, held: &Held, t: &Target) -> Result<Scheduled, ScheduleError> {
    let own: BTreeMap<u32, Package> = pkgs.iter().map(|(&m, p)| (m, from_l0(p))).collect();
    let (ordered, held, totals) = order(&own, held, t)?;
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
        held,
        totals,
    })
}

fn order(
    pkgs: &BTreeMap<u32, Package>,
    held: &Held,
    t: &Target,
) -> Result<(BTreeMap<u32, Package>, Held, Totals), ScheduleError> {
    let mut sim = Sim::new(t, None);
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
    sim.run(EVENT_LIMIT);
    let invalid_cluster = sim.findings.iter().any(|f| f.kind == Kind::Cluster);
    let stuck: Vec<Finding> = sim
        .findings
        .iter()
        .filter(|f| {
            matches!(f.kind, Kind::Deadlock | Kind::Cluster | Kind::Vector)
                || (invalid_cluster && f.kind == Kind::Bounds)
        })
        .cloned()
        .collect();
    if !stuck.is_empty() {
        return Err(ScheduleError { findings: stuck });
    }
    let mut packages = BTreeMap::new();
    for &(mesh, i) in &nodes {
        let got = sim.node_packages(i).map_err(|text| ScheduleError {
            findings: vec![Finding::new(Kind::Model, format!("node {mesh}"), text)],
        })?;
        packages.insert(mesh, got.into_iter().next().expect("one package"));
    }
    Ok((packages, held_after(&sim), sim.tally()))
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

fn run<'t>(pkgs: &Queued, t: &'t Target, totals: Option<Totals>) -> Sim<'t> {
    let mut sim = Sim::new(t, totals);
    for (&mesh, ps) in pkgs {
        sim.add_node(mesh, ps.clone(), false);
    }
    sim.run(EVENT_LIMIT);
    sim
}

/// Every finding for `runs` (each run's bound package per mesh, as built,
/// unordered): each run scheduled against the vector cores the runs before
/// left, then all run back to back on a fresh machine with the totals of a
/// first run (L1.VER.004). Empty means none of the finding kinds can happen.
pub fn check(runs: &[Nodes], t: &Target) -> Vec<Finding> {
    let mut out = checked(Held::new(), BTreeMap::new(), Totals::new(), runs, t);
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
    let pkgs = first
        .packages
        .iter()
        .map(|(&m, p)| (m, vec![from_l0(p)]))
        .collect();
    let mut out = checked(first.held.clone(), pkgs, first.totals.clone(), rest, t);
    line_findings(std::slice::from_ref(&first.packages), t, &mut out);
    line_findings(rest, t, &mut out);
    out
}

/// `check` from `held`, the ordered runs in `pkgs` and their `totals` on:
/// `runs` each ordered in turn, then all run back to back.
fn checked(held: Held, pkgs: Queued, totals: Totals, runs: &[Nodes], t: &Target) -> Vec<Finding> {
    let (pkgs, mut totals) = match ordered(held, pkgs, totals, runs, t) {
        Err(findings) => return findings,
        Ok(got) => got,
    };
    if on("verify.check/totals-prepass") {
        totals = run(&pkgs, t, None).tally();
    }
    let sim = run(&pkgs, t, Some(totals));
    let mut out = sim.findings.clone();
    if out.iter().any(|f| f.kind == Kind::Deadlock) {
        return out;
    }
    out.extend(sim.rest());
    out.extend(sim.port_findings());
    // Primary counter/routing failures already refuse the program; do not
    // replace their diagnostics with consequent uninitialized reads.
    if !out.iter().any(|f| f.kind.refuses()) {
        out.extend(sim.sequence_findings.clone());
    }
    out
}

/// `runs` each ordered in turn after `pkgs` (from `held`), and the
/// back-to-back run's totals: `totals` joined with each ordering run's.
fn ordered(
    mut held: Held,
    mut pkgs: Queued,
    mut totals: Totals,
    runs: &[Nodes],
    t: &Target,
) -> Result<(Queued, Totals), Vec<Finding>> {
    for nodes in runs {
        let own: BTreeMap<u32, Package> = nodes.iter().map(|(&m, p)| (m, from_l0(p))).collect();
        let (ordered, after, tally) = order(&own, &held, t).map_err(|e| e.findings)?;
        held = after;
        // Totals only feed clocks, and clocks never steer a process.
        join(&mut totals, tally);
        for (mesh, p) in ordered {
            pkgs.entry(mesh).or_default().push(p);
        }
    }
    Ok((pkgs, totals))
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
    let sim = run(&own, t, None);
    sim.units
        .iter()
        .filter(|u| matches!(u.kind, Kinded::Vc(_)))
        .map(|u| {
            let (r, w) = u.mem.touched(crate::sim::memory::Space::L1);
            (u.name.clone(), r, w)
        })
        .collect()
}
