//! Static mover payloads have no abstract-machine consumers. A single ordinary
//! write stream cannot compete for partial lines; its walk must still be legal.

use super::proof::{extrema, Refusal};
use ktpu_isa::unit::mover::{R_AXIS, R_CTRL, R_DIM, R_HDR, R_WINDOW};
use ktpu_l0::format::template::Template;
use ktpu_l0::{Op, Package};
use ktpu_verify::Target;
use serde::Serialize;

pub const NO_SUMMARY: &str = "verify.template/mover-no-summary";
pub const NO_GUARDS: &str = "verify.template/mover-no-guards";
pub const NO_PROJECTION: &str = "verify.template/mover-no-projection";

#[derive(Debug, Clone, Serialize)]
pub struct WalkBound {
    pub count_min: i128,
    pub count_max: i128,
    pub stride_min: i128,
    pub stride_max: i128,
}

#[derive(Debug, Clone, Serialize)]
pub struct SingleStream {
    pub base: u64,
    pub dimensions: Vec<WalkBound>,
    pub address_min: i128,
    pub address_max: i128,
    pub words_max: i128,
    pub argument: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct Certificate {
    pub mesh: u32,
    pub execution: &'static str,
    pub single_stream: Option<SingleStream>,
    pub summary_refusal: Option<Refusal>,
}

#[derive(Clone, Copy, Default)]
struct Range {
    lo: i128,
    hi: i128,
    aligned: bool,
}

#[derive(Clone, Copy, Default)]
struct Dim {
    count: Range,
    stride: Range,
}

#[derive(Clone, Default)]
struct Walker {
    base: u64,
    ndim: usize,
    dims: [Dim; 8],
}

/// No control, payload mutation, dispatch, or other consumer of mover words.
/// Exact field patches cannot alter register selectors, GO, or walk structure.
pub(super) fn derive(
    mesh: u32,
    template: &Template,
    bound: &Package,
    target: &Target,
) -> Option<Certificate> {
    if !bound.units.is_empty()
        || bound
            .steps
            .iter()
            .any(|s| !matches!(s.op, Op::Mover | Op::Mwait | Op::Barrier | Op::End))
        || template.table.sites.iter().any(|p| {
            p.step.is_some()
                || p.buffer.is_some()
                || p.shift != 0
                || !matches!((p.bit % 128, p.width), (68, 16) | (84, 32))
                || bound
                    .payloads
                    .get(p.payload)
                    .is_none_or(|w| w.get(p.bit / 128 * 128, 64) != u64::from(R_DIM))
        })
    {
        return None;
    }
    // A relocation may only replace a fixed HDR base, never the DIM operands.
    if bound.relocs.iter().any(|r| {
        let half = r.bit as u32 / 128 * 128;
        bound.payloads.get(r.payload as usize).is_none_or(|w| {
            w.get(half, 64) != u64::from(R_HDR)
                || (r.bit as u32) < half + 68
                || r.bit as u32 + r.width as u32 > half + 108
        })
    }) {
        return None;
    }
    let result = if ktpu_verify::mutant::on(NO_SUMMARY) {
        Err(Refusal::new("mover summary", "summary disabled"))
    } else {
        single_stream(template, bound, target)
    };
    let (single_stream, summary_refusal) = match result {
        Ok(Some(summary)) => (Some(summary), None),
        Ok(None) => (None, Some(Refusal::new("mover summary", "no GO stream"))),
        Err(refusal) => (None, Some(refusal)),
    };
    Some(Certificate {
        mesh,
        execution: "empty unit table; fixed MOVER/MWAIT/BARRIER/END steps; payloads consumed only by independent line checker; simulation and dispatch order invariant",
        single_stream, summary_refusal,
    })
}

fn range(
    t: &Template,
    bound: &Package,
    payload: usize,
    bit: u32,
    width: u32,
    word: i128,
) -> Result<Range, Refusal> {
    let patch = t
        .table
        .sites
        .iter()
        .find(|p| p.payload == payload && p.bit == bit);
    let (mut lo, mut hi, aligned) = if let Some(p) = patch {
        let (lo, hi, origin, _) = extrema(t, p)?;
        let mut at = p.constant as i128;
        for (n, c) in &p.coefficients {
            at += *c as i128 * origin[n] as i128;
        }
        let aligned = at % word == 0
            && p.coefficients.iter().all(|(n, c)| {
                t.table.parameters[n].lo == t.table.parameters[n].hi || *c as i128 % word == 0
            });
        (lo, hi, aligned)
    } else {
        let value = bound.payloads[payload].get(bit, width) as i128;
        (value, value, value % word == 0)
    };
    if width == 32 {
        // A signed field must not cross the sign discontinuity.
        if lo < 1 << 31 && hi >= 1 << 31 {
            return Err(Refusal::new(
                "mover stride",
                "signed range crosses discontinuity",
            ));
        }
        if lo >= 1 << 31 {
            lo -= 1i128 << 32;
            hi -= 1i128 << 32;
        }
    }
    Ok(Range { lo, hi, aligned })
}

fn single_stream(
    t: &Template,
    bound: &Package,
    target: &Target,
) -> Result<Option<SingleStream>, Refusal> {
    let (line, word) = (
        target.facts.line_bytes as i128,
        target.facts.word_bytes as i128,
    );
    if line <= 0 || word <= 0 || line % word != 0 || (1i128 << 32) % word != 0 {
        return Err(Refusal::new(
            "mover summary",
            "unsupported line/word geometry",
        ));
    }
    let mut walkers = [Walker::default(), Walker::default()];
    let mut selector = 0;
    let mut pending = (0, Dim::default());
    let mut answer = None;
    let no_guards = ktpu_verify::mutant::on(NO_GUARDS);
    for s in &bound.steps {
        if s.op == Op::End {
            break;
        }
        if s.op != Op::Mover {
            continue;
        }
        let Some(end) = s.arg.checked_add(s.count) else {
            return Err(Refusal::new("mover summary", "payload range overflow"));
        };
        if end > bound.payloads.len() as u64 {
            return Err(Refusal::new(
                "mover summary",
                "payload range outside package",
            ));
        }
        for i in s.arg as usize..end as usize {
            for half in [0, 128] {
                let raw = bound.payloads[i];
                let (reg, value) = (raw.get(half, 64), raw.get(half + 64, 64));
                match reg as u32 {
                    R_HDR if reg == u64::from(R_HDR) => {
                        selector = (value & 1) as usize;
                        walkers[selector].base = (value >> 4) & ((1 << 40) - 1);
                        walkers[selector].ndim = ((value >> 44) & 7) as usize;
                    }
                    R_DIM if reg == u64::from(R_DIM) => {
                        selector = (value & 1) as usize;
                        pending = (
                            ((value >> 1) & 7) as usize,
                            Dim {
                                count: range(t, bound, i, half + 68, 16, word)?,
                                stride: range(t, bound, i, half + 84, 32, word)?,
                            },
                        );
                    }
                    R_AXIS if reg == u64::from(R_AXIS) => {
                        walkers[selector].dims[pending.0] = pending.1
                    }
                    R_WINDOW if reg == u64::from(R_WINDOW) => selector = (value & 1) as usize,
                    R_CTRL if reg == u64::from(R_CTRL) && value & (1 << 16) != 0 => {
                        if answer.is_some() && !no_guards {
                            return Err(Refusal::new(
                                "mover summary",
                                "multiple GO streams require exact line checking",
                            ));
                        }
                        let modes = &target.isa.mover.modes;
                        let mode = value & 7;
                        if mode == u64::from(modes.transform) || mode == u64::from(modes.scatter) {
                            return Err(Refusal::new(
                                "mover summary",
                                "transform/scatter requires exact line checking",
                            ));
                        }
                        if ![
                            modes.copy,
                            modes.transpose,
                            modes.gather,
                            modes.generate,
                            modes.fill,
                        ]
                        .iter()
                        .any(|m| u64::from(*m) == mode)
                        {
                            return Err(Refusal::new(
                                "mover summary",
                                "transform/scatter or unknown mode requires exact line checking",
                            ));
                        }
                        let dst = &walkers[1];
                        let (mut lo, mut hi, mut words) =
                            (dst.base as i128, dst.base as i128, 1i128);
                        let mut dimensions = Vec::new();
                        if lo % word != 0 && !no_guards {
                            return Err(Refusal::new("mover summary", "base is not word aligned"));
                        }
                        for dim in &dst.dims[..dst.ndim] {
                            if (dim.count.lo <= 0 || !dim.stride.aligned) && !no_guards {
                                return Err(Refusal::new(
                                    "mover summary",
                                    "positive count/word-stride congruence not proven",
                                ));
                            }
                            words = words.checked_mul(dim.count.hi).unwrap_or(i128::MAX);
                            let distance = (dim.count.hi - 1).max(0);
                            lo += (distance * dim.stride.lo).min(0);
                            hi += (distance * dim.stride.hi).max(0);
                            dimensions.push(WalkBound {
                                count_min: dim.count.lo,
                                count_max: dim.count.hi,
                                stride_min: dim.stride.lo,
                                stride_max: dim.stride.hi,
                            });
                        }
                        if (lo < 0
                            || hi > u64::MAX as i128
                            || words > ktpu_verify::lines::LIMIT as i128)
                            && !no_guards
                        {
                            return Err(Refusal::new(
                                "mover summary",
                                "destination range or line-checker word budget not proven",
                            ));
                        }
                        answer = Some(SingleStream {
                            base: dst.base, dimensions, address_min: lo, address_max: hi, words_max: words,
                            argument: "one non-transform/non-scatter GO; positive counts; affine stride congruences prove word alignment; interval enclosure of all integer walk indices proves nonnegative u64 destinations; count-product bounded by line-checker budget; windows only remove visits; no second partial-line owner exists",
                        });
                    }
                    _ => (),
                }
            }
        }
    }
    Ok(answer)
}
