//! The scratch arenas of an L1 body: the parameters its caller does not pass
//! (the L2 pipeline's temporaries), one arena per (memory space, node), each
//! member at a write-line-aligned offset in parameter order. Tiled cluster
//! outputs also start on a complete `mg.wburst * die.word_bytes` burst. An arena
//! is one span, so one package buffer, whatever its member count.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_machine::Machine;

use crate::error::Result;
use crate::layout::buffer::Buffer;
use crate::text::{Module, Term};

/// One arena: its space and node, its exact size (a whole number of write lines)
/// and each member's offset.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Arena {
    pub space: String,
    pub node: Option<i128>,
    pub bytes: u64,
    pub members: Vec<(String, u64)>,
}

impl Arena {
    /// The arena's buffer name: `scratch:<space>[:<node>]`.
    pub fn name(&self) -> String {
        match self.node {
            Some(n) => format!("scratch:{}:{n}", self.space),
            None => format!("scratch:{}", self.space),
        }
    }
}

/// Runtime allocation layout after drain legalization, shared by plan pricing.
pub fn runtime_arenas(
    module: &Module,
    name: &str,
    caller: &BTreeSet<String>,
    machine: &Machine,
) -> Result<Vec<Arena>> {
    let plan = crate::lower::node::plan_drains(module, name, machine, None)?;
    arenas_with_bursts(&plan.module, name, caller, machine, &plan.burst_ports)
}

/// Prove capacity from placed allocation declarations. Drain legalization adds
/// only DRAM storage; its staging effect is burst alignment of existing members.
/// False means that the budget lies between the bounds and needs exact layout.
pub fn staging_bounds(
    params: &[(String, Term)],
    caller: &BTreeSet<String>,
    machine: &Machine,
    budget: ktpu_machine::StagingBudget,
) -> Result<bool> {
    let lower = arenas_of(params, caller, machine, &BTreeSet::new())?;
    check_staging(&lower, machine, budget)?;
    if !machine.flaws.f016.present {
        return Ok(true);
    }
    let stage: BTreeSet<_> = lower
        .iter()
        .filter(|arena| arena.space == "stage")
        .flat_map(|arena| arena.members.iter().map(|(name, _)| name.clone()))
        .collect();
    if stage.is_empty() {
        return Ok(true);
    }
    let line = u64::from(machine.card.dram.line_bytes).max(if machine.flaws.f014.present {
        128
    } else {
        1
    });
    let burst = u64::from(machine.mg.wburst) * u64::from(machine.die.word_bytes);
    // Nested alignment grids make all-member alignment an upper bound.
    if line.max(burst) % line != 0 {
        return Ok(false);
    }
    let upper = arenas_of(params, caller, machine, &stage)?;
    Ok(check_staging(&upper, machine, budget).is_ok())
}

/// Admit a complete candidate, expanding drains only at a capacity boundary.
pub fn admit_staging(
    module: &Module,
    name: &str,
    caller: &BTreeSet<String>,
    machine: &Machine,
    budget: ktpu_machine::StagingBudget,
) -> Result<()> {
    if staging_bounds(&module.body(name, "l1")?.params, caller, machine, budget)? {
        return Ok(());
    }
    check_staging(
        &runtime_arenas(module, name, caller, machine)?,
        machine,
        budget,
    )
}

/// Admission of complete runtime windows, including alignment between arenas.
/// Scratch members retain their allocation for the whole launch; graph aliases
/// already represent any storage reused across disjoint lifetimes.
pub fn check_staging(
    arenas: &[Arena],
    machine: &Machine,
    budget: ktpu_machine::StagingBudget,
) -> Result<()> {
    let align = u64::from(machine.card.dram.line_bytes).max(256);
    let mut used = BTreeMap::<i128, u64>::new();
    for arena in arenas.iter().filter(|a| a.space == "stage") {
        let node = arena.node.unwrap_or(budget.default_node);
        let bytes = arena.bytes.div_ceil(align).checked_mul(align);
        let total = used.entry(node).or_default();
        *total = bytes.and_then(|n| total.checked_add(n)).ok_or_else(|| {
            crate::error::Error::new("DSL.KERN.020", "staging capacity: footprint overflows")
        })?;
    }
    for (node, bytes) in used {
        if bytes > budget.bytes {
            return Err(crate::error::Error::new(
                "DSL.KERN.020",
                format!(
                    "staging capacity: sysnode {node} needs {bytes} bytes, budget {}",
                    budget.bytes,
                ),
            ));
        }
    }
    Ok(())
}

/// The arenas of `fn name.l1`: every parameter not in `caller`, in the
/// arena of its space and node, in first-member order.
pub fn arenas(
    module: &Module,
    name: &str,
    caller: &BTreeSet<String>,
    machine: &Machine,
) -> Result<Vec<Arena>> {
    arenas_with_bursts(module, name, caller, machine, &BTreeSet::new())
}

/// Arenas whose named members also satisfy memory-drain burst proofs.
pub fn arenas_with_bursts(
    module: &Module,
    name: &str,
    caller: &BTreeSet<String>,
    machine: &Machine,
    burst_members: &BTreeSet<String>,
) -> Result<Vec<Arena>> {
    arenas_of(
        &module.body(name, "l1")?.params,
        caller,
        machine,
        burst_members,
    )
}

/// Parameter-order layout, also used by allocation-only capacity proofs.
fn arenas_of(
    params: &[(String, Term)],
    caller: &BTreeSet<String>,
    machine: &Machine,
    burst_members: &BTreeSet<String>,
) -> Result<Vec<Arena>> {
    if crate::mutant::on("l1.layout/no-arena") {
        return Ok(Vec::new());
    }
    let line = if crate::mutant::on("l1.layout/word-aligned-arena") {
        machine.die.word_bytes
    } else {
        machine.card.dram.line_bytes
    } as u64;
    if line == 0 {
        return Err(crate::error::Error::new(
            "MACHINE",
            "DRAM write line is zero",
        ));
    }
    let line = if machine.flaws.f014.present {
        line.max(128)
    } else {
        line
    };
    let up = |n: u64| n.div_ceil(line) * line;
    let mut out: Vec<Arena> = Vec::new();
    for (param, t) in params {
        if caller.contains(param) {
            continue;
        }
        let b = Buffer::of(t)?;
        let node = b.layout.get("node").and_then(|v| v.int());
        let i = match out
            .iter()
            .position(|a| a.space == b.space && a.node == node)
        {
            Some(i) => i,
            None => {
                out.push(Arena {
                    space: b.space.clone(),
                    node,
                    bytes: 0,
                    members: Vec::new(),
                });
                out.len() - 1
            }
        };
        let a = &mut out[i];
        if b.layout.contains_key("tile") || burst_members.contains(param) {
            let burst = u64::from(machine.mg.wburst) * u64::from(machine.die.word_bytes);
            let align = line.max(burst);
            a.bytes = a.bytes.div_ceil(align) * align;
        }
        a.members.push((param.clone(), a.bytes));
        a.bytes = up(a.bytes + b.bytes(machine)?);
    }
    Ok(out)
}
