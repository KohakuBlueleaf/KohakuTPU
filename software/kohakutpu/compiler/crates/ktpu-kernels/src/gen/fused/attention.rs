//! Materialized planar attention: FP16 scores/statistics, MX7 probabilities.
//! This is a named-buffer fused stage, independent of projection extraction.

use super::*;
use crate::trace::buffer::at;
use crate::trace::vector::Fold;
use g::ValueOp as Op;
use serde::Deserialize;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attention {
    pub q: String,
    pub k: String,
    pub vt: String,
    pub out: String,
    pub tail: Option<String>,
    pub heads: i64,
    pub queries: i64,
    pub keys: i64,
    pub dim: i64,
    pub query_tile: i64,
    pub query_cores: i64,
    pub key_tile: i64,
    pub valid_keys: i64,
    pub block_rows: i64,
    pub blocked: bool,
    /// Sequential groups of heads with a shared score/probability workspace.
    #[serde(default)]
    pub head_group: i64,
    #[serde(default)]
    pub incremental_max: bool,
    #[serde(default)]
    pub sum_columns: i64,
    #[serde(default)]
    pub compact_mask: bool,
    #[serde(default)]
    pub direct_quant: bool,
    #[serde(default)]
    pub score_tile: i64,
    #[serde(default)]
    pub merge: Option<super::planes::Planes>,
    #[serde(default)]
    pub schedule: BTreeMap<String, i64>,
}

fn layout(key: &str, value: i64) -> (String, g::LayoutValue) {
    (key.into(), g::LayoutValue::Int(value))
}

fn tiled(t: &mut Trace, rows: i64, cols: i64, tm: i64, tn: i64) -> R<Buf> {
    t.buffer(
        &[rows, cols],
        g::Dtype::F16,
        vec![("tile".into(), g::LayoutValue::Ints(vec![tm / 4, tn / 4]))],
    )
}

/// A common grouped packed contraction, preserving one drain per complete tile.
#[allow(clippy::too_many_arguments)]
fn sweep(
    t: &mut Trace,
    a: &Buf,
    b: &Buf,
    out: &Buf,
    a0: Idx,
    b0: Idx,
    rows: i64,
    cols: i64,
    k: i64,
    groups: i64,
    tm: i64,
    tn: i64,
    hw: &Hw,
) -> R<()> {
    let ntn = cols / tn;
    let tiles = rows / tm * ntn;
    let units = hw.mg.min(tiles);
    let per = cdiv(tiles, units);
    let kc = if k % 64 == 0 { 64 } else { 32 };
    t.par(units, g::UnitKind::Mg, At::Var, |t, u| {
        t.range(per, |t, i| {
            let tile = u.add(i.mul(units));
            let emit = |t: &mut Trace| {
                let ri = tile.div(ntn).mul(tm);
                let ci = tile.rem(ntn).mul(tn);
                let br = b0
                    .add(tile.div(rows / groups / tm * ntn).mul(cols))
                    .add(ci.clone());
                let ar = a0.add(ri.clone());
                let acc = t.zero(tm, tn)?;
                t.range(k / kc, |t, ki| {
                    let col = ki.mul(kc);
                    let av = t.fill(
                        &a.at(&[sl(ar.clone(), ar.add(tm)), sl(col.clone(), col.add(kc))])?,
                    )?;
                    let bv = t.fill(
                        &b.at(&[sl(br.clone(), br.add(tn)), sl(col.clone(), col.add(kc))])?,
                    )?;
                    t.gemm(av, bv, acc)?;
                    Ok(())
                })?;
                t.drain(
                    acc,
                    &out.at(&[sl(ri.clone(), ri.add(tm)), sl(ci.clone(), ci.add(tn))])?,
                )
            };
            if per * units == tiles {
                emit(t)
            } else {
                t.when(Cond::new(&tile, g::CmpOp::Lt, tiles), emit)
            }
        })
    })
}

fn rows(
    t: &mut Trace,
    count: i64,
    tm: i64,
    hw: &Hw,
    mut emit: impl FnMut(&mut Trace, Idx) -> R<()>,
) -> R<()> {
    let limit = (count / hw.vc).min(tm).max(16);
    let per = (1..=limit / 16)
        .rev()
        .find(|n| count % (n * 16) == 0 && tm % (n * 16) == 0)
        .unwrap_or(1)
        * 16;
    if per <= 0 || per % 16 != 0 || count % per != 0 || tm % per != 0 {
        return fail(
            "API.OP.001",
            "attention rows must form complete vector bands",
        );
    }
    let instances = count / per;
    t.par_local(
        instances,
        g::UnitKind::Vc,
        if instances > hw.vc { At::Any } else { At::Var },
        |t, u| t.range(per / 16, |t, i| emit(t, u.mul(per).add(i.mul(16)))),
    )
}

/// Prepare a broadcast mask once per VC instance, outside its row-trip loop.
fn masked_rows(
    t: &mut Trace,
    count: i64,
    tm: i64,
    hw: &Hw,
    mask: Option<&Buf>,
    mut emit: impl FnMut(&mut Trace, Idx, Option<V>) -> R<()>,
) -> R<()> {
    let Some(mask) = mask else {
        return rows(t, count, tm, hw, |t, row| emit(t, row, None));
    };
    let limit = (count / hw.vc).min(tm).max(16);
    let per = (1..=limit / 16)
        .rev()
        .find(|n| count % (n * 16) == 0 && tm % (n * 16) == 0)
        .unwrap_or(1)
        * 16;
    if per <= 0 || per % 16 != 0 || count % per != 0 || tm % per != 0 {
        return fail(
            "API.OP.001",
            "attention rows must form complete vector bands",
        );
    }
    let instances = count / per;
    t.par_local(
        instances,
        g::UnitKind::Vc,
        if instances > hw.vc { At::Any } else { At::Var },
        |t, u| {
            let loaded = t.load(&mask.whole(), false)?;
            let loaded = t.unpack(loaded, None)?;
            t.range(per / 16, |t, i| {
                emit(t, u.mul(per).add(i.mul(16)), Some(loaded))
            })
        },
    )
}

/// Immutable compact tail masks, scoped to one fused package construction.
pub(super) type MaskCache = BTreeMap<(i64, i64, i64, Option<String>), Buf>;

impl Attention {
    pub(super) fn validate(&self, env: &Env, m: &Machine) -> R<()> {
        let hw = Hw::of(m);
        if self.schedule.iter().any(|(key, value)| {
            !QUANT_SCHEDULE
                .iter()
                .any(|(known, _)| *known == key.as_str())
                || if key == "finite_batch" {
                    *value <= 0
                } else {
                    ![0, 1].contains(value)
                }
        }) {
            return fail("API.OP.001", "invalid attention quantization schedule");
        }
        let (h, q, k, d, tm, tn) = (
            self.heads,
            self.queries,
            self.keys,
            self.dim,
            self.query_tile,
            self.key_tile,
        );
        if [h, q, k, d, tm, tn, self.block_rows, self.query_cores]
            .iter()
            .any(|n| *n <= 0)
            || tm % 16 != 0
            || tm > 256
            || tn % 64 != 0
            || tn > 256
            || d % 32 != 0
            || d > 256
            || q % 16 != 0
            || k % tn != 0
            || self.valid_keys <= 0
            || self.valid_keys > k
        {
            return fail(
                "API.OP.001",
                "invalid materialized attention domain or tile",
            );
        }
        if self.head_group < 0
            || self.head_group > 0 && (h % self.head_group != 0 || self.blocked)
            || self.sum_columns < 0
            || self.sum_columns > 0 && (tn % self.sum_columns != 0 || self.sum_columns % 32 != 0)
            || self.score_tile < 0
            || self.score_tile > 0
                && (k % self.score_tile != 0 || self.score_tile % tn != 0 || self.score_tile > 256)
            || self.direct_quant && (q % tm != 0 || self.query_cores != 1)
        {
            return fail(
                "API.OP.001",
                "invalid grouped attention materialization policy",
            );
        }
        let Some(quantum) = self.query_cores.checked_mul(tm) else {
            return fail("API.OP.001", "attention query quantum overflow");
        };
        let Some(padded) = (q / quantum + i64::from(q % quantum != 0)).checked_mul(quantum) else {
            return fail("API.OP.001", "attention query capacity overflow");
        };
        if self.blocked && (self.block_rows % tm != 0 || padded % self.block_rows != 0) {
            return fail(
                "API.OP.001",
                "attention blocks must exactly cover query capacity",
            );
        }
        if h.checked_mul(padded)
            .and_then(|rows| rows.checked_mul(k.max(d).max(128)))
            .and_then(|values| values.checked_mul(8))
            .is_none()
        {
            return fail("API.OP.001", "attention scratch extent overflow");
        }
        for (name, shape) in [
            (&self.q, [h.checked_mul(q), Some(d)]),
            (&self.k, [h.checked_mul(k), Some(d)]),
            (&self.vt, [h.checked_mul(d), Some(k)]),
            (&self.out, [h.checked_mul(q), Some(d)]),
        ] {
            let Some(buf) = env.get(name) else {
                return fail("API.OP.001", "unknown attention buffer");
            };
            let (Some(r), Some(c)) = (shape[0], shape[1]) else {
                return fail("API.OP.001", "attention extent overflow");
            };
            if r.checked_mul(c).and_then(|n| n.checked_mul(2)).is_none()
                || buf.spec.dtype != g::Dtype::F16
                || buf.spec.shape != [r, c]
            {
                return fail(
                    "API.OP.001",
                    "attention buffer does not match its FP16 domain",
                );
            }
        }
        if let Some(merge) = &self.merge {
            if self.blocked
                || padded != q
                || !merge.scatter
                || merge.src != self.out
                || merge.batches.checked_mul(merge.groups) != Some(h)
                || merge.padded_rows != q
                || merge.columns != d
                || [&self.q, &self.k, &self.vt].contains(&&merge.dst)
            {
                return fail(
                    "API.OP.001",
                    "streamed attention merge must cover completed head planes",
                );
            }
        }
        if self.out == self.k || self.out == self.vt {
            return fail("API.OP.001", "attention output may reuse Q, not K or V");
        }
        if self.valid_keys % hw.lanes != 0 {
            let Some(tail) = self.tail.as_ref().and_then(|name| env.get(name)) else {
                return fail("API.OP.001", "attention needs the caller tail mask");
            };
            if tail.spec.dtype != g::Dtype::F16 || tail.spec.shape != [hw.lanes] {
                return fail("API.OP.001", "attention tail must be one FP16 word");
            }
        }
        Ok(())
    }

    pub(super) fn emit(
        &self,
        t: &mut Trace,
        env: &Env,
        m: &Machine,
        masks: &mut MaskCache,
    ) -> R<()> {
        self.validate(env, m)?;
        let hw = Hw::of(m);
        let (h, q, k, d, tm, tn) = (
            self.heads,
            self.queries,
            self.keys,
            self.dim,
            self.query_tile,
            self.key_tile,
        );
        let logical = q;
        let q = cdiv(q, self.query_cores * tm) * self.query_cores * tm;
        let result = if logical == q {
            env[&self.out].clone()
        } else {
            t.buffer(&[h * q, d], g::Dtype::F16, vec![])?
        };
        let qp = t.buffer(
            &[h * q, d],
            g::Dtype::Mx7a,
            vec![layout("gm", tm / 4), layout("nk", 2)],
        )?;
        let score_tile = if self.score_tile == 0 {
            tn
        } else {
            self.score_tile
        };
        let kp = t.buffer(
            &[h * k, d],
            g::Dtype::Mx7b,
            vec![layout("gn", score_tile / 4), layout("nk", 2)],
        )?;
        let vp = t.buffer(
            &[h * d, k],
            g::Dtype::Mx7b,
            vec![layout("gn", d / 4), layout("nk", 2)],
        )?;
        let schedule: Vec<_> = QUANT_SCHEDULE
            .iter()
            .map(|(key, default)| (*key, self.schedule.get(*key).copied().unwrap_or(*default)))
            .collect();
        if self.direct_quant {
            t.quant(&qp.whole(), &env[&self.q].whole())?;
            t.quant(&kp.whole(), &env[&self.k].whole())?;
            for head in 0..h {
                let axes = [sl(head * d, (head + 1) * d), ALL];
                t.quant(&vp.at(&axes)?, &env[&self.vt].at(&axes)?)?;
            }
        } else {
            for (source, packed, r, rp, c) in [
                (&env[&self.q], &qp, logical, q, d),
                (&env[&self.k], &kp, k, k, d),
                (&env[&self.vt], &vp, d, d, k),
            ] {
                super::super::matmul::quant_into(
                    t,
                    source,
                    packed,
                    &v::Operand::full(c, h * r),
                    r,
                    c,
                    rp,
                    c,
                    2,
                    h,
                    m,
                    false,
                    &schedule,
                    false,
                    false,
                )?;
            }
        }
        let block = if self.blocked { self.block_rows } else { q };
        let groups = if self.blocked {
            1
        } else if self.head_group > 0 {
            self.head_group
        } else {
            h
        };
        let r = groups * block;
        let nb = k / tn;
        let mask_key = (k, tn, self.valid_keys, self.tail.clone());
        let cached = if self.compact_mask {
            masks.get(&mask_key).cloned()
        } else {
            None
        };
        let valid = if let Some(valid) = cached {
            valid
        } else {
            let mask = t.buffer(&[1, tn], g::Dtype::F16, vec![])?;
            t.fill_value(&mask.whole(), 0, None, None)?;
            let btail = (self.valid_keys / tn).min(nb - 1);
            let rem = (self.valid_keys - btail * tn).min(tn);
            let full = rem / hw.lanes * hw.lanes;
            if full > 0 {
                t.fill_value(&mask.at(&[ALL, sl(0, full)])?, 0x3c00, Some(16), None)?;
            }
            if self.valid_keys % hw.lanes != 0 {
                let word = [(1, hw.word_bytes)];
                t.copy(
                    &mask.whole(),
                    &env[self.tail.as_ref().unwrap()].whole(),
                    Some((&word, None)),
                    Some((&word, Some(full * 2))),
                )?;
            }
            let mask_rows = if self.compact_mask { 4 } else { r };
            let mask_tile = if self.compact_mask { 4 } else { tm };
            let valid = tiled(t, mask_rows, tn, mask_tile, tn)?;
            t.gt4(
                &valid.whole(),
                &mask.whole(),
                &[
                    (mask_rows / mask_tile, 0),
                    (mask_tile / 4, 0),
                    (tn / 16, 32),
                    (4, 0),
                ],
                &[
                    (mask_rows / mask_tile, mask_tile * tn * 2),
                    (mask_tile / 4, tn * 8),
                    (tn / 16, 128),
                ],
            )?;
            if self.compact_mask {
                masks.insert(mask_key, valid.clone());
            }
            valid
        };
        let scores = tiled(t, r, k, tm, score_tile)?;
        let probs = t.buffer(
            &[r, k],
            g::Dtype::Mx7a,
            vec![layout("gm", tm / 4), layout("nk", 2)],
        )?;
        let maxima = if self.incremental_max {
            None
        } else {
            Some(t.buffer(&[nb, r], g::Dtype::F16, vec![])?)
        };
        let sum_width = if self.sum_columns == 0 {
            tn
        } else {
            self.sum_columns
        };
        let sum_parts = k / sum_width;
        let sums = t.buffer(&[sum_parts, r], g::Dtype::F16, vec![])?;
        let rowmax = t.buffer(&[r], g::Dtype::F16, vec![])?;
        let rowsum = t.buffer(&[r], g::Dtype::F16, vec![])?;
        let product = tiled(t, r, d, tm, d)?;
        let scale = (d as f64).powf(-0.5) * std::f64::consts::LOG2_E;
        let compute = |t: &mut Trace, head: Idx, qi: Idx| {
            let a0 = head.mul(q).add(qi.mul(block));
            sweep(
                t,
                &qp,
                &kp,
                &scores,
                a0.clone(),
                head.mul(k),
                r,
                k,
                d,
                groups,
                tm,
                score_tile,
                &hw,
            )?;
            let needs_mask = |b: i64| {
                (b + 1) * tn > self.valid_keys
                    || self.valid_keys >= k && (!self.incremental_max || b == nb - 1)
            };
            let scaled = |t: &mut Trace, row: Idx, b: i64, prepared_mask: Option<V>| -> R<V> {
                let raw = t.load(
                    &scores.at(&[sl(row.clone(), row.add(16)), sl(b * tn, (b + 1) * tn)])?,
                    false,
                )?;
                let x = t.math(Op::Mul, &[raw.into(), scale.into()])?;
                if b * tn >= self.valid_keys {
                    // A completely padded tile must contribute no probability.
                    t.math(Op::Sel, &[0.0.into(), x.into(), f64::NEG_INFINITY.into()])
                } else if needs_mask(b) {
                    let mask = if let Some(mask) = prepared_mask {
                        mask
                    } else {
                        t.load(
                            &valid.at(&[sl(row.clone(), row.add(16)), sl(0, tn)])?,
                            false,
                        )?
                    };
                    t.math(Op::Sel, &[mask.into(), x.into(), f64::NEG_INFINITY.into()])
                } else {
                    Ok(x)
                }
            };
            if self.incremental_max {
                for b in 0..nb {
                    let mask = (self.compact_mask && needs_mask(b)).then_some(&valid);
                    masked_rows(t, r, tm, &hw, mask, |t, row, prepared| {
                        let x = scaled(t, row.clone(), b, prepared)?;
                        let value = t.reduce(Op::Rmax, x.into())?;
                        let dst = rowmax.at(&[sl(row.clone(), row.add(16))])?;
                        let value = if b == 0 {
                            value
                        } else {
                            let previous = t.load(&dst, false)?;
                            t.math(Op::Max, &[previous.into(), value.into()])?
                        };
                        t.store(&dst, value.into())
                    })?;
                }
            } else {
                for b in 0..nb {
                    let mask = (self.compact_mask && needs_mask(b)).then_some(&valid);
                    masked_rows(t, r, tm, &hw, mask, |t, row, prepared| {
                        let x = scaled(t, row.clone(), b, prepared)?;
                        let value = t.reduce_over(Op::Rmax, x.into(), Fold::default())?;
                        t.store(
                            &maxima
                                .as_ref()
                                .unwrap()
                                .at(&[at(b), sl(row.clone(), row.add(16))])?,
                            value.into(),
                        )
                    })?;
                }
                rows(t, r, tm, &hw, |t, row| {
                    let x = t.load(
                        &maxima
                            .as_ref()
                            .unwrap()
                            .at(&[at(0), sl(row.clone(), row.add(16))])?,
                        false,
                    )?;
                    let mut acc = t.unpack(x, None)?;
                    for b in 1..nb {
                        let x = t.load(
                            &maxima
                                .as_ref()
                                .unwrap()
                                .at(&[at(b), sl(row.clone(), row.add(16))])?,
                            false,
                        )?;
                        acc = t.math(Op::Max, &[acc.into(), x.into()])?;
                    }
                    t.store(&rowmax.at(&[sl(row.clone(), row.add(16))])?, acc.into())
                })?;
            }
            for b in 0..nb {
                let mask = (self.compact_mask && needs_mask(b)).then_some(&valid);
                masked_rows(t, r, tm, &hw, mask, |t, row, prepared| {
                    let x = scaled(t, row.clone(), b, prepared)?;
                    let max = t.load(&rowmax.at(&[sl(row.clone(), row.add(16))])?, false)?;
                    let diff = t.math(Op::Sub, &[x.into(), max.into()])?;
                    let prob = t.math(Op::Exp2, &[diff.into()])?;
                    t.store(
                        &probs.at(&[sl(row.clone(), row.add(16)), sl(b * tn, (b + 1) * tn)])?,
                        prob.into(),
                    )?;
                    for part in 0..tn / sum_width {
                        let sum = if sum_width == tn {
                            t.reduce(Op::Rsum, prob.into())?
                        } else {
                            t.reduce_columns(Op::Rsum, prob, part * sum_width, sum_width)?
                        };
                        t.store(
                            &sums.at(&[
                                at(b * (tn / sum_width) + part),
                                sl(row.clone(), row.add(16)),
                            ])?,
                            sum.into(),
                        )?;
                    }
                    Ok(())
                })?;
            }
            if self.incremental_max {
                sweep(
                    t,
                    &probs,
                    &vp,
                    &product,
                    0.into(),
                    head.mul(d),
                    r,
                    d,
                    k,
                    groups,
                    tm,
                    d,
                    &hw,
                )?;
            }
            rows(t, r, tm, &hw, |t, row| {
                let x = t.load(&sums.at(&[at(0), sl(row.clone(), row.add(16))])?, false)?;
                let mut acc = t.unpack(x, None)?;
                for b in 1..sum_parts {
                    let x = t.load(&sums.at(&[at(b), sl(row.clone(), row.add(16))])?, false)?;
                    acc = t.math(Op::Add, &[acc.into(), x.into()])?;
                }
                t.store(&rowsum.at(&[sl(row.clone(), row.add(16))])?, acc.into())
            })?;
            if !self.incremental_max {
                sweep(
                    t,
                    &probs,
                    &vp,
                    &product,
                    0.into(),
                    head.mul(d),
                    r,
                    d,
                    k,
                    groups,
                    tm,
                    d,
                    &hw,
                )?;
            }
            rows(t, r, tm, &hw, |t, row| {
                let value = t.load(&product.at(&[sl(row.clone(), row.add(16)), ALL])?, false)?;
                let sum = t.load(&rowsum.at(&[sl(row.clone(), row.add(16))])?, false)?;
                let inverse = t.math(Op::Inv, &[sum.into()])?;
                let out = t.math(Op::Mul, &[value.into(), inverse.into()])?;
                let target = a0.add(row);
                t.store(
                    &result.at(&[sl(target.clone(), target.add(16)), ALL])?,
                    out.into(),
                )
            })
        };
        if self.blocked {
            t.range(h, |t, head| {
                t.range(q / block, |t, qi| compute(t, head.clone(), qi))
            })?;
        } else {
            for first in (0..h).step_by(groups as usize) {
                compute(t, first.into(), 0.into())?;
                if let Some(merge) = &self.merge {
                    merge.emit(t, env, Some((first, groups)), &hw)?;
                }
            }
        }
        if logical != q {
            let walk = [(h, q * d * 2), (logical * d / hw.lanes, hw.word_bytes)];
            let dwalk = [
                (h, logical * d * 2),
                (logical * d / hw.lanes, hw.word_bytes),
            ];
            t.copy_classed(
                &env[&self.out].whole(),
                &result.whole(),
                (&walk, None),
                (&dwalk, None),
                Some(1),
            )?;
        }
        Ok(())
    }
}
