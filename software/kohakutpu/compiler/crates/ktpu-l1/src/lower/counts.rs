//! Specialize bind-time register-count prefixes without executing a kernel.
use std::collections::BTreeMap;

use ktpu_l0::format::{Op, Step, F_IMM};
use ktpu_l0::Package;

pub fn specialize(pkg: &Package) -> Option<Package> {
    // A jump could revisit a specialized target without replaying the prefix.
    if pkg.steps.iter().any(|s| s.op == Op::Jump) {
        return None;
    }
    let mut regs = BTreeMap::new();
    let mut out = pkg.clone();
    let mut changed = false;
    for i in 0..out.steps.len() {
        let s = if crate::mutant::on("imr/prefix-stale-alu") {
            pkg.steps[i]
        } else {
            out.steps[i]
        };
        if s.op == Op::Alu && s.count & 255 == 0 && s.flags & F_IMM != 0 {
            regs.insert(s.unit, s.arg);
        } else if s.op == Op::Patch && s.arg >> 24 & 1 != 0 && s.count as usize > i {
            let value = *regs.get(&s.unit)?;
            let bit = s.arg & 255;
            let width = (s.arg >> 8) & 255;
            // Count-path sites affect only count or repeat multiplicity, not opcode/topology.
            let target = out.steps.get_mut(s.count as usize)?;
            let field = if bit >= 32 && bit + width <= 64 {
                &mut target.count
            } else if bit >= 64 && bit + width <= 128 {
                &mut target.arg
            } else {
                return None;
            };
            let low = bit % 32;
            let low = if bit >= 64 { bit - 64 } else { low };
            let mask = if width == 64 {
                u64::MAX
            } else {
                (1 << width) - 1
            };
            let mut v = value >> ((s.arg >> 16) & 255);
            if s.arg >> 26 & 1 != 0 {
                v = v.wrapping_add(*field >> low);
            }
            *field = (*field & !(mask << low)) | ((v & mask) << low);
            out.steps[i] = Step::bare(Op::Settle);
            changed = true;
        } else {
            break;
        }
    }
    changed.then_some(out)
}
