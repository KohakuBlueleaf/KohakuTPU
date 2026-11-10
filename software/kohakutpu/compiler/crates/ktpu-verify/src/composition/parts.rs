//! Retained package transitions, not a concatenated execution or a fresh-Held
//! approximation. Each transition is checked on the complete incoming machine
//! state. Only its executable package is installed. Causal DRAM components,
//! publication/value provenance, persistent unit state and symbolic alternatives
//! travel with the transition; no host join is invented at a capacity boundary.
//!
//! A checked terminal gather/release permits delaying an early participant's
//! next package until the other participants finish the protocol-only suffix.
//! This changes visitation order, not process clocks. No data operation remains
//! in that suffix. Counter balance and a drain at entry are checked dynamically.

use std::collections::BTreeMap;
use std::sync::Arc;

use ktpu_l0::Op;
use crate::adapter::{l0::from_l0, package::Package as Input};
use crate::check::{MemoryContext, Nodes};
use crate::model::finding::{Finding, Kind};
use crate::sim::{Sim, EVENT_LIMIT};
use crate::Target;

mod contract;
use contract::Contract;

#[derive(Default)]
pub struct Report {
    pub findings: Vec<Finding>,
    pub checked_parts: usize,
    pub events: u64,
    pub peak_package_steps: usize,
    /// Actual counter-prefix copy/freeze/search work, not simulated events.
    pub history_visits: u64,
    pub retirement_visits: u64,
}

struct State<'t> {
    sim: Sim<'t>,
    // Only self-modifying packages need snapshots across replay epochs.
    // Unchanged input remains in the caller's immutable part list.
    modified: BTreeMap<(usize, u32), Arc<Input>>,
}

fn refused(text: impl Into<String>) -> Finding {
    Finding::new(Kind::Model, "retained part sequence", text)
}

/// Exact immutable packages and their real epoch policy. Unsupported boundary
/// forms are refused, never silently passed to an unbounded whole-run checker.
pub fn check(runs: &[Nodes], t: &Target, context: &MemoryContext) -> Report {
    let history = crate::work::history_visits();
    let retirement = crate::work::retirement_visits();
    let mut report = crate::composition::prove(|| check_inner(runs, t, context)).0;
    report.history_visits = crate::work::history_visits().saturating_sub(history);
    report.retirement_visits = crate::work::retirement_visits().saturating_sub(retirement);
    report
}

fn check_inner(runs: &[Nodes], t: &Target, context: &MemoryContext) -> Report {
    let mut report = Report::default();
    let period = context.host_join_every;
    if !context.independent_packages || period == 0 || runs.is_empty()
        || runs.len() % period != 0 || !context.packages.is_empty()
    {
        report.findings.push(refused("part proof requires complete independent epochs and allocator-owned packages"));
        return report;
    }
    let participants: Vec<_> = runs[0].keys().copied().collect();
    if participants.is_empty() || runs.iter().any(|r| r.keys().copied().collect::<Vec<_>>() != participants) {
        report.findings.push(refused("part proof changed its participant set"));
        return report;
    }
    // Replays name the same retained allocations and bytes, not a new epoch's
    // unrelated executable. Mutated bytes are carried in State::modified.
    if runs.iter().enumerate().any(|(i, r)| i >= period && r != &runs[i % period]) {
        report.findings.push(refused("part replay changed immutable package identity"));
        return report;
    }
    let mut contracts = Vec::with_capacity(period);
    let mut channel = None;
    for (index, nodes) in runs[..period].iter().enumerate() {
        match Contract::of(nodes, index + 1 < period, channel) {
            Ok(c) => { channel = c.channel; contracts.push(c); },
            Err(e) => { report.findings.push(refused(e)); return report; }
        }
        for (&mesh, p) in nodes {
            if let Err(why) = crate::lines::admit(p, mesh, t) {
                report.findings.push(Finding::new(Kind::Line, format!("sysnode {mesh}"), why));
            }
        }
    }
    if report.findings.iter().any(|f| f.kind.refuses()) { return report; }
    let mut initial = Sim::new(t, None);
    initial.initialize_memory(context);
    initial.part_proof = true;
    let mut states = vec![State { sim: initial, modified: BTreeMap::new() }];
    for (ordinal, nodes) in runs.iter().enumerate() {
        let part = ordinal % period;
        let contract = &contracts[part];
        let mut following = Vec::new();
        for mut state in states {
            if ordinal > 0 && part == 0 {
                state.sim.part_host_join();
            }
            let inputs: BTreeMap<u32, Input> = nodes.iter().map(|(&mesh, p)| {
                let input = state.modified.get(&(part, mesh))
                    .map(|p| p.as_ref().clone()).unwrap_or_else(|| from_l0(p));
                (mesh, input)
            }).collect();
            // Mutations from a previous epoch must still satisfy the exact
            // control-flow/suffix contract before they can execute again.
            if let Err(why) = contract.check_inputs(&inputs) {
                report.findings.push(refused(why));
                return report;
            }
            if let Err(why) = state.sim.install_part(&inputs, part, contract.channel, &contract.checkpoints(&inputs)) {
                report.findings.push(refused(why));
                return report;
            }
            report.peak_package_steps = report.peak_package_steps.max(
                inputs.values().map(|p| p.steps.len()).sum(),
            );
            state.sim.part_totals(None);
            // Discovery supplies owner bounds for this CLOSED transition only.
            // It is not itself a race certificate. No suffix package can supply
            // a needed old token: the checked terminal protocol + slot balance
            // excludes that case. Keep each feasible branch correlated.
            let discovered = state.sim.clone().run(EVENT_LIMIT);
            let mut paths = Vec::new();
            for discovery in discovered {
                report.events = report.events.saturating_add(discovery.events);
                if discovery.findings.iter().any(|f| matches!(f.kind,
                    Kind::Deadlock | Kind::Model | Kind::Loop | Kind::Cluster | Kind::Vector))
                {
                    report.findings.extend(discovery.findings);
                    return report;
                }
                paths.push((discovery.path.clone(), discovery.tally()));
            }
            // Drop discovery machines before proof. Reuse the incoming machine
            // for the final (normally only) path instead of cloning it twice.
            let count = paths.len();
            let mut incoming = Some(state.sim);
            for (index, (path, totals)) in paths.into_iter().enumerate() {
                let mut proving = if index + 1 == count {
                    incoming.take().expect("last incoming proof state")
                } else {
                    incoming.as_ref().expect("incoming proof state").clone()
                };
                proving.choices = path;
                proving.part_totals(Some(totals));
                for mut proved in proving.run(EVENT_LIMIT) {
                    report.events = report.events.saturating_add(proved.events);
                    let mut found = proved.part_findings();
                    proved.witness(&mut found);
                    let failed = found.iter().any(|f| f.kind.refuses());
                    report.findings.extend(found);
                    if failed { return report; }
                    let mut modified = state.modified.clone();
                    for (&mesh, original) in nodes {
                        if original.steps.iter().any(|s| s.op == Op::Patch) {
                            modified.insert((part, mesh), Arc::new(proved.part_package(mesh, &inputs[&mesh])));
                        }
                    }
                    // Carry the full state, not Held or Values::after. No
                    // histories or counter publications are cleared here.
                    proved.part_diagnostics_clear();
                    following.push(State { sim: proved, modified });
                }
            }
        }
        if following.is_empty() {
            report.findings.push(refused("part proof has no feasible successor"));
            return report;
        }
        states = following;
        report.checked_parts += 1;
    }
    report
}
