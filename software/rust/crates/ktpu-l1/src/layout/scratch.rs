//! The scratch arenas of an L1 body: the parameters its caller does not pass
//! (the L2 pipeline's temporaries), one arena per (memory space, node), each
//! member at a write-line-aligned offset in parameter order. Tiled cluster
//! outputs also start on a complete `mg.wburst * die.word_bytes` burst. An arena
//! is one span, so one package buffer, whatever its member count.

use std::collections::BTreeSet;

use ktpu_machine::Machine;

use crate::error::Result;
use crate::layout::buffer::Buffer;
use crate::text::Module;

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

/// The arenas of `fn name.l1`: every parameter not in `caller`, in the
/// arena of its space and node, in first-member order.
pub fn arenas(
    module: &Module,
    name: &str,
    caller: &BTreeSet<String>,
    machine: &Machine,
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
    let up = |n: u64| n.div_ceil(line) * line;
    let mut out: Vec<Arena> = Vec::new();
    for (param, t) in &module.body(name, "l1")?.params {
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
        if b.layout.contains_key("tile") {
            let burst = u64::from(machine.mg.wburst) * u64::from(machine.die.word_bytes);
            let align = line.max(burst);
            a.bytes = a.bytes.div_ceil(align) * align;
        }
        a.members.push((param.clone(), a.bytes));
        a.bytes = up(a.bytes + b.bytes(machine)?);
    }
    Ok(out)
}
