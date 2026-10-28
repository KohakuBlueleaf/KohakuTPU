//! VC image consistency against the reference ISA and machine geometry.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use ktpu_isa::layout::{bits_for, mask};
use ktpu_isa::unit::vector::{core::*, VecCu, DIM_BOUND_BITS, DIM_STRIDE_BITS, WORD_BITS, WORD_HI};
use ktpu_isa::W256;

use crate::adapter::isa::{Fields, VOp};
use crate::Target;

/// CU padding is canonical except for IMEM's documented extended register bits.
pub(crate) fn cu(t: &Target, raw: W256) -> Result<(), String> {
    let isa = &t.isa.vector;
    let decoded = isa.decode_cu(raw).ok_or("no vector CU instruction")?;
    if let VecCu::Desc { fld, value, .. } = decoded {
        if fld != 0 && value >> (DIM_BOUND_BITS + DIM_STRIDE_BITS) != 0 {
            return Err("DESC dimension carries address high bits".into());
        }
        if fld > t.facts.vc_descriptor_dims {
            return Err("DESC field exceeds descriptor dimensions".into());
        }
    }
    let (name, _) = isa.set.decode(raw).map_err(|e| e.to_string())?;
    let fmt = isa.set.by_name(name).expect("decoded CU format");
    let padding = fmt.width - fmt.fields.iter().map(|f| f.width).sum::<u32>();
    let pad = fmt.field("_pad").map(|_| fmt.span("_pad"));
    if !raw.is_zero_in(0, padding)
        || pad.is_some_and(|(lo, width)| {
            let extended = if name == "IMEM" { WORD_HI } else { 0 };
            !raw.is_zero_in(lo + extended, width - extended)
        })
    {
        return Err("nonzero reserved CU bits".into());
    }
    Ok(())
}

pub(crate) fn word(t: &Target, raw: u64) -> Result<(VOp, Fields), String> {
    let isa = &t.isa.vector;
    let (name, fields) = isa
        .decode_word_reference(raw)
        .ok_or("no core instruction (reserved bits)")?;
    let op = VOp::from_name(name).ok_or("unknown core format")?;
    let f = Fields::from(fields);
    let g = |n| f.g(n);
    let fmt = isa.core.set.by_name(name).expect("decoded format");
    let xr =
        bits_for(isa.cfg.regs as u64).saturating_sub(isa.core.unpk.field("reg").unwrap().width);
    let mut high = 0;
    for (i, n) in ["vc", "vb", "va", "vd"].iter().enumerate() {
        if fmt.field(n).is_some() || (*n == "vd" && fmt.field("reg").is_some()) {
            high |= mask(xr) << (i as u32 * xr);
        }
    }
    if (raw >> WORD_BITS) & !high != 0 {
        return Err("unused extended register bits".into());
    }
    if op == VOp::Vcfg {
        return Err("unknown VCFG selector or nonzero reserved configuration bits".into());
    }
    for n in ["vd", "va", "vb", "vc", "reg"] {
        if g(n) >= i64::from(isa.cfg.regs) {
            return Err(format!("{name}: {n} exceeds vector registers"));
        }
    }
    if op.is_math()
        && ["sa", "sb", "sc"]
            .iter()
            .any(|n| !matches!(g(n), 0 | 1 | 3))
    {
        return Err("math source selector faults F_SRC".into());
    }
    if f.get("sreg").is_some_and(|r| r >= t.facts.vc_sregs) {
        return Err("scalar register exceeds machine registers".into());
    }
    if f.get("ad").is_some_and(|r| r >= 1 << isa.cfg.desc_sel_bits) {
        return Err("descriptor selector exceeds machine descriptors".into());
    }
    match op {
        VOp::Chk | VOp::Chkvl => {
            if op == VOp::Chkvl && g("vl1") >= i64::from(isa.cfg.vlmax) {
                return Err("CHKVL exceeds machine VL".into());
            }
            if ["a_base", "b_base", "c_base", "d_base"]
                .iter()
                .any(|n| g(n) >= i64::from(isa.cfg.chunks))
            {
                return Err("chunk configuration exceeds machine chunks".into());
            }
        }
        VOp::Xb if g("pr") >= t.facts.vc_preds => {
            return Err("predicate exceeds machine predicates".into())
        }
        VOp::Vunpk | VOp::Vpack => {
            let (n, cb, mode) = (g("n1") + 1, g("cb"), g("mode"));
            let chunks = i64::from(isa.cfg.chunks);
            let group = t.facts.vc_gt4_words;
            if n > chunks || cb >= chunks {
                return Err("walk exceeds machine chunks".into());
            }
            // Chunk and L1 walks wrap by definition; only faulting geometries refuse.
            if mode == 1 && n % group != 0 {
                return Err("GT4 chunk count faults F_GT4".into());
            }
            if mode == 3 && op == VOp::Vpack && !t.facts.vector_mx7_allowed {
                return Err("conversion.fp16_mx7 forbids vector_mx7".into());
            }
            if mode == 3 && (op == VOp::Vunpk || !t.facts.vc_has_mx || n != chunks || cb > 1) {
                return Err("MX7 walk geometry faults F_MODE".into());
            }
        }
        VOp::Vsync | VOp::Vwait | VOp::Vmark
            if g("waiter") > i64::from(W_D)
                || (g("waiter") == i64::from(W_MK)
                    && (op != VOp::Vmark || g("on") == i64::from(K_COUNT))) =>
        {
            return Err("sync waiter/kind faults F_SYNC".into());
        }
        _ => {}
    }
    Ok((op, f))
}

/// Visit both skip arms and one structural trip of each non-nested loop.
/// Immediate words are data, never decoded as instructions.
pub(crate) fn image(
    t: &Target,
    imem: &HashMap<i64, u64, impl std::hash::BuildHasher>,
    start: i64,
) -> Result<(), String> {
    let mut todo = vec![(start, None)];
    let mut seen = BTreeSet::new();
    let mut instructions = BTreeSet::new();
    let mut immediates = BTreeSet::new();
    while let Some((pc, end)) = todo.pop() {
        let end = end.filter(|&e| pc < e);
        if !seen.insert((pc, end)) {
            continue;
        }
        if pc < 0 || pc >= i64::from(t.isa.vector.cfg.imem_words()) {
            return Err(format!("pc {pc}: control flow exceeds IMEM"));
        }
        if immediates.contains(&pc) {
            return Err(format!("pc {pc}: control flow enters a VSETI immediate"));
        }
        instructions.insert(pc);
        let raw = *imem
            .get(&pc)
            .ok_or_else(|| format!("pc {pc}: no loaded instruction / missing VHALT"))?;
        let (op, f) = word(t, raw).map_err(|e| format!("pc {pc}: {e}"))?;
        let mut next = pc + 1;
        match op {
            VOp::Vhalt => continue,
            VOp::Vseti => {
                if !imem.contains_key(&next) || instructions.contains(&next) || end == Some(next) {
                    return Err(format!("pc {pc}: missing or overlapping VSETI immediate"));
                }
                immediates.insert(next);
                next += 1;
            }
            VOp::Vloop => {
                let last = next + f.g("body");
                if end.is_some()
                    || f.g("body") == 0
                    || last >= i64::from(t.isa.vector.cfg.imem_words())
                {
                    return Err(format!("pc {pc}: nested, empty or out-of-range loop body"));
                }
                todo.push((last, None));
                todo.push((next, Some(last)));
                continue;
            }
            VOp::Vskipz => {
                let target = next + f.g("skip");
                if end.is_some_and(|e| target > e) {
                    return Err(format!("pc {pc}: skip crosses loop boundary"));
                }
                todo.push((target, end));
            }
            _ => {}
        }
        todo.push((next, end));
    }
    Ok(())
}

/// Scalar control values at RUN boundaries; None is an unproven value.
pub(crate) type Scalars = BTreeMap<i64, Option<i64>>;
type ScalarLoop = Option<(i64, i64, bool)>;
type ScalarPc = (i64, ScalarLoop);
type ScalarWork = Vec<(ScalarPc, Scalars)>;

fn merge(into: &mut Scalars, other: &Scalars) -> bool {
    let before = into.clone();
    for key in before.keys().chain(other.keys()) {
        let a = before.get(key).copied().flatten();
        let b = other.get(key).copied().flatten();
        into.insert(*key, if a == b { a } else { None });
    }
    *into != before
}

/// A scalar fixed point over both skip arms and loop backedges. A skipped
/// assignment cannot establish the count or VL of its successor.
pub(crate) fn scalars(
    t: &Target,
    imem: &HashMap<i64, u64, impl std::hash::BuildHasher>,
    start: i64,
    initial: &Scalars,
) -> Result<Scalars, String> {
    let mut states = BTreeMap::<ScalarPc, Scalars>::new();
    let mut todo = vec![((start, None), initial.clone())];
    let mut result: Option<Scalars> = None;
    while let Some(((pc, lp), mut s)) = todo.pop() {
        if let Some(old) = states.get_mut(&(pc, lp)) {
            if !merge(old, &s) {
                continue;
            }
            s = old.clone();
        } else {
            states.insert((pc, lp), s.clone());
        }
        let (op, f) = word(t, imem[&pc])?;
        let mut next = pc + 1;
        let value = s.get(&f.g("sreg")).copied().flatten();
        match op {
            VOp::Vhalt => {
                if let Some(out) = &mut result {
                    merge(out, &s);
                } else {
                    result = Some(s);
                }
                continue;
            }
            VOp::Vseti => {
                if f.g("to_k") == 0 {
                    s.insert(f.g("sreg"), Some((imem[&next] & mask(SCALAR_BITS)) as i64));
                }
                next += 1;
            }
            VOp::Getstk => {
                s.insert(f.g("sreg"), None);
            }
            VOp::Vsetvl => {
                if !value.is_some_and(|v| v > 0 && v <= i64::from(t.isa.vector.cfg.vlmax)) {
                    return Err(format!("pc {pc}: VSETVL value is unknown or faults F_VL"));
                }
            }
            VOp::Vloop => {
                if value.is_none() {
                    return Err(format!("pc {pc}: a loop over a count not set by VSETI"));
                }
                todo.push((
                    (next, Some((next, next + f.g("body"), value.unwrap() > 1))),
                    s,
                ));
                continue;
            }
            VOp::Vskipz => {
                let target = next + f.g("skip");
                if value.is_none() {
                    conditional_block(t, imem, next, target)?;
                }
                if value.is_none() || value == Some(0) {
                    scalar_edge(&mut todo, target, lp, s.clone());
                }
                if value == Some(0) {
                    continue;
                }
            }
            _ => {}
        }
        scalar_edge(&mut todo, next, lp, s);
    }
    result.ok_or_else(|| "no reachable VHALT".into())
}

fn scalar_edge(todo: &mut ScalarWork, next: i64, lp: ScalarLoop, s: Scalars) {
    if let Some((first, end, repeat)) = lp {
        if next == end {
            if repeat {
                todo.push(((first, lp), s.clone()));
            }
            todo.push(((end, None), s));
            return;
        }
    }
    todo.push(((next, lp), s));
}

fn conditional_block(
    t: &Target,
    imem: &HashMap<i64, u64, impl std::hash::BuildHasher>,
    mut pc: i64,
    mut end: i64,
) -> Result<(), String> {
    while pc < end {
        let raw = *imem
            .get(&pc)
            .ok_or_else(|| format!("pc {pc}: skipped control block is not loaded"))?;
        let (op, f) = word(t, raw)?;
        if matches!(op, VOp::Vwait | VOp::Vpost | VOp::Vhalt)
            || (op == VOp::Vdrain && f.g("node") != 0)
        {
            return Err(format!("pc {pc}: {op} under a data-dependent skip"));
        }
        // Skipping a control header also makes its body's counter totals conditional.
        if matches!(op, VOp::Vloop | VOp::Vskipz) {
            let span = if op == VOp::Vloop {
                f.g("body")
            } else {
                f.g("skip")
            };
            end = end.max(pc + 1 + span);
        }
        pc += if op == VOp::Vseti { 2 } else { 1 };
    }
    Ok(())
}

/// DMA counts and strides are checked before footprint allocation.
pub(crate) fn dma(
    t: &Target,
    f: &Fields,
    desc: &HashMap<(i64, i64), i64, impl std::hash::BuildHasher>,
) -> Result<(), String> {
    let mut count = 1i64;
    for fld in 1..=t.facts.vc_descriptor_dims {
        let value = desc.get(&(f.g("ad"), i64::from(fld))).copied().unwrap_or(0) as u64;
        count = count
            .checked_mul((value & mask(DIM_BOUND_BITS)).max(1) as i64)
            .ok_or("descriptor count overflow")?;
        if count > t.facts.vc_walk_max {
            return Err("DMA descriptor count faults F_LEN".into());
        }
        if f.g("node") == 0 {
            let stride = (value >> DIM_BOUND_BITS) & mask(DIM_STRIDE_BITS);
            if !stride.is_multiple_of(t.facts.word_bytes as u64) {
                return Err("DMA descriptor stride is not word aligned".into());
            }
        }
    }
    if f.g("node") == 0
        && desc
            .get(&(f.g("ad"), 0))
            .copied()
            .unwrap_or(0)
            .rem_euclid(t.facts.word_bytes)
            != 0
    {
        return Err("DMA descriptor base is not word aligned".into());
    }
    Ok(())
}
