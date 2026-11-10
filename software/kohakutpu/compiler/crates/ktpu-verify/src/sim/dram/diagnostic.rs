//! Bounded evidence for the FIRST component refusal. No proof decision reads
//! this module. Clocks are sparse projections, with explicit omitted counts.
//! No per-access diagnostic provenance is collected. Historical access/launch
//! counts are explicitly untracked; gap/back describe a retained shard envelope
//! selected only on refusal. Raw packages need not use the reserved join slot.

use super::components::Failure;
use crate::model::clock::Clock;
use crate::sim::{Pk, Sim};

const TEXT_LIMIT: usize = 768;

// ASCII even for caller-controlled names; truncation is byte-safe and explicit.
fn clip(text: &str, limit: usize) -> String {
    let text: String = text
        .chars()
        .map(|c| {
            if c.is_ascii_graphic() || c == ' ' {
                c
            } else {
                '?'
            }
        })
        .collect();
    if text.len() <= limit {
        return text;
    }
    if limit == 0 {
        return String::new();
    }
    format!("{}~", &text[..limit - 1])
}

fn field(out: &mut String, key: &str, value: &str, limit: usize) {
    let room = TEXT_LIMIT.saturating_sub(out.len() + key.len() + 2);
    if room == 0 {
        return;
    }
    out.push(' ');
    out.push_str(key);
    out.push('=');
    out.push_str(&clip(value, room.min(limit)));
}

fn entries(values: impl IntoIterator<Item = String>, limit: usize) -> String {
    let values: Vec<_> = values.into_iter().collect();
    let mut out = String::from("[");
    let mut used = 0;
    for value in &values {
        // Reserve enough for the final omitted-count suffix and closing bracket.
        let reserve = format!(",+{}]", values.len()).len();
        if out.len() + value.len() + usize::from(used != 0) + reserve > limit {
            break;
        }
        if used != 0 {
            out.push(',');
        }
        out.push_str(value);
        used += 1;
    }
    if used < values.len() {
        if used != 0 {
            out.push(',');
        }
        out.push_str(&format!("+{}", values.len() - used));
    }
    out.push(']');
    out
}

fn sparse(clock: &Clock, processes: usize, focus: &[usize], limit: usize) -> String {
    // Missing coordinates first, then other nonzero coordinates in numeric order.
    let mut order = focus.to_vec();
    order.extend((0..processes).filter(|p| !focus.contains(p) && clock.at(*p) != 0));
    entries(order.iter().map(|&p| format!("{p}:{}", clock.at(p))), limit)
}

impl Sim<'_> {
    pub(crate) fn dram_origin(&self, owner: usize) -> Option<(u32, usize)> {
        let process = self.procs.get(owner)?;
        let mesh = match process.kind {
            Pk::Node(node) => self.nodes[node].mesh,
            Pk::MgSeq(u)
            | Pk::MgDq(u)
            | Pk::VcCu(u)
            | Pk::VcFe(u)
            | Pk::VcEng(u, _)
            | Pk::SrShell(u) => self.units[u].key.0,
            Pk::Port => {
                if let Some(issuer) = self.card.visibility.issuer(owner) {
                    // A response port is created for an execution issuer, never
                    // for another response port (visibility::memory_write).
                    return self.dram_origin(issuer);
                }
                if let Some(node) = self.nodes.iter().find(|n| n.owns_process(owner)) {
                    node.mesh
                } else {
                    self.units
                        .iter()
                        .find(|u| u.port == owner || u.completer == owner)?
                        .key
                        .0
                }
            }
        };
        let node = *self.node_of.get(&mesh)?;
        Some((mesh, self.nodes[node].proc))
    }

    pub(crate) fn dram_failure_text(&self, failure: &Failure) -> String {
        let n = self.procs.len();
        let (gaps, reverse): (Vec<usize>, Vec<usize>) = failure.previous.as_ref().map_or_else(
            || (Vec::new(), Vec::new()),
            |(earliest, latest)| {
                (
                    (0..n)
                        .filter(|&p| latest.at(p) > failure.start.at(p))
                        .collect(),
                    (0..n)
                        .filter(|&p| failure.done.at(p) > earliest.at(p))
                        .collect(),
                )
            },
        );
        let focus: Vec<_> = gaps.iter().copied().take(2).collect();
        let mut out = String::from("global DRAM proof budget exhausted in an unordered component");
        field(&mut out, "work", &failure.work.to_string(), 20);
        field(&mut out, "accesses", "untracked", 20);
        field(&mut out, "launches~", "untracked", 20);
        field(&mut out, "gap_scope", "prior_shard", 20);
        if let Some((mesh, _)) = self.dram_origin(failure.owner) {
            field(&mut out, "mesh", &mesh.to_string(), 12);
        }
        field(
            &mut out,
            "owner",
            &format!("{}:{}", failure.owner, self.procs[failure.owner].name),
            42,
        );
        field(&mut out, "access", &failure.what, 42);
        for (key, clock) in [
            ("start", &failure.start),
            ("done", &failure.done),
            ("latest", &failure.latest),
        ] {
            field(&mut out, key, &sparse(clock, n, &focus, 44), 44);
        }
        let (gap_text, back_text) = if let Some((earliest, latest)) = &failure.previous {
            (
                entries(
                    gaps.iter()
                        .map(|&p| format!("{p}:{}>{}", latest.at(p), failure.start.at(p))),
                    48,
                ),
                entries(
                    reverse
                        .iter()
                        .map(|&p| format!("{p}:{}>{}", failure.done.at(p), earliest.at(p))),
                    32,
                ),
            )
        } else {
            ("none".into(), "none".into())
        };
        field(&mut out, "gap", &gap_text, 48);
        field(&mut out, "back", &back_text, 32);
        // Name an unacquired memory response when present, not merely the
        // lowest-numbered CPU coordinate in the envelope. Both remain in gap.
        // Select only among actual gaps; provenance never creates a dependency.
        let gap_owner = gaps
            .iter()
            .copied()
            .find(|&p| self.card.visibility.issuer(p).is_some())
            .or_else(|| gaps.first().copied());
        if let Some(p) = gap_owner {
            field(
                &mut out,
                "gap_owner",
                &format!("{p}:{}", self.procs[p].name),
                42,
            );
        }
        // The retained composer uses the highest common slot (slots-1). Sorted
        // destination/owner order, never HashMap or simulator visitation order.
        // Counts are cumulative per owner; clocks show the owner event plus the
        // first two missing coordinates, not merely the raw counter value.
        let slot = self.t.machine.il.slots.saturating_sub(1);
        let mut marks = Vec::new();
        for counter in &self.counters {
            let Some(rest) = counter.name.strip_prefix("slot:") else {
                continue;
            };
            let Some((destination, channel)) = rest.split_once(':') else {
                continue;
            };
            let (Ok(destination), Ok(channel)) =
                (destination.parse::<u32>(), channel.parse::<u64>())
            else {
                continue;
            };
            if channel != slot as u64 {
                continue;
            }
            for (owner, history) in counter.histories() {
                if let Some((count, clock)) = history.last() {
                    marks.push((destination, owner, *count, clock));
                }
            }
        }
        marks.sort_by_key(|&(destination, owner, _, _)| (destination, owner));
        let room = TEXT_LIMIT.saturating_sub(out.len() + 10).min(232);
        let marks = entries(
            marks.into_iter().map(|(destination, owner, count, clock)| {
                let mut coords = vec![owner];
                coords.extend(focus.iter().copied().filter(|p| *p != owner));
                let projection = coords
                    .iter()
                    .map(|&p| format!("{p}:{}", clock.at(p)))
                    .collect::<Vec<_>>()
                    .join(",");
                format!("{destination}<{owner}:{count}@{projection}")
            }),
            room,
        );
        field(&mut out, &format!("J{slot}"), &marks, room);
        out
    }
}
