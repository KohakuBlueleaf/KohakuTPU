//! Strided batched plane extraction with optional exact lane-mask transpose.

use super::*;
use crate::split::{PlannedKernel, Region, ShardRole, SplitRequest};
use serde::Deserialize;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Planes {
    #[serde(default)]
    pub scatter: bool,
    pub src: String,
    pub dst: String,
    pub mask: Option<String>,
    pub batches: i64,
    pub groups: i64,
    pub rows: i64,
    pub columns: i64,
    pub padded_rows: i64,
    pub source_offset: i64,
    pub batch_stride: i64,
    pub group_stride: i64,
    pub row_stride: i64,
}

fn extent(values: &[i64]) -> R<i64> {
    values
        .iter()
        .try_fold(1i64, |a, b| a.checked_mul(*b))
        .ok_or_else(|| ktpu_machine::Refusal::new("API.OP.001", "plane extent exceeds i64"))
}

impl Planes {
    fn planes(&self) -> i64 {
        self.batches * self.groups
    }

    fn size(&self) -> i64 {
        self.padded_rows * self.columns
    }

    pub(super) fn source(&self, plane: i64) -> i64 {
        self.source_offset
            + plane / self.groups * self.batch_stride
            + plane % self.groups * self.group_stride
    }

    pub(super) fn validate(&self, p: &Package, m: &Machine) -> R<()> {
        let hw = Hw::of(m);
        if [
            self.batches,
            self.groups,
            self.rows,
            self.columns,
            self.padded_rows,
        ]
        .iter()
        .any(|n| *n <= 0)
            || self.rows > self.padded_rows
            || self.columns % hw.lanes != 0
            || self.mask.is_some() && self.padded_rows % hw.lanes != 0
            || [
                self.source_offset,
                self.batch_stride,
                self.group_stride,
                self.row_stride,
            ]
            .iter()
            .any(|n| *n < 0 || *n % hw.lanes != 0)
        {
            return fail(
                "API.OP.001",
                "planes require positive word-aligned domains and strides",
            );
        }
        let bytes = extent(&[self.batches, self.groups, self.padded_rows, self.columns, 2])?;
        let end = [
            Some(self.source_offset),
            (self.batches - 1).checked_mul(self.batch_stride),
            (self.groups - 1).checked_mul(self.group_stride),
            (self.rows - 1).checked_mul(self.row_stride),
            Some(self.columns),
        ]
        .into_iter()
        .try_fold(0i64, |sum, n| sum.checked_add(n?));
        let Some(end) = end.filter(|end| end.checked_mul(2).is_some()) else {
            return fail("API.OP.001", "plane source extent exceeds i64");
        };
        let lookup = |name: &str| {
            p.ports
                .iter()
                .map(|(buffer, _)| buffer)
                .chain(p.scratch.iter())
                .find(|buffer| buffer.name == name)
        };
        let (Some(source), Some(destination)) = (lookup(&self.src), lookup(&self.dst)) else {
            return fail("API.OP.001", "unknown named plane allocation");
        };
        if p.ports
            .iter()
            .any(|(buffer, written)| buffer.name == self.dst && !written)
        {
            return fail("API.OP.001", "plane destination must be writable");
        }
        if self.src == self.dst || self.scatter && self.mask.is_some() {
            return fail(
                "API.OP.001",
                "plane transfer needs distinct buffers; scatter is not a transpose",
            );
        }
        let expected = if self.mask.is_some() {
            vec![self.planes() * self.columns, self.padded_rows]
        } else {
            vec![self.planes() * self.padded_rows, self.columns]
        };
        let (strided, planar) = if self.scatter {
            (destination, source)
        } else {
            (source, destination)
        };
        if strided.dtype != g::Dtype::F16
            || planar.dtype != g::Dtype::F16
            || extent(&strided.shape)? < end
            || planar.shape != expected
            || extent(&planar.shape)?.checked_mul(2) != Some(bytes)
        {
            return fail(
                "API.OP.001",
                "plane views exceed their named FP16 allocations",
            );
        }
        if let Some(mask) = &self.mask {
            let Some((mask, _)) = p
                .ports
                .iter()
                .find(|(b, written)| &b.name == mask && !written)
            else {
                return fail("API.OP.001", "plane transpose needs a public mask input");
            };
            let swaps = super::super::transpose::masks(m)?.len() as i64 / hw.vlmax;
            if mask.dtype != g::Dtype::F16 || mask.shape != [swaps, hw.vlmax] {
                return fail(
                    "API.OP.001",
                    "plane transpose mask does not match the lane schedule",
                );
            }
        }
        Ok(())
    }

    pub(super) fn emit(
        &self,
        t: &mut Trace,
        env: &Env,
        range: Option<(i64, i64)>,
        hw: &Hw,
    ) -> R<()> {
        let (first, count) = range.unwrap_or((0, self.planes()));
        let src = &env[&self.src];
        let dst = &env[&self.dst];
        if self.scatter {
            // Inverse planar layout: preserve every destination byte outside the
            // selected batch/head rectangle. Do not clear the public output.
            let mut at = first;
            while at < first + count {
                let groups = (self.groups - at % self.groups).min(first + count - at);
                let batches = if at % self.groups == 0 && groups == self.groups {
                    (first + count - at) / groups
                } else {
                    1
                };
                let words = batches * self.rows * groups * self.columns / hw.lanes;
                let walk = [
                    (batches, self.groups * self.size() * 2),
                    (self.rows, self.columns * 2),
                    (groups, self.size() * 2),
                    (self.columns / hw.lanes, hw.word_bytes),
                ];
                let dwalk = [
                    (
                        batches,
                        if batches == 1 {
                            0
                        } else {
                            self.batch_stride * 2
                        },
                    ),
                    (
                        self.rows,
                        if self.rows == 1 {
                            0
                        } else {
                            self.row_stride * 2
                        },
                    ),
                    (
                        groups,
                        if groups == 1 {
                            0
                        } else {
                            self.group_stride * 2
                        },
                    ),
                    (self.columns / hw.lanes, hw.word_bytes),
                ];
                let contiguous = self.group_stride == self.columns
                    && self.row_stride == groups * self.columns
                    && (batches == 1 || self.batch_stride == self.rows * self.row_stride);
                let flat = [(words, hw.word_bytes)];
                t.copy(
                    &dst.whole(),
                    &src.whole(),
                    Some((&walk, Some(at * self.size() * 2))),
                    Some((
                        if contiguous { &flat } else { &dwalk },
                        Some(self.source(at) * 2),
                    )),
                )?;
                at += batches * groups;
            }
            return Ok(());
        }

        let temporary = if self.mask.is_some() {
            Some(t.buffer(
                &[count * self.size() / hw.lanes, hw.lanes],
                g::Dtype::F16,
                vec![],
            )?)
        } else {
            None
        };
        let target = temporary.as_ref().unwrap_or(dst);
        let base = if temporary.is_some() {
            0
        } else {
            first * self.size()
        };
        let target_width = target.spec.shape[1];
        let filled = if base == 0 && count == self.planes() {
            target.whole()
        } else {
            target.at(&[
                sl(
                    base / target_width,
                    (base + count * self.size()) / target_width,
                ),
                ALL,
            ])?
        };
        t.fill_value(&filled, 0, None, None)?;
        let mut at = first;
        while at < first + count {
            let groups = if at % self.groups == 0 && first + count - at >= self.groups {
                self.groups
            } else {
                (self.groups - at % self.groups).min(first + count - at)
            };
            let batches = if groups == self.groups {
                (first + count - at) / groups
            } else {
                1
            };
            // Singleton walk axes never advance; their input stride is irrelevant.
            let stride = |count, elements| if count == 1 { 0 } else { elements * 2 };
            let walk = [
                (batches, stride(batches, self.batch_stride)),
                (groups, stride(groups, self.group_stride)),
                (self.rows, stride(self.rows, self.row_stride)),
                (self.columns / hw.lanes, hw.word_bytes),
            ];
            let dwalk = [
                (batches * groups, self.size() * 2),
                (self.rows * self.columns / hw.lanes, hw.word_bytes),
            ];
            let origin = self.source(at) * 2;
            let destination = (base + (at - first) * self.size()) * 2;
            t.copy(
                &target.whole(),
                &src.whole(),
                Some((&walk, Some(origin))),
                Some((&dwalk, Some(destination))),
            )?;
            at += batches * groups;
        }
        if let Some(mask) = &self.mask {
            super::super::transpose::emit_gt4_window(
                t,
                &target.whole(),
                &env[mask],
                0,
                &dst.whole(),
                count,
                self.padded_rows,
                self.columns,
                None,
                self.columns * 2,
                self.padded_rows * 2,
                None,
                (first != 0).then_some(first * self.size() * 2),
                hw,
            )?;
        }
        Ok(())
    }
}

/// Distinct output planes can be co-partitioned; other valid packages keep
/// their ordinary ordered schedule rather than asserting independent owners.
pub(crate) fn independent(p: &Package) -> bool {
    let mut domain = None;
    let mut outputs = std::collections::HashSet::new();
    !p.steps.is_empty()
        && p.steps.iter().all(|step| {
            let Step::Planes(step) = step else {
                return false;
            };
            if step.scatter
                || p.scratch
                    .iter()
                    .any(|buffer| buffer.name == step.src || buffer.name == step.dst)
            {
                return false;
            }
            let count = step.batches.checked_mul(step.groups);
            if count.is_none() || !outputs.insert(&step.dst) {
                return false;
            }
            if let Some(previous) = domain {
                previous == count
            } else {
                domain = Some(count);
                true
            }
        })
}

/// All members have the same independent plane domain and distinct outputs.
/// One owner performs extraction, padding and optional transpose of each plane.
pub(crate) fn split(
    p: &Package,
    key: &str,
    request: &SplitRequest,
    m: &Machine,
) -> R<PlannedKernel> {
    let signature = kernel(p, key, m)?;
    let mut members = Vec::new();
    for step in &p.steps {
        let Step::Planes(step) = step else {
            return fail("API.OP.001", "plane package needs plane stages");
        };
        members.push(step);
    }
    let Some(first) = members.first() else {
        return fail("API.OP.001", "plane package needs a stage");
    };
    let shape = v::split::Shape {
        rows: first.planes(),
        quantum: 1,
        tokens: false,
        reads: vec![],
        writes: members
            .iter()
            .map(|step| v::split::Tensor {
                name: step.dst.clone(),
                operand: v::Operand::full(step.size(), step.planes()),
                width: step.size(),
                planes: vec![0],
            })
            .collect(),
    };
    let mut options = v::split::split_options(&shape, request, m)?;
    for option in &mut options {
        option.id = "planes".into();
        for shard in &mut option.shards {
            if shard.role == ShardRole::Empty {
                continue;
            }
            let first = shard.logical_origin[0];
            for step in &members {
                for plane in first..first + shard.valid_extent[0] {
                    for row in 0..step.rows {
                        let start = (step.source(plane) + row * step.row_stride) * 2;
                        shard.read_regions.push(Region {
                            allocation: step.src.clone(),
                            bytes: start as u64..(start + step.columns * 2) as u64,
                        });
                    }
                }
                if let Some(mask) = &step.mask {
                    shard.read_regions.push(Region {
                        allocation: mask.clone(),
                        bytes: 0..(super::super::transpose::masks(m)?.len() * 2) as u64,
                    });
                }
            }
        }
    }
    v::split::kernel_options(options, signature, request, m, |first, count| {
        let key = name(
            "plane_range",
            vec![Py::str(key), Py::Int(first), Py::Int(count)],
        );
        kernel_at(p, &key, Some((first, count)), m, true)
    })
}
