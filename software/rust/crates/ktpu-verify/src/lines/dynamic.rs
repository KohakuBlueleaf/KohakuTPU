//! Finite known-bit interpretation of all node paths. Every reachable GO
//! must cover whole lines; base addresses may vary only in line-high bits.

use std::collections::VecDeque;

use super::{alu, Mover, LIMIT};
use crate::Target;
use ktpu_l0::format::{F_IMM, MOVER_SKIP};
use ktpu_l0::{Op, Package};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct Bits {
    value: u64,
    unknown: u64,
}

impl Bits {
    const ANY: Self = Self {
        value: 0,
        unknown: u64::MAX,
    };

    fn exact(value: u64) -> Self {
        Self { value, unknown: 0 }
    }

    fn get(self, shift: u32, width: u32) -> Self {
        let mask = u64::MAX >> (64 - width);
        Self {
            value: (self.value >> shift) & mask,
            unknown: (self.unknown >> shift) & mask,
        }
    }

    fn known(self) -> Result<u64, ()> {
        if self.unknown == 0 {
            Ok(self.value)
        } else {
            Err(())
        }
    }

    fn join(&mut self, other: Self) -> bool {
        let mask = self.unknown | other.unknown | (self.value ^ other.value);
        let next = Self {
            value: self.value & !mask,
            unknown: mask,
        };
        let changed = *self != next;
        *self = next;
        changed
    }

    fn alu(op: u64, a: Self, b: Self) -> Self {
        if op == 0 {
            return b;
        }
        if a.unknown == 0 && b.unknown == 0 {
            return alu(op, a.value, b.value).map_or(Self::ANY, Self::exact);
        }
        let low = a.unknown.trailing_zeros().min(b.unknown.trailing_zeros());
        let (value, known) = match op {
            1 => (a.value.wrapping_add(b.value), low),
            2 => (a.value.wrapping_sub(b.value), low),
            3 => {
                // A constant multiplier also proves its power-of-two factor.
                let extra = if a.unknown == 0 {
                    a.value.trailing_zeros()
                } else if b.unknown == 0 {
                    b.value.trailing_zeros()
                } else {
                    0
                };
                (a.value.wrapping_mul(b.value), (low + extra).min(64))
            }
            4 => {
                return Self {
                    value: a.value & b.value,
                    unknown: (a.unknown & (b.value | b.unknown))
                        | (b.unknown & (a.value | a.unknown)),
                }
            }
            7 if b.unknown == 0 => {
                let shift = (b.value & 63) as u32;
                return Self {
                    value: a.value << shift,
                    unknown: a.unknown << shift,
                };
            }
            8 if b.unknown == 0 => {
                let shift = (b.value & 63) as u32;
                return Self {
                    value: a.value >> shift,
                    unknown: a.unknown >> shift,
                };
            }
            _ => return Self::ANY,
        };
        let unknown = u64::MAX.checked_shl(known).unwrap_or(0);
        Self {
            value: value & !unknown,
            unknown,
        }
    }
}

// Headers, committed DIM/AXIS pairs, windows, row, staged DIM, selector.
const ROW: usize = 38;
const DIM: usize = 39;
const SELECT: usize = 40;

#[derive(Clone, PartialEq, Eq)]
struct State {
    regs: [Bits; 16],
    payload: Vec<Bits>,
    mover: [Bits; 41],
}

impl State {
    fn join(&mut self, other: &Self) -> bool {
        let mut changed = false;
        for (a, b) in self
            .regs
            .iter_mut()
            .zip(&other.regs)
            .chain(self.payload.iter_mut().zip(&other.payload))
            .chain(self.mover.iter_mut().zip(&other.mover))
        {
            changed |= a.join(*b);
        }
        changed
    }

    fn put(&mut self, reg: u64, value: Bits, t: &Target, budget: &mut usize) -> Result<(), ()> {
        match reg {
            0x10 => {
                let sel = value.get(0, 1).known()? as usize;
                self.mover[SELECT] = Bits::exact(sel as u64);
                self.mover[sel] = value;
            }
            0x18 => {
                self.mover[SELECT] = value.get(0, 1);
                self.mover[DIM] = value;
            }
            0x20 => {
                let sel = self.mover[SELECT].known()? as usize;
                let dim = self.mover[DIM].get(1, 3).known()? as usize;
                self.mover[2 + sel * 8 + dim] = self.mover[DIM];
                self.mover[18 + sel * 8 + dim] = value;
            }
            0x28 => {
                let sel = value.get(0, 1).known()? as usize;
                let dim = value.get(1, 1).known()? as usize;
                self.mover[SELECT] = Bits::exact(sel as u64);
                self.mover[34 + sel * 2 + dim] = value;
            }
            0x50 => self.mover[ROW] = value,
            0 => {
                let go = value.known()?;
                if go & (1 << 16) != 0 {
                    *budget = budget.saturating_add(self.full_lines(go & 7, t)?);
                    if *budget > LIMIT {
                        return Err(());
                    }
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn full_lines(&self, mode: u64, t: &Target) -> Result<usize, ()> {
        let line = t.facts.line_bytes as u64;
        if !line.is_power_of_two() {
            return Err(());
        }
        let mut mover = Mover {
            full_only: true,
            ..Mover::default()
        };
        let mut findings = Vec::new();
        let mut put = |reg, value| mover.put(reg, value, t, 0, &mut findings).map_err(|_| ());
        let source =
            mode == t.isa.mover.modes.transform as u64 || mode == t.isa.mover.modes.scatter as u64;
        for sel in usize::from(!source)..2 {
            let header = self.mover[sel];
            let base_mask = ((1u64 << 40) - 1) << 4;
            let allowed = if sel == 1 {
                base_mask & !((line - 1) << 4)
            } else {
                base_mask
            };
            if header.unknown & !allowed != 0 {
                return Err(());
            }
            put(0x10, header.value)?;
            let ndim = ((header.value >> 44) & 7) as usize;
            for dim in 0..ndim {
                let value = self.mover[2 + sel * 8 + dim].known()?;
                put(0x18, (value & !15) | sel as u64 | ((dim as u64) << 1))?;
                put(0x20, self.mover[18 + sel * 8 + dim].known()?)?;
            }
            for dim in 0..2 {
                let value = self.mover[34 + sel * 2 + dim].known()?;
                put(0x28, (value & !3) | sel as u64 | ((dim as u64) << 1))?;
            }
        }
        if mode == t.isa.mover.modes.scatter as u64 {
            put(0x50, self.mover[ROW].known()?)?;
        }
        put(0, (1 << 16) | mode)?;
        if findings.is_empty() {
            Ok(mover.words)
        } else {
            Err(())
        }
    }

    fn patch(
        &mut self,
        index: u64,
        bit: u32,
        width: u32,
        value: Bits,
        add: bool,
    ) -> Result<(), ()> {
        if width == 0 || width > 64 || bit + width > 256 {
            return Err(());
        }
        let start = usize::try_from(index)
            .map_err(|_| ())?
            .checked_mul(4)
            .ok_or(())?;
        if start + 4 > self.payload.len() {
            return Err(());
        }
        let mut previous = Bits::default();
        for k in 0..width {
            let limb = self.payload[start + ((bit + k) / 64) as usize];
            let at = (bit + k) % 64;
            previous.value |= ((limb.value >> at) & 1) << k;
            previous.unknown |= ((limb.unknown >> at) & 1) << k;
        }
        let value = if add {
            Bits::alu(1, previous, value)
        } else {
            value
        };
        for k in 0..width {
            let limb = &mut self.payload[start + ((bit + k) / 64) as usize];
            let mask = 1 << ((bit + k) % 64);
            limb.value = (limb.value & !mask) | (((value.value >> k) & 1) * mask);
            limb.unknown = (limb.unknown & !mask) | (((value.unknown >> k) & 1) * mask);
        }
        Ok(())
    }
}

pub(super) fn proves(pkg: &Package, t: &Target) -> bool {
    if crate::mutant::on("verify.lines/no-dynamic-full-lines") {
        return false;
    }
    interpret(pkg, t).is_ok()
}

fn interpret(pkg: &Package, t: &Target) -> Result<(), ()> {
    let initial = State {
        regs: [Bits::default(); 16],
        payload: pkg
            .payloads
            .iter()
            .flat_map(|w| w.0.map(Bits::exact))
            .collect(),
        mover: [Bits::ANY; 41],
    };
    if pkg
        .steps
        .len()
        .saturating_mul(initial.payload.len().saturating_add(57))
        > LIMIT
    {
        return Err(());
    }
    let mut states = vec![None; pkg.steps.len()];
    let mut todo = VecDeque::from([(0, initial)]);
    let mut budget = 0;
    while let Some((pc, incoming)) = todo.pop_front() {
        let Some(step) = pkg.steps.get(pc) else {
            continue;
        };
        budget += 1;
        if budget > LIMIT {
            return Err(());
        }
        let state = &mut states[pc];
        let mut s = match state {
            Some(held) => {
                if !State::join(held, &incoming) {
                    continue;
                }
                held.clone()
            }
            None => {
                *state = Some(incoming.clone());
                incoming
            }
        };
        let r = usize::try_from(step.unit).map_err(|_| ())?;
        let a = s.regs[((step.count >> 8) & 15) as usize];
        let b = if step.flags & F_IMM != 0 {
            Bits::exact(step.arg)
        } else {
            s.regs[((step.count >> 12) & 15) as usize]
        };
        match step.op {
            Op::End => continue,
            Op::Load => {
                *s.regs.get_mut(r).ok_or(())? = if step.flags == F_IMM {
                    *s.payload.get(step.arg as usize * 4).ok_or(())?
                } else {
                    Bits::ANY
                }
            }
            Op::Alu => *s.regs.get_mut(r).ok_or(())? = Bits::alu(step.count & 255, a, b),
            Op::Jump => {
                let cond = step.count & 255;
                let go = if cond == 0 {
                    Some(true)
                } else if a.unknown != 0 || b.unknown != 0 {
                    None
                } else {
                    Some(match cond {
                        1 => a.value == b.value,
                        2 => a.value != b.value,
                        3 => a.value < b.value,
                        4 => a.value >= b.value,
                        5 => (a.value as i64) < (b.value as i64),
                        6 => (a.value as i64) >= (b.value as i64),
                        _ => return Err(()),
                    })
                };
                if go != Some(false) {
                    todo.push_back((r, s.clone()));
                }
                if go == Some(true) {
                    continue;
                }
            }
            Op::Patch => {
                if step.arg & (1 << 24) != 0 {
                    return Err(());
                }
                let shift = ((step.arg >> 16) & 255) as u32;
                if shift >= 64 {
                    return Err(());
                }
                let source = s.regs.get(r).ok_or(())?;
                s.patch(
                    step.count,
                    (step.arg & 255) as u32,
                    ((step.arg >> 8) & 255) as u32,
                    Bits {
                        value: source.value >> shift,
                        unknown: source.unknown >> shift,
                    },
                    step.arg & (1 << 26) != 0,
                )?;
            }
            // RX data is stored to DRAM, not this interpreter's payloads or
            // scalar registers. LOAD from that DRAM is still Bits::ANY.
            Op::Recv => {
                if crate::mutant::on("verify.lines/recv-is-payload") {
                    return Err(());
                }
            }
            Op::Mover | Op::Engine => {
                if step.count > LIMIT as u64 {
                    return Err(());
                }
                for k in 0..step.count {
                    let engine = step.op == Op::Engine;
                    let index = step
                        .arg
                        .checked_add(if engine { k / 3 } else { k })
                        .ok_or(())?;
                    let at = usize::try_from(index)
                        .map_err(|_| ())?
                        .checked_mul(4)
                        .ok_or(())?;
                    let w = s.payload.get(at..at.checked_add(4).ok_or(())?).ok_or(())?;
                    let entries = if engine {
                        let j = (k % 3) as usize;
                        let code = w[0].get((j * 8) as u32, 8).known()?;
                        if code & 64 != 0 && (8..24).contains(&(code & !64)) {
                            return Err(());
                        }
                        if (8..24).contains(&code) {
                            vec![((code - 8) * 8, w[j + 1])]
                        } else {
                            Vec::new()
                        }
                    } else {
                        let mut entries = Vec::new();
                        for half in [0, 2] {
                            let reg = w[half].known()?;
                            if reg != MOVER_SKIP {
                                entries.push((reg, w[half + 1]));
                            }
                        }
                        entries
                    };
                    for (reg, value) in entries {
                        s.put(reg, value, t, &mut budget)?;
                    }
                }
            }
            _ => {}
        }
        todo.push_back((pc + 1, s));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_bits_contain_every_four_bit_concretization() {
        let patterns: Vec<_> = (0..16)
            .flat_map(|unknown| {
                (0..16)
                    .filter(move |value| value & unknown == 0)
                    .map(move |value| Bits { value, unknown })
            })
            .collect();
        for &a in &patterns {
            for &b in &patterns {
                for op in [0, 1, 2, 3, 4, 7, 8] {
                    let result = Bits::alu(op, a, b);
                    for x in (0..16).filter(|x| x & !a.unknown == a.value) {
                        for y in (0..16).filter(|y| y & !b.unknown == b.value) {
                            let actual = alu(op, x, y).unwrap();
                            assert_eq!(actual & !result.unknown, result.value);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn replacing_and_adding_cross_limb_patches_preserves_known_low_bits() {
        let mut state = State {
            regs: [Bits::default(); 16],
            payload: vec![Bits::exact(0); 4],
            mover: [Bits::ANY; 41],
        };
        state
            .patch(
                0,
                60,
                40,
                Bits {
                    value: 0,
                    unknown: u64::MAX << 6,
                },
                false,
            )
            .unwrap();
        assert_eq!(state.payload[0].unknown, 0);
        assert_eq!(state.payload[1].unknown & 3, 0);
        state.patch(0, 60, 40, Bits::exact(64), true).unwrap();
        assert_eq!(state.payload[1].unknown & 3, 0);
        state.patch(0, 60, 40, Bits::exact(3), false).unwrap();
        assert_eq!(state.payload[0], Bits::exact(3 << 60));
        assert_eq!(state.payload[1], Bits::exact(0));
    }
}
