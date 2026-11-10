//! A terminal all-participant protocol extracted from exact bound bytes.
//! The channel is isolated throughout the epoch, not just in the final suffix.
use std::collections::BTreeMap;
use crate::adapter::{l0::from_l0, package::{op, flag, Package, Step, PATCH_STEPS}};
use crate::check::Nodes;

pub(super) struct Contract {
    participants: Vec<u32>,
    pub channel: Option<u16>,
    successor: bool,
    joins: usize,
}

impl Contract {
    pub fn of(nodes: &Nodes, successor: bool, channel: Option<u16>) -> Result<Self, String> {
        let inputs: BTreeMap<_, _> = nodes.iter().map(|(&n, p)| (n, from_l0(p))).collect();
        let participants: Vec<_> = inputs.keys().copied().collect();
        let mut channel = channel;
        if successor && participants.len() > 1 && channel.is_none() {
            let p = &inputs[&participants[0]];
            let end = body_end(p)?;
            let first = end.checked_sub(participants.len()).ok_or("missing retained join")?;
            let wait = p.steps[first];
            if wait.op != op::WAIT_SLOT { return Err("missing retained gather wait".into()); }
            channel = Some(wait.unit);
        }
        let mut result = Self { participants, channel, successor, joins: 0 };
        result.joins = result.validate(&inputs)?;
        Ok(result)
    }

    pub fn check_inputs(&self, nodes: &BTreeMap<u32, Package>) -> Result<(), String> {
        if self.validate(nodes)? != self.joins {
            return Err("retained mutation changed the join sequence".into());
        }
        Ok(())
    }

    pub fn checkpoints(&self, nodes: &BTreeMap<u32, Package>) -> BTreeMap<u32, (Vec<usize>, usize)> {
        let coordinator = self.participants[0];
        nodes.iter().map(|(&mesh, p)| {
            let mut entries = Vec::new();
            let mut tail = p.steps.len();
            if let Some(slot) = self.channel {
                for (i, s) in p.steps.iter().enumerate() {
                    if (mesh == coordinator && s.op == op::WAIT_SLOT && s.unit == slot)
                        || (mesh != coordinator && s.op == op::RING && s.count == u32::from(slot)) {
                        entries.push(i);
                        if self.successor { tail = i + if mesh == coordinator { nodes.len() } else { 2 }; }
                    }
                }
            }
            (mesh, (entries, tail))
        }).collect()
    }

    fn validate(&self, nodes: &BTreeMap<u32, Package>) -> Result<usize, String> {
        if !nodes.keys().copied().eq(self.participants.iter().copied()) {
            return Err("retained join participant mismatch".into());
        }
        let coordinator = *self.participants.first().ok_or("empty part")?;
        let mut common_count = None;
        for (&mesh, p) in nodes {
            let end = body_end(p)?;
            let mut blocks = Vec::new();
            if let Some(slot) = self.channel {
                let n = if mesh == coordinator { nodes.len() } else { 2 };
                let mut i = 0;
                while i < end {
                    let s = p.steps[i];
                    let uses = (s.op == op::RING && s.count == u32::from(slot))
                        || (s.op == op::WAIT_SLOT && s.unit == slot);
                    if !uses { i += 1; continue; }
                    if i + n > end {
                        return Err("retained join channel is used outside a complete drained protocol".into());
                    }
                    let block = &p.steps[i..i+n];
                    let wait = if mesh == coordinator { block[0] } else { block[1] };
                    let need = if mesh == coordinator { nodes.len() as u32 - 1 } else { 1 };
                    if wait != Step::new(op::WAIT_SLOT, slot, need, 0, flag::CONSUME) {
                        return Err("missing consuming retained join wait".into());
                    }
                    let rings: Vec<_> = if mesh == coordinator {
                        self.participants.iter().copied().filter(|&n| n != mesh)
                            .zip(block[1..].iter()).collect()
                    } else { vec![(coordinator, &block[0])] };
                    for (destination, ring) in rings {
                        if ring.op != op::RING || u32::from(ring.unit) != destination
                            || ring.count != u32::from(slot) || ring.flags != 0
                            || ring.arg & 0xffff > 1 || ring.arg & !0x1ffff != 0 {
                            return Err("missing retained gather/release ring".into());
                        }
                    }
                    blocks.push((i, i+n));
                    i += n;
                }
                if self.successor && blocks.last().map(|b| b.1) != Some(end) {
                    return Err("nonterminal retained part lacks trailing gather/release".into());
                }
            }
            // No conditional may skip, repeat or jump into a join. Otherwise a
            // numerically balanced token could belong to the wrong generation.
            // Body loops remain legal within their single launch region.
            for (index, step) in p.steps.iter().enumerate() {
                match step.op {
                    op::END if index + 1 != p.steps.len() =>
                        return Err("retained part has an early END".into()),
                    op::JUMP => {
                        let target = usize::from(step.unit);
                        if target >= p.steps.len() || blocks.iter().any(|&(lo, hi)| {
                            (lo..hi).contains(&target) || (index < lo) != (target < lo)
                        }) { return Err("retained branch crosses its join generation".into()); }
                    }
                    op::PATCH if step.arg & PATCH_STEPS != 0 =>
                        return Err("step-modifying part lacks an immutable join contract".into()),
                    op::REPEAT => return Err("encoded REPEAT is not a retained join certificate".into()),
                    _ => (),
                }
            }
            if common_count.replace(blocks.len()).is_some_and(|n| n != blocks.len()) {
                return Err("retained participants execute different join generations".into());
            }
        }
        Ok(common_count.unwrap_or(0))
    }
}

fn body_end(p: &Package) -> Result<usize, String> {
    let mut end = p.steps.len();
    // The in-memory L0 representation may omit END; Node::load appends it.
    if end > 0 && p.steps[end-1].op == op::END { end -= 1; }
    loop {
        if end == 0 { break; }
        let s = p.steps[end-1];
        if s.op == op::BARRIER { end -= 1; continue; }
        // Program::build appends await_all() before its terminal barrier.
        // These ordinary completion waits are as protocol-only as ENGINE WAITs.
        // This only finds the suffix: part_join_drain still checks acquisition
        // AT the gather/release entry, so a drain after release cannot repair it.
        if s.op == op::AWAIT && usize::from(s.unit) < p.units.len()
            && s.arg == 0 && (s.flags == 0 || (s.flags == flag::IMM && s.count == 0)) {
            end -= 1;
            continue;
        }
        if s.op != op::ENGINE { break; }
        let mut waits = true;
        for entry in 0..s.count {
            let at = s.arg.checked_add(u64::from(entry / 3)).ok_or("ENGINE extent overflow")?;
            let word = p.payloads.get(usize::try_from(at).map_err(|_| "ENGINE extent overflow")?)
                .ok_or("ENGINE outside payloads")?;
            let code = (word.0[0] >> ((entry % 3) * 8)) & 255;
            if code != ktpu_l0::engine::WAIT { waits = false; break; }
        }
        if !waits { break; }
        end -= 1;
    }
    Ok(end)
}
