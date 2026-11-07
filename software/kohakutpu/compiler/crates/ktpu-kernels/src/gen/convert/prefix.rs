//! Checked pitched FP16 prefix boundaries for typed row chunks.
//!
//! Copies and initialization share the logical-row emitter's schedule. Origins
//! and pitches are elements at the interface and bytes only in mover walks.

use super::*;
use crate::split::{PlannedKernel, SplitAxis, SplitOption, SplitRequest};
use crate::trace::fail;
use crate::trace::{Buf, Trace};

#[derive(Clone, Copy)]
pub struct Prefix {
    pub src_rows: i64,
    pub dst_rows: i64,
    pub src_pitch: i64,
    pub dst_pitch: i64,
    pub src_first: i64,
    pub dst_first: i64,
    pub width: i64,
    pub rows: i64,
    pub copied_rows: i64,
    pub classed: bool,
    pub initialize: bool,
}

impl Prefix {
    pub fn read(a: &serde_json::Value, m: &Machine) -> R<Self> {
        let prefix = Self::parse(a)?;
        prefix.validate(m)?;
        Ok(prefix)
    }

    pub(crate) fn parse(a: &serde_json::Value) -> R<Self> {
        let integer = |key: &str, default: Option<i64>| {
            a.get(key).map_or_else(
                || {
                    default.ok_or_else(|| {
                        ktpu_machine::Refusal::new("API.OP.001", format!("prefix integer {key}"))
                    })
                },
                |v| {
                    v.as_i64().ok_or_else(|| {
                        ktpu_machine::Refusal::new("API.OP.001", format!("prefix integer {key}"))
                    })
                },
            )
        };
        let boolean = |key: &str, default| {
            a.get(key).map_or(Ok(default), |v| {
                v.as_bool().ok_or_else(|| {
                    ktpu_machine::Refusal::new("API.OP.001", format!("prefix boolean {key}"))
                })
            })
        };
        let width = integer("width", None)?;
        let rows = integer("rows", None)?;
        let copied_rows = integer("copied_rows", Some(rows))?;
        let prefix = Self {
            src_rows: integer("src_rows", None)?,
            dst_rows: integer("dst_rows", None)?,
            src_pitch: integer("src_pitch", Some(width))?,
            dst_pitch: integer("dst_pitch", Some(width))?,
            src_first: integer("src_first", Some(0))?,
            dst_first: integer("dst_first", Some(0))?,
            width,
            rows,
            copied_rows,
            classed: boolean("classed", false)?,
            initialize: boolean("initialize", copied_rows < rows)?,
        };
        Ok(prefix)
    }

    pub fn validate(&self, m: &Machine) -> R<()> {
        let word = Hw::of(m).word_bytes;
        let whole = |n: i64| n > 0 && n.checked_mul(2).is_some_and(|n| n % word == 0);
        let extent = |pitch: i64, rows: i64| {
            rows > 0
                && pitch
                    .checked_mul(rows)
                    .and_then(|n| n.checked_mul(2))
                    .is_some()
        };
        if !whole(self.width)
            || !whole(self.src_pitch)
            || !whole(self.dst_pitch)
            || self.width > self.src_pitch
            || self.width > self.dst_pitch
            || !extent(self.src_pitch, self.src_rows)
            || !extent(self.dst_pitch, self.dst_rows)
            || self.src_first < 0
            || self.dst_first < 0
            || self.rows <= 0
            || self.copied_rows < 0
            || self.copied_rows > self.rows
            || self
                .src_first
                .checked_add(self.copied_rows)
                .is_none_or(|n| n > self.src_rows)
            || self
                .dst_first
                .checked_add(self.rows)
                .is_none_or(|n| n > self.dst_rows)
            || !self.initialize && self.copied_rows != self.rows
        {
            return fail(
                "API.OP.001",
                "prefix requires bounded whole-word FP16 row intervals",
            );
        }
        Ok(())
    }

    fn ports(&self) -> R<Ports> {
        let src = [self.src_rows, self.src_pitch];
        let dst = [self.dst_rows, self.dst_pitch];
        Ok((
            vec![
                Port::input("src", &src, g::Dtype::F16),
                Port::output("dst", &dst, g::Dtype::F16),
            ],
            vec![("src".into(), f16(&src)?), ("dst".into(), f16(&dst)?)],
        ))
    }

    fn emit(&self, first: i64, count: i64, m: &Machine) -> R<Kernel> {
        let hw = Hw::of(m);
        let (ports, given) = self.ports()?;
        let key = name(
            "row_prefix",
            vec![
                Py::ints(&[
                    self.src_rows,
                    self.dst_rows,
                    self.src_pitch,
                    self.dst_pitch,
                    self.src_first,
                    self.dst_first,
                    self.width,
                    self.rows,
                    self.copied_rows,
                    first,
                    count,
                ]),
                Py::Bool(self.classed),
                Py::Bool(self.initialize),
            ],
        );
        build(&key, m, ports, given, |t, b| {
            self.emit_into(t, &b[0], &b[1], first, count, &hw)
        })
    }

    /// Emit an already validated boundary on the caller's named allocations.
    pub(crate) fn emit_into(
        &self,
        t: &mut Trace,
        src: &Buf,
        dst: &Buf,
        first: i64,
        count: i64,
        hw: &Hw,
    ) -> R<()> {
        let end = first + count;
        if self.initialize {
            let start = if self.classed {
                first
            } else {
                first.max(self.copied_rows)
            };
            if start < end {
                let start = self.dst_first + start;
                let stop = self.dst_first + end;
                let dst = if start == 0 && stop == self.dst_rows && self.width == self.dst_pitch {
                    dst.whole()
                } else {
                    let columns = if self.width == self.dst_pitch {
                        ALL
                    } else {
                        sl(0, self.width)
                    };
                    dst.at(&[sl(start, stop), columns])?
                };
                t.fill_value(&dst, 0, None, None)?;
            }
        }
        let copied = end.min(self.copied_rows) - first;
        if copied > 0 {
            let walk = |pitch| {
                if pitch == self.width {
                    vec![(copied * self.width * 2 / hw.word_bytes, hw.word_bytes)]
                } else {
                    vec![
                        (copied, pitch * 2),
                        (self.width * 2 / hw.word_bytes, hw.word_bytes),
                    ]
                }
            };
            let src_walk = walk(self.src_pitch);
            let dst_walk = walk(self.dst_pitch);
            let src_at = (self.src_first + first) * self.src_pitch * 2;
            let dst_at = (self.dst_first + first) * self.dst_pitch * 2;
            let source = (&src_walk[..], (src_at != 0).then_some(src_at));
            let destination = (&dst_walk[..], (dst_at != 0).then_some(dst_at));
            if self.classed {
                t.copy_classed(&dst.whole(), &src.whole(), source, destination, Some(1))?;
            } else {
                t.copy(&dst.whole(), &src.whole(), Some(source), Some(destination))?;
            }
        }
        Ok(())
    }

    pub fn kernel(&self, m: &Machine) -> R<Kernel> {
        self.validate(m)?;
        self.emit(0, self.rows, m)
    }

    /// Initialization may not destroy source rows before they are copied.
    /// Matching named allocations have identical pitches and extents.
    pub(crate) fn initializes_source(&self) -> bool {
        if !self.initialize || self.copied_rows == 0 {
            return false;
        }
        let first = self.dst_first + if self.classed { 0 } else { self.copied_rows };
        first < self.dst_first + self.rows
            && first < self.src_first + self.copied_rows
            && self.src_first < self.dst_first + self.rows
    }

    /// Whether one allocation can be partitioned without cross-owner hazards.
    /// Extents have already been checked by the boundary constructor.
    pub(crate) fn independent_alias(&self) -> bool {
        self.copied_rows == 0
            || self.src_pitch == self.dst_pitch && self.src_first == self.dst_first
            || (self.src_first + self.copied_rows - 1) * self.src_pitch + self.width
                <= self.dst_first * self.dst_pitch
            || (self.dst_first + self.rows - 1) * self.dst_pitch + self.width
                <= self.src_first * self.src_pitch
    }

    pub(crate) fn options(&self, request: &SplitRequest, m: &Machine) -> R<Vec<SplitOption>> {
        self.validate(m)?;
        let shape = vector::split::Shape {
            rows: self.rows,
            quantum: 1,
            reads: vec![],
            writes: vec![vector::split::Tensor {
                name: "dst".into(),
                operand: vector::Operand {
                    offset: self.dst_first * self.dst_pitch,
                    ..vector::Operand::full(self.dst_pitch, self.dst_rows)
                },
                width: self.width,
                planes: vec![0],
            }],
            tokens: false,
        };
        let mut options = vector::split::split_options(&shape, request, m)?;
        for option in &mut options {
            option.id = "destination-tiles".into();
            option.axis = SplitAxis::DestinationTiles;
            for share in &mut option.shards {
                if share.role == crate::split::ShardRole::Empty {
                    continue;
                }
                let first = share.logical_origin[0];
                let end = (first + share.valid_extent[0]).min(self.copied_rows);
                if first < end {
                    if self.src_pitch == self.width {
                        share.read_regions.push(crate::split::Region {
                            allocation: "src".into(),
                            bytes: ((self.src_first + first) * self.src_pitch * 2) as u64
                                ..((self.src_first + end) * self.src_pitch * 2) as u64,
                        });
                    } else {
                        for row in first..end {
                            let at = (self.src_first + row) * self.src_pitch * 2;
                            share.read_regions.push(crate::split::Region {
                                allocation: "src".into(),
                                bytes: at as u64..(at + self.width * 2) as u64,
                            });
                        }
                    }
                }
                share.local_schedule = "row-prefix".into();
            }
        }
        Ok(options)
    }

    pub fn split(&self, request: &SplitRequest, m: &Machine) -> R<PlannedKernel> {
        vector::split::kernel_options(
            self.options(request, m)?,
            self.kernel(m)?,
            request,
            m,
            |first, count| self.emit(first, count, m),
        )
    }
}

/// Normalize either typed boundary descriptor for shared chain optimizations.
pub(crate) fn recorded(call: &serde_json::Value, m: &Machine) -> R<Option<Prefix>> {
    match call.get("gen").and_then(serde_json::Value::as_str) {
        Some("convert.prefix_kernel") => Prefix::read(&call["args"], m).map(Some),
        Some("convert.rows_kernel") => {
            super::rows::Boundary::read(&call["args"], m).map(|b| Some(b.prefix()))
        }
        _ => Ok(None),
    }
}
