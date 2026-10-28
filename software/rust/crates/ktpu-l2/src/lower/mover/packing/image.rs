//! MX7 batch images and a device-side finite-exponent classifier.

use crate::mutant::on;

pub fn clear(name: &str) -> String {
    format!("image {name}()\n    vsync fe on=math\n    getstk s14\n    vmov v31, k0\n    vsync fe on=math\n    halt")
}

pub fn status(name: &str) -> String {
    format!("image {name}(status)\n    desc a2 = status walk=(32, 4)\n    vpack v31 -> l1[384] f16 n=4\n    vsync fe on=pack\n    vdrain a2 <- l1[384]\n    vsync fe on=drain\n    halt")
}

pub struct Batch<'a> {
    pub name: &'a str,
    pub n: i64,
    pub pitch: i64,
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
        if self.checked && self.finite_batch > 1 {
            for first in (0..self.n).step_by(self.finite_batch as usize) {
                self.classified(first, input, output, ind, lines);
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
        lines.push(format!("{ind}vdrain a1 <- l1[{output}] rel"));
        if !self.overlap && !on("l2.packing/no-drain-wait") {
            lines.push(format!("{ind}vsync fe on=drain"));
        }
    }

    fn input_words(&self) -> i64 {
        self.valid_rows * self.row_words
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

    fn classified(&self, first: i64, input: i64, output: i64, ind: &str, lines: &mut Vec<String>) {
        let n = self.finite_batch.min(self.n - first);
        for j in 0..n {
            self.unpack(4 * j, input + (first + j) * self.input_words(), ind, lines);
        }
        lines.push(format!("{ind}vsync fe on=unpack"));
        for j in 0..n {
            lines.push(format!("{ind}vabs v{}, v{}", 4 * j + 1, 4 * j));
            lines.push(format!("{ind}vmov v{}, s3", 4 * j + 2));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!("{ind}vcmplt p{j}, v{}, s2", 4 * j + 1));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!("{ind}vmov v{}, k0 if=p{j}", 4 * j + 2));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!("{ind}vexp2d v{}, v{}, k0", 4 * j + 3, 4 * j + 2));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!(
                "{ind}vpack v{} -> l1[{}] {}",
                4 * j,
                output + (first + j) * 4,
                self.dtype
            ));
        }
        if !on("l2.packing/batch-no-pack-wait") {
            lines.push(format!("{ind}vsync fe on=pack"));
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
        let mut lines = vec![
            format!("image {}(trips)", self.name),
            format!(
                "    ainc a0 += {}",
                if self.incremental {
                    self.n * 4 * self.pitch
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
                "    skipz s0".into(),
                "        loop s0".into(),
                "            vfill a0 -> l1[192] rel".into(),
            ]);
            self.compute(0, "            ", &mut lines);
            lines.extend([
                "            vsync fe on=fill".into(),
                "            vfill a0 -> l1[0] rel".into(),
            ]);
            self.compute(1, "            ", &mut lines);
            lines.extend([
                "            vsync fe on=fill".into(),
                "    vfill a0 -> l1[192] rel".into(),
            ]);
            self.compute(0, "    ", &mut lines);
            lines.push("    vsync fe on=fill".into());
            self.compute(1, "    ", &mut lines);
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
        lines.push("    halt".into());
        lines.join("\n")
    }
}
