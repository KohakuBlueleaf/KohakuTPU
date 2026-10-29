//! MX7 batch images and a device-side finite-exponent classifier.

use ktpu_isa::numerics::e8m15;
use ktpu_l1::layout::tensor::zero_lift_value;

use crate::mutant::on;

/// The S register a lifting batch image holds `tensor::ZERO_LIFT` in: every
/// entry's zeros are selected to it before VPACK, so an all-zero block packs
/// E=0.
const LIFT: &str = "s4";

fn lift_seti() -> String {
    format!("    seti {LIFT} = {}", e8m15(zero_lift_value()))
}

pub fn clear(name: &str) -> String {
    format!("image {name}()\n    vsync fe on=math\n    getstk s14\n    vmov v31, k0\n    vsync fe on=math\n    halt")
}

pub fn status(name: &str) -> String {
    format!("image {name}(status)\n    desc a2 = status walk=(32, 4)\n    vpack v31 -> l1[384] f16 n=4\n    vsync fe on=pack\n    vdrain a2 <- l1[384]\n    vsync fe on=drain\n    halt")
}

pub struct Batch<'a> {
    pub name: &'a str,
    pub n: i64,
    pub source_stride: i64,
    pub stride: i64,
    pub incremental: bool,
    pub dtype: &'a str,
    pub slots: i64,
    pub checked: bool,
    pub split_rows: bool,
    pub finite_batch: i64,
    pub valid_rows: i64,
    pub row_words: i64,
    pub overlap: bool,
    pub classifier_overlap: bool,
    pub pipeline: bool,
    pub zero_classifier: bool,
    pub fill_overlap: bool,
    pub zero_reuse: bool,
    pub classifier_banks: bool,
    pub steady: Option<bool>,
    pub odd: bool,
    /// The destination pairs K blocks (`nk` >= 2): zeros pack as `ZERO_LIFT`.
    pub lift: bool,
}

impl Batch<'_> {
    fn compute(&self, slot: i64, ind: &str, lines: &mut Vec<String>) {
        let input = if on("l2.packing/alias-slots") {
            0
        } else {
            slot * 192
        };
        let output = input + 128;
        let v = slot * 4;
        if self.overlap && !on("l2.packing/overlap-no-reuse-wait") {
            lines.push(format!("{ind}vsync pack on=drain slack={}", self.slots - 1));
        }
        // One architectural loop register: the bounded 16-entry body is unrolled.
        if self.pipeline
            || self.zero_classifier
            || (self.finite_batch > 1 && (self.checked || !on("l2.packing/no-finite-batch")))
        {
            for first in (0..self.n).step_by(self.finite_batch as usize) {
                self.classified(slot, first, input, output, ind, lines);
            }
        } else {
            for j in 0..self.n {
                self.entry(
                    v,
                    input + j * self.input_words(),
                    output + j * 4,
                    false,
                    ind,
                    lines,
                );
            }
        }
        if self.pipeline {
            // Input reuse waits for reads; only the drain queue waits for output.
            lines.push(format!("{ind}vsync fe on=unpack"));
            lines.push(format!("{ind}vsync drain on=pack"));
        }
        lines.push(format!("{ind}vdrain a1 <- l1[{output}] rel"));
        if !self.overlap && !on("l2.packing/no-drain-wait") {
            lines.push(format!("{ind}vsync fe on=drain"));
        }
    }

    fn input_words(&self) -> i64 {
        self.valid_rows * self.row_words
    }

    fn lift(&self, v: i64, ind: &str, lines: &mut Vec<String>) {
        if self.lift {
            lines.push(format!("{ind}vsel v{v}, v{v}, {LIFT}, v{v}"));
        }
    }

    fn unpack(&self, v: i64, input: i64, ind: &str, lines: &mut Vec<String>) {
        if self.valid_rows != 4 || self.row_words != 2 {
            let zero = if on("l2.packing/pad-nonzero") {
                "k1"
            } else {
                "k0"
            };
            lines.push(format!("{ind}vmov v{v}, {zero}"));
            lines.push(format!("{ind}vsync fe on=math"));
            for row in 0..self.valid_rows {
                lines.push(format!(
                    "{ind}vunpk v{v}[{}:] <- l1[{}] f16 n={}",
                    2 * row,
                    input + row * self.row_words,
                    self.row_words
                ));
            }
        } else {
            lines.push(format!("{ind}vunpk v{v} <- l1[{input}] f16"));
        }
    }

    fn classified(
        &self,
        slot: i64,
        first: i64,
        input: i64,
        output: i64,
        ind: &str,
        lines: &mut Vec<String>,
    ) {
        let n = self.finite_batch.min(self.n - first);
        // Three registers per zero-classified value; two disjoint batches stay
        // below reserved sticky-status v31. Unpack can lead the previous pack.
        let step = if self.classifier_banks { 3 } else { 4 };
        let base = if self.classifier_banks {
            12 * ((slot * ((self.n + self.finite_batch - 1) / self.finite_batch)
                + first / self.finite_batch)
                % 2)
        } else {
            0
        };
        for j in 0..n {
            self.unpack(
                base + step * j,
                input + (first + j) * self.input_words(),
                ind,
                lines,
            );
        }
        if !self.pipeline {
            lines.push(format!("{ind}vsync fe on=unpack"));
        }
        for j in 0..n {
            self.lift(base + step * j, ind, lines);
        }
        if self.checked {
            self.classify(n, base, step, ind, lines);
        }
        for j in 0..n {
            lines.push(format!(
                "{ind}vpack v{} -> l1[{}] {}",
                base + step * j,
                output + (first + j) * 4,
                self.dtype
            ));
        }
        if !self.pipeline && !on("l2.packing/batch-no-pack-wait") {
            lines.push(format!("{ind}vsync fe on=pack"));
        }
    }

    fn classify(&self, n: i64, base: i64, step: i64, ind: &str, lines: &mut Vec<String>) {
        if self.zero_classifier {
            // IEEE special handling precedes the zero-factor shortcut: finite*0
            // is zero; Inf*0 and NaN*0 are NaN. EXP2D maps zero to 1 (<2^tau)
            // and any NaN to sticky. vector_quant admits checked tau=8 only.
            for j in 0..n {
                lines.push(format!(
                    "{ind}vmul v{}, v{}, k0",
                    base + step * j + 1,
                    base + step * j
                ));
            }
            for j in 0..n {
                lines.push(format!(
                    "{ind}vexp2d v{}, v{}, k0",
                    base + step * j + step - 1,
                    base + step * j + 1
                ));
            }
            if !self.pipeline {
                lines.push(format!("{ind}vsync fe on=math"));
            }
            return;
        }
        let overlap =
            self.pipeline || (self.classifier_overlap && !on("l2.packing/serial-classifier"));
        for j in 0..n {
            lines.push(format!("{ind}vabs v{}, v{}", 4 * j + 1, 4 * j));
            lines.push(format!("{ind}vmov v{}, s3", 4 * j + 2));
        }
        // Register RAWs are scoreboarded; predicates still need a completed compare.
        if !overlap {
            lines.push(format!("{ind}vsync fe on=math"));
        }
        for j in 0..n {
            lines.push(format!("{ind}vcmplt p{j}, v{}, s2", 4 * j + 1));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!("{ind}vmov v{}, k0 if=p{j}", 4 * j + 2));
        }
        if !overlap {
            lines.push(format!("{ind}vsync fe on=math"));
        }
        for j in 0..n {
            lines.push(format!("{ind}vexp2d v{}, v{}, k0", 4 * j + 3, 4 * j + 2));
        }
        if !self.pipeline {
            lines.push(format!("{ind}vsync fe on=math"));
        }
    }

    fn entry(
        &self,
        v: i64,
        input: i64,
        output: i64,
        rel: bool,
        ind: &str,
        lines: &mut Vec<String>,
    ) {
        let rel = if rel { " rel" } else { "" };
        if self.valid_rows != 4 || self.row_words != 2 {
            self.unpack(v, input, ind, lines);
        } else if on("l2.packing/wrong-order") && rel.is_empty() {
            for c in 0..8 {
                lines.push(format!(
                    "{ind}vunpk v{v}[{c}:] <- l1[{}] f16 n=1",
                    input + (c ^ 1)
                ));
            }
        } else {
            lines.push(format!("{ind}vunpk v{v} <- l1[{input}] f16{rel}"));
        }
        lines.push(format!("{ind}vsync fe on=unpack"));
        self.lift(v, ind, lines);
        if self.checked && !on("l2.packing/drop-finite-check") {
            // Exact equivalence to raw FP16 exponent != 31, including sNaNs.
            // Predicate masks RF writes, not EXP2D sticky: materialize first.
            for line in [
                format!("vabs v{}, v{v}", v + 1),
                "vsync fe on=math".into(),
                format!("vcmplt p0, v{}, s2", v + 1),
                "vsync fe on=math".into(),
                format!("vmov v{}, s3", v + 2),
                "vsync fe on=math".into(),
                format!("vmov v{}, k0 if=p0", v + 2),
                "vsync fe on=math".into(),
                format!("vexp2d v{}, v{}, k0", v + 3, v + 2),
                "vsync fe on=math".into(),
            ] {
                lines.push(format!("{ind}{line}"));
            }
        }
        if on("l2.packing/wrong-scale") {
            lines.push(format!("{ind}vadd v{v}, v{v}, v{v}"));
            lines.push(format!("{ind}vsync fe on=math"));
        }
        lines.push(format!(
            "{ind}vpack v{v} -> l1[{output}] {}{rel}",
            self.dtype
        ));
        lines.push(format!("{ind}vsync fe on=pack"));
    }

    pub fn emit(&self) -> String {
        if self.zero_reuse && self.valid_rows == 0 {
            // Packed zero entries are immutable for the image lifetime. Prepare
            // one slot once, then only advance the destination descriptor.
            let mut lines = vec![format!("image {}(trips)", self.name)];
            lines.push(format!(
                "    ainc a1 += {}",
                if self.incremental {
                    self.n * self.stride
                } else {
                    0
                }
            ));
            if self.lift {
                lines.push(lift_seti());
                lines.push(format!("    vmov v0, {LIFT}"));
            } else {
                lines.push("    vmov v0, k0".into());
            }
            lines.push("    vsync fe on=math".into());
            for j in 0..self.n {
                lines.push(format!(
                    "    vpack v0 -> l1[{}] {}",
                    128 + j * 4,
                    self.dtype
                ));
            }
            lines.push("    vsync fe on=pack".into());
            lines.push("    seti s0 = trips".into());
            if self.slots == 2 {
                lines.extend([
                    "    skipz s0".into(),
                    "        loop s0".into(),
                    "            vdrain a1 <- l1[128] rel".into(),
                    "            vdrain a1 <- l1[128] rel".into(),
                    "    vdrain a1 <- l1[128] rel".into(),
                    "    vdrain a1 <- l1[128] rel".into(),
                ]);
            } else {
                lines.extend([
                    "    loop s0".into(),
                    "        vdrain a1 <- l1[128] rel".into(),
                ]);
            }
            lines.extend(["    vsync fe on=drain".into(), "    halt".into()]);
            return lines.join("\n");
        }
        let mut lines = vec![
            format!("image {}(trips)", self.name),
            format!(
                "    ainc a0 += {}",
                if self.incremental {
                    self.n * self.source_stride
                } else {
                    0
                }
            ),
            format!(
                "    ainc a1 += {}",
                if self.incremental {
                    self.n * self.stride
                } else {
                    0
                }
            ),
        ];
        if self.checked {
            lines.extend(["    seti s2 = 65536.0".into(), "    seti s3 = 16.0".into()]);
        }
        if self.lift {
            lines.push(lift_seti());
        }
        if self.slots == 1 {
            lines.extend([
                "    seti s0 = trips".into(),
                "    loop s0".into(),
                "        vfill a0 -> l1[0] rel".into(),
            ]);
            if self.split_rows {
                for (row, desc) in [3, 4, 5]
                    .iter()
                    .take(self.valid_rows.saturating_sub(1) as usize)
                    .enumerate()
                {
                    let slot = (row as i64 + 1) * self.row_words;
                    lines.push(format!("        vfill a{desc} -> l1[{slot}]"));
                }
            }
            lines.push("        vsync fe on=fill".into());
            self.compute(0, "        ", &mut lines);
        } else {
            // Prologue/steady-state/epilogue: no fill beyond the final pair.
            lines.extend([
                "    vfill a0 -> l1[0] rel".into(),
                "    vsync fe on=fill".into(),
                "    seti s0 = trips".into(),
            ]);
            if self.steady != Some(false) {
                // Known trip topology needs no SKIPZ displacement across a
                // large loop. Legacy parameterized images retain the guard.
                let outer = if self.steady.is_none() {
                    lines.push("    skipz s0".into());
                    "        "
                } else {
                    "    "
                };
                let ind = format!("{outer}    ");
                lines.push(format!("{outer}loop s0"));
                lines.push(format!("{ind}vfill a0 -> l1[192] rel"));
                self.compute(0, &ind, &mut lines);
                lines.push(format!("{ind}vsync fe on=fill"));
                lines.push(format!("{ind}vfill a0 -> l1[0] rel"));
                self.compute(1, &ind, &mut lines);
                lines.push(format!("{ind}vsync fe on=fill"));
            }
            lines.push("    vfill a0 -> l1[192] rel".into());
            self.compute(0, "    ", &mut lines);
            lines.push("    vsync fe on=fill".into());
            self.compute(1, "    ", &mut lines);
            if self.odd {
                lines.push("    vfill a0 -> l1[0] rel".into());
                lines.push("    vsync fe on=fill".into());
                self.compute(0, "    ", &mut lines);
            }
        }
        if self.checked {
            // RUN resets the sticky latch, but not RF. Fold each run before HALT.
            lines.extend([
                "    vsync fe on=math".into(),
                "    getstk s14".into(),
                "    vmax v31, v31, s14".into(),
                "    vsync fe on=math".into(),
            ]);
        }
        if self.valid_rows == 0 {
            lines.retain(|line| {
                !line.trim_start().starts_with("vfill ")
                    && !line.trim_start().starts_with("ainc a0")
            });
        }
        if self.fill_overlap {
            for line in &mut lines {
                *line = line.replace("vsync fe on=fill", "vsync unpack on=fill");
            }
        }
        lines.push("    halt".into());
        lines.join("\n")
    }
}
