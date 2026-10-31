//! Field-by-field ISA confinement: every patch site is decoded to its named ISA
//! field from the machine's ISA spec facts and classified as hazard-relevant
//! (registers, addresses, extents, loop/skip counts, sync fields, opcodes) or
//! data-only (arithmetic immediates, XB amounts).
use ktpu_isa::layout::value;
use ktpu_isa::unit::mover::{
    R_AXIS, R_CTRL, R_DIM, R_HDR, R_IMM, R_INDEX, R_ROW, R_SEED, R_WINDOW,
};
use ktpu_isa::unit::vector::{VecCu, DIM_BOUND_BITS, DIM_STRIDE_BITS};
use ktpu_isa::{Format, Isa, W256};
use ktpu_l0::format::template::{Template, ValuePatch};
use ktpu_l0::Op;
use ktpu_machine::Machine;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FieldClass {
    Register,
    Address,
    Extent,
    LoopCount,
    Sync,
    Opcode,
    ArithmeticImmediate,
    XbAmount,
}

impl FieldClass {
    pub fn is_data_only(self) -> bool {
        match self {
            FieldClass::ArithmeticImmediate | FieldClass::XbAmount => true,
            FieldClass::LoopCount if ktpu_verify::mutant::on("verify.confinement/loop-data") => {
                true
            }
            _ => false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Certificate {
    pub version: u32,
    pub data_only: BTreeMap<String, bool>,
    pub sites: Vec<String>,
    pub classes: Vec<FieldClass>,
}

fn bits(format: &Format, name: &str) -> Option<(u32, u32)> {
    let mut at = format.width;
    for f in &format.fields {
        at -= f.width;
        if f.name == name {
            return Some((at, f.width));
        }
    }
    None
}

fn mask(width: u32) -> u64 {
    if width >= 64 {
        u64::MAX
    } else {
        (1u64 << width) - 1
    }
}

/// All potentially changed bits, proved by affine extrema and constant low bits.
fn changed(t: &Template, p: &ValuePatch) -> Option<u64> {
    if p.width > 64 || p.shift != 0 || p.buffer.is_some() {
        return None;
    }
    let (mut lo, mut hi) = (p.constant as i128, p.constant as i128);
    let mut zeros = 64;
    for (name, coefficient) in &p.coefficients {
        let b = t.table.parameters.get(name)?;
        let a = (*coefficient as i128).checked_mul(b.lo as i128)?;
        let z = (*coefficient as i128).checked_mul(b.hi as i128)?;
        lo = lo.checked_add(a.min(z))?;
        hi = hi.checked_add(a.max(z))?;
        if b.lo != b.hi {
            zeros = zeros.min(coefficient.unsigned_abs().trailing_zeros());
        }
    }
    if lo < 0 || hi > mask(p.width) as i128 {
        return None;
    }
    let (lo, hi) = (lo as u64, hi as u64);
    let spread = mask(64 - (lo ^ hi).leading_zeros()) & !mask(zeros);
    let original = t.package.payloads.get(p.payload)?.get(p.bit, p.width);
    Some(spread | (original ^ lo))
}

fn invariant(t: &Template, index: usize, spans: &[(u32, u32)], precise: bool) -> Option<()> {
    let overlaps = |bit, width| spans.iter().any(|(b, w)| bit < b + w && *b < bit + width);
    if t.package
        .relocs
        .iter()
        .any(|r| r.payload as usize == index && overlaps(r.bit as u32, r.width as u32))
    {
        return None;
    }
    for p in t
        .table
        .sites
        .iter()
        .filter(|s| s.step.is_none() && s.payload == index)
    {
        if precise {
            let delta = changed(t, p)?;
            if (0..p.width).any(|b| delta & (1 << b) != 0 && overlaps(p.bit + b, 1)) {
                return None;
            }
        } else if overlaps(p.bit, p.width) {
            return None;
        }
    }
    Some(())
}

struct Contexts {
    safe_xb: BTreeSet<usize>,
    vseti_imms: BTreeMap<usize, bool>,
}

/// Resident VC IMEM contexts: identifies safe instruction words and VSETI immediate words.
fn contexts(t: &Template, isa: &Isa) -> Contexts {
    let vc_code = ktpu_l0::format::type_code("VC").unwrap_or(0x5643);
    let mut images: BTreeMap<u64, BTreeMap<u32, usize>> = BTreeMap::new();
    let mut vseti_imms: BTreeMap<usize, bool> = BTreeMap::new();
    let (mut seen, mut unsafe_) = (BTreeSet::new(), BTreeSet::new());
    let static_steps = t.table.sites.iter().all(|p| {
        p.step.is_none_or(|i| {
            t.package
                .steps
                .get(i)
                .is_some_and(|s| matches!(s.op, Op::Await | Op::Mwait))
        })
    }) && t.package.steps.iter().all(|s| {
        matches!(
            s.op,
            Op::End | Op::Mover | Op::Await | Op::Mwait | Op::Barrier | Op::Dispatch | Op::Repeat
        )
    });
    for _ in 0..2 {
        for step in &t.package.steps {
            let (unit, start, count) = match step.op {
                Op::End => break,
                Op::Dispatch if step.flags == 0 => {
                    (step.unit, step.arg as usize, step.count as usize)
                }
                Op::Repeat => (
                    step.unit,
                    (step.arg & 0xffff_ffff) as usize,
                    step.count as usize,
                ),
                _ => continue,
            };
            let Some(u) = t.package.units.get(unit as usize) else {
                continue;
            };
            if u.type_code != vc_code {
                continue;
            }
            let image = images.entry(unit).or_default();
            for index in start..start.saturating_add(count) {
                let Some(&raw) = t.package.payloads.get(index) else {
                    break;
                };
                let Some(cu) = isa.vector.decode_cu(raw) else {
                    unsafe_.insert(index);
                    continue;
                };
                let mut headers = Vec::new();
                if let Some(span) = bits(&isa.vector.imem, "op") {
                    headers.push(span);
                }
                if matches!(cu, VecCu::Imem { .. }) {
                    if let Some(span) = bits(&isa.vector.imem, "addr") {
                        headers.push(span);
                    }
                    if let Some(span) = bits(&isa.vector.imem, "addr_hi") {
                        headers.push(span);
                    }
                }
                if invariant(t, index, &headers, false).is_none() {
                    unsafe_.insert(index);
                }
                match cu {
                    VecCu::Imem { addr, .. } => {
                        image.insert(addr, index);
                        if addr > 0 {
                            if let Some(&prev) = image.get(&(addr - 1)) {
                                if let Some(VecCu::Imem { word: pw, .. }) =
                                    isa.vector.decode_cu(t.package.payloads[prev])
                                {
                                    if let Some(("VSETI", pf)) = isa.vector.decode_word(pw) {
                                        let to_k = value(&pf, "to_k") != 0;
                                        vseti_imms
                                            .entry(index)
                                            .and_modify(|k| *k &= to_k)
                                            .or_insert(to_k);
                                    }
                                }
                            }
                        }
                    }
                    VecCu::Run { .. } => {
                        for (&addr, &at) in image.iter() {
                            seen.insert(at);
                            if addr == 0 {
                                continue;
                            }
                            let valid = (|| {
                                let previous = *image.get(&(addr - 1))?;
                                let VecCu::Imem { word, .. } =
                                    isa.vector.decode_cu(t.package.payloads[previous])?
                                else {
                                    return None;
                                };
                                let (name, pf) = isa.vector.decode_word(word)?;
                                if name == "VSETI" {
                                    let to_k = value(&pf, "to_k") != 0;
                                    vseti_imms
                                        .entry(at)
                                        .and_modify(|k| *k &= to_k)
                                        .or_insert(to_k);
                                    return None;
                                }
                                let format = isa
                                    .vector
                                    .core
                                    .formats()
                                    .into_iter()
                                    .find(|f| f.name == name)?;
                                let fixed: Vec<_> = format
                                    .fields
                                    .iter()
                                    .filter(|f| f.konst.is_some())
                                    .filter_map(|f| bits(&format, f.name))
                                    .collect();
                                invariant(t, previous, &fixed, true)
                            })();
                            if valid.is_none() {
                                unsafe_.insert(at);
                            }
                        }
                    }
                    VecCu::Desc { .. } => (),
                }
            }
        }
    }
    let safe_xb = if static_steps {
        seen.difference(&unsafe_).copied().collect()
    } else {
        BTreeSet::new()
    };
    Contexts {
        safe_xb,
        vseti_imms,
    }
}

fn vc_core_field_class(fmt: &str, field: &str) -> FieldClass {
    match (fmt, field) {
        (_, "op" | "sel" | "om" | "mode" | "xc" | "xm") => FieldClass::Opcode,
        ("XB", "xk") => FieldClass::XbAmount,
        ("VLOOP", "body") | ("VSKIPZ", "skip") => FieldClass::LoopCount,
        ("VSYNC" | "VWAIT" | "VMARK" | "VPOST" | "VBAR", _) | (_, "q" | "signal") => {
            FieldClass::Sync
        }
        (
            _,
            "vd" | "va" | "vb" | "vc" | "sa" | "sb" | "sc" | "reg" | "sreg" | "ad" | "pr" | "pm"
            | "to_k" | "dst_x" | "dst_y" | "buf_id" | "node",
        ) => FieldClass::Register,
        (_, "ptr" | "off" | "rel") => FieldClass::Address,
        _ => FieldClass::Extent,
    }
}

fn mg_field_class(fmt: &str, field: &str) -> FieldClass {
    match (fmt, field) {
        (_, "op" | "acc" | "emit" | "fuse" | "recv" | "dnode") => FieldClass::Opcode,
        (_, "addr" | "addr_hi" | "eoff" | "aoff" | "boff") => FieldClass::Address,
        ("POST", "n") | (_, "credit" | "dflags" | "dack_x" | "dack_y") => FieldClass::Sync,
        (_, "n" | "gm" | "gn" | "nk" | "npeer") => FieldClass::Extent,
        (_, "anchor") => FieldClass::ArithmeticImmediate,
        _ => FieldClass::Register,
    }
}

fn sr_field_class(_fmt: &str, field: &str) -> FieldClass {
    match field {
        "op" | "what" | "amem" | "peer" => FieldClass::Opcode,
        "addr" | "aaddr" | "oaddr" | "baddr" | "first" | "poff" | "word" => FieldClass::Address,
        "n" | "count" | "ax" | "ay" => FieldClass::Sync,
        "words" | "rows" | "astride" | "ostride" | "bstride" | "value" => FieldClass::Extent,
        _ => FieldClass::Register,
    }
}

fn active_span(t: &Template, p: &ValuePatch) -> (u32, u32) {
    if p.buffer.is_some() {
        return (p.bit, p.width);
    }
    if let Some(delta) = changed(t, p) {
        if delta != 0 {
            let lo = delta.trailing_zeros();
            let hi = 64 - delta.leading_zeros();
            return (p.bit + lo, hi - lo);
        }
    }
    (p.bit, p.width)
}

fn match_format_field(
    prefix: &str,
    fmt: &Format,
    base_offset: u32,
    span: (u32, u32),
    classify: impl Fn(&str, &str) -> FieldClass,
) -> Option<(String, FieldClass)> {
    let (s_bit, s_width) = span;
    for f in &fmt.fields {
        if f.name.starts_with('_') || f.name == "z" || f.name == "z1" {
            continue;
        }
        let (f_lo, f_w) = bits(fmt, f.name)?;
        let start = base_offset + f_lo;
        if s_bit >= start && s_bit + s_width <= start + f_w {
            return Some((
                format!("{prefix}.{}.{}[{start}+{f_w}]", fmt.name, f.name),
                classify(fmt.name, f.name),
            ));
        }
    }
    // If a wide lane patch covers a single non-constant field of fmt, resolve to it.
    let mut overlapped = Vec::new();
    for f in &fmt.fields {
        if f.konst.is_some() || f.name.starts_with('_') {
            continue;
        }
        let (f_lo, f_w) = bits(fmt, f.name)?;
        let start = base_offset + f_lo;
        if s_bit < start + f_w && start < s_bit + s_width {
            overlapped.push((f.name, start, f_w));
        }
    }
    if overlapped.len() == 1 {
        let (name, start, f_w) = overlapped[0];
        return Some((
            format!("{prefix}.{}.{name}[{start}+{f_w}]", fmt.name),
            classify(fmt.name, name),
        ));
    }
    None
}

fn classify_vc_payload(
    t: &Template,
    p: &ValuePatch,
    raw: W256,
    isa: &Isa,
    ctx: &Contexts,
) -> Option<(String, FieldClass)> {
    let span = active_span(t, p);
    let cu = isa.vector.decode_cu(raw)?;
    match cu {
        VecCu::Run { .. } => {
            match_format_field("VC", &isa.vector.run, 0, span, |_, name| match name {
                "op" => FieldClass::Opcode,
                _ => FieldClass::Address,
            })
        }
        VecCu::Desc { fld, .. } => {
            let (val_lo, val_w) = bits(&isa.vector.desc, "value")?;
            if fld != 0 && span.0 >= val_lo && span.0 + span.1 <= val_lo + val_w {
                let bound_span = (val_lo, DIM_BOUND_BITS);
                let stride_span = (val_lo + DIM_BOUND_BITS, DIM_STRIDE_BITS);
                if span.0 >= stride_span.0 && span.0 + span.1 <= stride_span.0 + stride_span.1 {
                    return Some((
                        format!("VC.DESC.stride[{}+{}]", stride_span.0, stride_span.1),
                        FieldClass::Extent,
                    ));
                }
                if span.0 >= bound_span.0 && span.0 + span.1 <= bound_span.0 + bound_span.1 {
                    return Some((
                        format!("VC.DESC.bound[{}+{}]", bound_span.0, bound_span.1),
                        FieldClass::Extent,
                    ));
                }
                return Some((
                    format!("VC.DESC.value[{val_lo}+{val_w}]"),
                    FieldClass::Extent,
                ));
            }
            for f in &isa.vector.desc.fields {
                if f.name.starts_with('_') {
                    continue;
                }
                let (f_lo, f_w) = bits(&isa.vector.desc, f.name)?;
                if span.0 >= f_lo && span.0 + span.1 <= f_lo + f_w {
                    let cls = match f.name {
                        "op" => FieldClass::Opcode,
                        "ad" | "fld" => FieldClass::Register,
                        "value" | "value_hi" if fld == 0 => FieldClass::Address,
                        _ => FieldClass::Extent,
                    };
                    return Some((format!("VC.DESC.{}[{f_lo}+{f_w}]", f.name), cls));
                }
            }
            None
        }
        VecCu::Imem { addr, word } => {
            for header in ["op", "addr", "addr_hi"] {
                if let Some((f_lo, f_w)) = bits(&isa.vector.imem, header) {
                    if span.0 >= f_lo && span.0 + span.1 <= f_lo + f_w {
                        let cls = if header == "op" {
                            FieldClass::Opcode
                        } else {
                            FieldClass::Address
                        };
                        return Some((format!("VC.IMEM.{header}[{f_lo}+{f_w}]"), cls));
                    }
                }
            }
            let (word_base, _) = bits(&isa.vector.imem, "word")?;
            let preceded_by_vseti = ctx.vseti_imms.get(&p.payload).copied().or_else(|| {
                t.package.payloads.iter().find_map(|w| {
                    let VecCu::Imem { addr: at, word: pw } = isa.vector.decode_cu(*w)? else {
                        return None;
                    };
                    if at.checked_add(1) == Some(addr) {
                        let (name, pf) = isa.vector.decode_word(pw)?;
                        if name == "VSETI" {
                            return Some(value(&pf, "to_k") != 0);
                        }
                    }
                    None
                })
            });
            if let Some(to_k) = preceded_by_vseti {
                if span.0 >= word_base && span.0 + span.1 <= word_base + 32 {
                    let imm_start = word_base;
                    if to_k {
                        return Some((
                            format!("VC.IMEM.VSETI.k_imm[{imm_start}+24]"),
                            FieldClass::ArithmeticImmediate,
                        ));
                    }
                    return Some((
                        format!("VC.IMEM.VSETI.s_imm[{imm_start}+24]"),
                        FieldClass::LoopCount,
                    ));
                }
            }
            if span.0 >= 32 && span.0 + span.1 <= 40 {
                return Some((
                    format!("VC.IMEM.REG_HI[{}+{}]", span.0, span.1),
                    FieldClass::Register,
                ));
            }
            let fmt = isa
                .vector
                .core
                .set
                .find(W256::from_u64(word & 0xFFFF_FFFF))?;
            match_format_field("VC.IMEM", fmt, word_base, span, vc_core_field_class)
        }
    }
}

fn classify_mover_payload(
    t: &Template,
    p: &ValuePatch,
    raw: W256,
    isa: &Isa,
) -> Option<(String, FieldClass)> {
    let span = active_span(t, p);
    let half = if span.0 < 128 { 0u32 } else { 128u32 };
    if span.0 < half + 64 {
        return Some((format!("MOVER.REG.addr[{half}+64]"), FieldClass::Register));
    }
    let reg = raw.get(half, 64) as u32;
    let base = half + 64;
    let ab = isa.mover.addr_bits;
    let table: &[(&str, u32, u32, FieldClass)] = match reg {
        R_CTRL => &[
            ("CTRL.mode", 0, 3, FieldClass::Opcode),
            ("CTRL.ewidth", 3, 2, FieldClass::Extent),
            ("CTRL.flags", 8, 8, FieldClass::Extent),
            ("CTRL.go", 16, 1, FieldClass::Opcode),
            ("CTRL.index_fp", 17, 1, FieldClass::Opcode),
        ],
        R_HDR => &[
            ("HDR.sel", 0, 1, FieldClass::Register),
            ("HDR.base", 4, ab, FieldClass::Address),
            ("HDR.levels", 44, 3, FieldClass::Extent),
            ("HDR.xform_id", 47, 4, FieldClass::Opcode),
            ("HDR.xform_mode", 55, 4, FieldClass::Opcode),
        ],
        R_DIM => &[
            ("DIM.sel", 0, 1, FieldClass::Register),
            ("DIM.level", 1, 3, FieldClass::Register),
            ("DIM.count", 4, 16, FieldClass::Extent),
            ("DIM.stride", 20, 32, FieldClass::Extent),
        ],
        R_AXIS => &[
            ("AXIS.axis", 0, 2, FieldClass::Register),
            ("AXIS.astep", 2, 16, FieldClass::Extent),
        ],
        R_WINDOW => &[
            ("WINDOW.sel", 0, 1, FieldClass::Register),
            ("WINDOW.axis", 1, 1, FieldClass::Register),
            ("WINDOW.base", 2, 16, FieldClass::Extent),
            ("WINDOW.extent", 18, 16, FieldClass::Extent),
        ],
        R_INDEX => &[
            ("INDEX.base", 0, ab, FieldClass::Address),
            ("INDEX.count", 40, 16, FieldClass::Extent),
        ],
        R_SEED => &[("SEED.seed", 0, 64, FieldClass::ArithmeticImmediate)],
        R_IMM => &[("IMM.imm", 0, 32, FieldClass::ArithmeticImmediate)],
        R_ROW => &[
            ("ROW.pitch", 0, 32, FieldClass::Extent),
            ("ROW.words", 32, 16, FieldClass::Extent),
        ],
        _ => return None,
    };
    for &(name, rel_lo, width, cls) in table {
        let start = base + rel_lo;
        if span.0 >= start && span.0 + span.1 <= start + width {
            return Some((format!("MOVER.{name}[{start}+{width}]"), cls));
        }
    }
    for &(name, rel_lo, width, cls) in table {
        let start = base + rel_lo;
        if span.0 < start + width && start < span.0 + span.1 {
            return Some((format!("MOVER.{name}[{start}+{width}]"), cls));
        }
    }
    None
}

/// Decode a patch site to its `(named_isa_field, FieldClass)` using the machine's ISA spec facts.
pub fn classify_site(
    t: &Template,
    p: &ValuePatch,
    isa: &Isa,
) -> Result<(String, FieldClass), String> {
    let ctx = contexts(t, isa);
    classify_site_with_contexts(t, p, isa, &ctx)
}

fn classify_site_with_contexts(
    t: &Template,
    p: &ValuePatch,
    isa: &Isa,
    ctx: &Contexts,
) -> Result<(String, FieldClass), String> {
    if let Some(step_idx) = p.step {
        let step = t
            .package
            .steps
            .get(step_idx)
            .ok_or_else(|| format!("step index {step_idx} out of bounds"))?;
        let (field, cls) = match (step.op, p.bit, p.width) {
            (Op::Repeat, 96, 16) => ("REPEAT.repeats", FieldClass::LoopCount),
            (Op::Jump, 64, 64) => ("JUMP.arg", FieldClass::LoopCount),
            (Op::Alu, 64, 64) => ("ALU.arg", FieldClass::Extent),
            (Op::Dispatch, 32, 32) => ("DISPATCH.count", FieldClass::Extent),
            (Op::Fetch, 32, 32) => ("FETCH.count", FieldClass::Extent),
            (Op::Await, 32, 32) => ("AWAIT.count", FieldClass::Sync),
            (Op::Mwait, 32, 32) => ("MWAIT.count", FieldClass::Sync),
            _ => {
                return Err(format!(
                    "unrecognized step patch {:?}[{}+{}]",
                    step.op, p.bit, p.width
                ))
            }
        };
        return Ok((format!("L0.STEP.{field}[{}+{}]", p.bit, p.width), cls));
    }
    let raw = *t
        .package
        .payloads
        .get(p.payload)
        .ok_or_else(|| format!("payload index {} out of bounds", p.payload))?;
    let pidx = p.payload as u64;
    let is_mover = t
        .package
        .steps
        .iter()
        .any(|s| s.op == Op::Mover && pidx >= s.arg && pidx < s.arg.saturating_add(s.count));
    if is_mover {
        return classify_mover_payload(t, p, raw, isa)
            .ok_or_else(|| format!("unrecognized MOVER field at payload {}", p.payload));
    }
    for s in &t.package.steps {
        if s.op == Op::Repeat {
            let first = s.arg & 0xffff_ffff;
            let nincs = s.arg >> 48;
            let ntab = nincs.div_ceil(2);
            if pidx >= first.saturating_add(s.count)
                && pidx < first.saturating_add(s.count).saturating_add(ntab)
            {
                let span = active_span(t, p);
                let half = if span.0 < 128 { 0u32 } else { 128u32 };
                let (name, start, width, cls) = if span.0 < half + 32 {
                    ("relative", half, 32, FieldClass::Address)
                } else if span.0 < half + 40 {
                    ("bit", half + 32, 8, FieldClass::Address)
                } else if span.0 < half + 48 {
                    ("width", half + 40, 8, FieldClass::Extent)
                } else {
                    ("delta", half + 64, 64, FieldClass::Extent)
                };
                return Ok((format!("L0.REPEAT.{name}[{start}+{width}]"), cls));
            }
        }
    }
    let mut unit_types = BTreeSet::new();
    for s in &t.package.steps {
        let (u, first, count) = match s.op {
            Op::Dispatch => (s.unit, s.arg, s.count),
            Op::Repeat => (s.unit, s.arg & 0xffff_ffff, s.count),
            _ => continue,
        };
        if pidx >= first && pidx < first.saturating_add(count) {
            if let Some(unit) = t.package.units.get(u as usize) {
                unit_types.insert(unit.type_code);
            }
        }
    }
    let vc_code = ktpu_l0::format::type_code("VC").unwrap_or(0x5643);
    let mg_code = ktpu_l0::format::type_code("MG").unwrap_or(0x4D47);
    let sr_code = ktpu_l0::format::type_code("SR").unwrap_or(0x5352);
    if unit_types.len() == 1 {
        let ut = *unit_types.iter().next().unwrap();
        if ut == vc_code {
            return classify_vc_payload(t, p, raw, isa, ctx)
                .ok_or_else(|| format!("unrecognized VC field at payload {}", p.payload));
        }
        if ut == mg_code {
            let span = active_span(t, p);
            let fmt = isa
                .cluster
                .set
                .find(raw)
                .ok_or_else(|| format!("unrecognized MG word at payload {}", p.payload))?;
            return match_format_field("MG", fmt, 0, span, mg_field_class)
                .ok_or_else(|| format!("unrecognized MG field at payload {}", p.payload));
        }
        if ut == sr_code {
            let span = active_span(t, p);
            let fmt = isa
                .sru
                .set
                .find(raw)
                .ok_or_else(|| format!("unrecognized SR word at payload {}", p.payload))?;
            return match_format_field("SR", fmt, 0, span, sr_field_class)
                .ok_or_else(|| format!("unrecognized SR field at payload {}", p.payload));
        }
    }
    if !unit_types.is_empty() {
        return Err(format!(
            "ambiguous or unsupported ISA context at payload {}",
            p.payload
        ));
    }
    // Fallback for unreferenced payloads in synthetic fixtures: decode against VC/MG/SR.
    if let Some(res) = classify_vc_payload(t, p, raw, isa, ctx) {
        return Ok(res);
    }
    Err(format!(
        "unrecognized ISA field at payload {} [{}+{}]",
        p.payload, p.bit, p.width
    ))
}

fn site_is_confined_data(t: &Template, p: &ValuePatch, cls: FieldClass, ctx: &Contexts) -> bool {
    // Named-field classification is not itself a proof of checker independence.
    // Only XB amounts have the resident-image witness needed for exemption.
    // Other arithmetic fields remain conservatively checked at every sample.
    let witnessed = cls == FieldClass::XbAmount
        || (cls == FieldClass::LoopCount
            && ktpu_verify::mutant::on("verify.confinement/loop-data"));
    if !witnessed || !cls.is_data_only() || p.step.is_some() || p.buffer.is_some() {
        return false;
    }
    if changed(t, p).is_none() {
        return false;
    }
    if cls == FieldClass::XbAmount {
        if !ctx.safe_xb.contains(&p.payload) {
            return false;
        }
        if t.table.sites.iter().any(|other| {
            other.step.is_none() && other.payload == p.payload && !std::ptr::eq(other, p)
        }) || t
            .package
            .relocs
            .iter()
            .any(|r| r.payload as usize == p.payload)
        {
            return false;
        }
    }
    true
}

pub fn derive(t: &Template, machine: &Machine) -> Result<Certificate, String> {
    let isa = Isa::new(machine).map_err(|e| e.to_string())?;
    let ctx = contexts(t, &isa);
    let mut data_only: BTreeMap<_, _> = t
        .table
        .parameters
        .keys()
        .map(|n| (n.clone(), true))
        .collect();
    let mut sites = Vec::with_capacity(t.table.sites.len());
    let mut classes = Vec::with_capacity(t.table.sites.len());
    for p in &t.table.sites {
        let (field_name, cls) = classify_site_with_contexts(t, p, &isa, &ctx)?;
        let mut is_data = site_is_confined_data(t, p, cls, &ctx);
        if cls == FieldClass::XbAmount {
            // A wide patch may overlap constant opcode bits. Its complete
            // possible-change span, not just its first decoded field, must fit xk.
            let (base, _) = bits(&isa.vector.imem, "word").ok_or("missing IMEM word")?;
            let xb = isa
                .vector
                .core
                .set
                .by_name("XB")
                .ok_or("missing XB format")?;
            let (bit, width) = bits(xb, "xk").ok_or("missing XB amount")?;
            let (start, count) = active_span(t, p);
            is_data &= start >= base + bit && start + count <= base + bit + width;
        }
        for (name, coefficient) in &p.coefficients {
            if *coefficient != 0 {
                let flag = data_only.get_mut(name).ok_or("unknown patch parameter")?;
                *flag &= is_data;
            }
        }
        sites.push(field_name);
        classes.push(cls);
    }
    Ok(Certificate {
        version: 2,
        data_only,
        sites,
        classes,
    })
}
