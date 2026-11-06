//! Concurrent pre-engine L0 streams. Unit words use the encoded ISA path;
//! signal slots belong to their receiving sysnode.

use std::collections::BTreeMap;

use ktpu_isa::W256;
use ktpu_l0::format::{F_CONSUME, F_IMM};
use ktpu_l0::{Op, Package};

use super::{words, Units};
use crate::machine::{Machine, Unit};
use crate::units::link::Place;
use crate::R;

struct Node<'a> {
    mesh: usize,
    pkg: &'a Package,
    pc: usize,
    units: Vec<Unit>,
    base: Vec<u64>,
    sent: Vec<u64>,
    expected: Vec<u64>,
    frontiers: BTreeMap<u64, (usize, u64)>,
}

fn done(m: &Machine, unit: Unit) -> u64 {
    match unit {
        Unit::Vc(i) => m.vc[i].done,
        Unit::Mg(i) => m.mg[i].done,
        Unit::Mover => m.moves_done,
    }
}

/// Execute bound packages before engine lowering, admitting every node before
/// waiting for completion. Unsupported node opcodes use the simulator's error.
pub fn run(packages: &BTreeMap<u32, Package>, units: &Units, m: &mut Machine) -> R<()> {
    let mut places = BTreeMap::new();
    for (coords, vc) in [(&units.vc, true), (&units.mg, false)] {
        for (i, &(x, y)) in coords.iter().enumerate() {
            let unit = if vc { Unit::Vc(i) } else { Unit::Mg(i) };
            places.insert((x, y), unit);
            m.places.insert(
                (x as u32, y as u32),
                if vc { Place::Vc(i) } else { Place::Mg(i) },
            );
        }
    }
    let mut nodes = Vec::new();
    for (&mesh, pkg) in packages {
        let mapped = pkg
            .units
            .iter()
            .map(|u| {
                places
                    .get(&(u.x, u.y))
                    .map(|&u| m.in_mesh(mesh as usize, u))
                    .ok_or_else(|| format!("no unit at ({}, {})", u.x, u.y))
            })
            .collect::<R<Vec<_>>>()?;
        nodes.push(Node {
            mesh: mesh as usize,
            pkg,
            pc: 0,
            base: mapped.iter().map(|&u| done(m, u)).collect(),
            sent: vec![0; mapped.len()],
            expected: vec![0; mapped.len()],
            units: mapped,
            frontiers: BTreeMap::new(),
        });
    }
    let isa = m.isa.clone().ok_or("no ISA")?;
    let mut slots: BTreeMap<(usize, u64), u64> = BTreeMap::new();
    while nodes.iter().any(|n| n.pc < n.pkg.steps.len()) {
        let mut progress = false;
        for n in &mut nodes {
            let Some(s) = n.pkg.steps.get(n.pc) else {
                continue;
            };
            let u = s.unit as usize;
            let reached = |u: usize, target: u64| done(m, n.units[u]) >= n.base[u] + target;
            match s.op {
                Op::Dispatch => {
                    for k in 0..s.count {
                        let w = W256(n.pkg.payloads[(s.arg + k) as usize].0);
                        match n.units[u] {
                            Unit::Vc(i) => m.send_vc(i, words::vector(&isa, w)?)?,
                            Unit::Mg(i) => m.send_mg(i, words::cluster(&isa, w)?)?,
                            Unit::Mover => unreachable!("unit table contains compute units"),
                        }
                    }
                    n.sent[u] += s.count;
                }
                Op::Await => {
                    let target = if s.flags == F_IMM {
                        n.sent[u]
                    } else {
                        n.expected[u] + s.count
                    };
                    if !reached(u, target) {
                        continue;
                    }
                    n.expected[u] = target;
                }
                Op::Barrier | Op::End => {
                    if !(0..n.units.len()).all(|u| reached(u, n.sent[u].max(n.expected[u]))) {
                        continue;
                    }
                    if s.op == Op::End {
                        n.pc = n.pkg.steps.len();
                        progress = true;
                        continue;
                    }
                }
                Op::Frontier => match s.flags {
                    0 => {
                        n.frontiers.insert(s.count, (u, n.sent[u]));
                    }
                    1 => {
                        let &(owner, target) = n
                            .frontiers
                            .get(&s.count)
                            .ok_or("FRONTIER wait has no saved target")?;
                        if owner != u {
                            return Err("FRONTIER unit mismatch".into());
                        }
                        if !reached(u, target) {
                            continue;
                        }
                        n.expected[u] = n.expected[u].max(target);
                    }
                    2 => n
                        .frontiers
                        .retain(|k, _| !(s.arg..s.arg + s.count).contains(k)),
                    _ => return Err("invalid FRONTIER flags".into()),
                },
                Op::Ring => {
                    *slots.entry((u, s.count)).or_default() += s.arg & 0xffff;
                }
                Op::WaitSlot => {
                    let value = slots.entry((n.mesh, s.unit)).or_default();
                    if *value < s.count {
                        continue;
                    }
                    if s.flags & F_CONSUME != 0 {
                        *value -= s.count;
                    }
                }
                op => return Err(format!("{} in the pre-engine card simulator", op.name())),
            }
            n.pc += 1;
            progress = true;
        }
        if !progress && !m.step()? {
            return Err("a card wait no unit can end".into());
        }
    }
    m.until(Machine::idle)
}
