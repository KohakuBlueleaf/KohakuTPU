//! Common register-value classifier and MX7 pack stage for quant schedulers.

use crate::mutant::on;

#[derive(Clone, Copy)]
pub(super) struct Finish<'a> {
    pub checked: bool,
    pub dtype: &'a str,
    pub pipeline: bool,
    pub classifier_overlap: bool,
    pub zero_classifier: bool,
    pub lift: bool,
}

impl Finish<'_> {
    #[allow(clippy::too_many_arguments)]
    pub fn batch(
        &self,
        first_v: i64,
        step: i64,
        n: i64,
        output: i64,
        rel: &str,
        ind: &str,
        lines: &mut Vec<String>,
    ) {
        if self.lift {
            for j in 0..n {
                let v = first_v + step * j;
                lines.push(format!(
                    "{ind}vsel v{v}, v{v}, {}, v{v}",
                    super::image::LIFT
                ));
            }
        }
        if self.checked && !on("l2.packing/drop-finite-check") {
            self.classify(first_v, step, n, ind, lines);
        }
        for j in 0..n {
            let v = first_v + step * j;
            if on("l2.packing/wrong-scale") {
                lines.push(format!("{ind}vadd v{v}, v{v}, v{v}"));
            }
            lines.push(format!(
                "{ind}vpack v{v} -> l1[{}] {}{rel}",
                output + j * 4,
                self.dtype
            ));
        }
        if !self.pipeline && !on("l2.packing/batch-no-pack-wait") {
            lines.push(format!("{ind}vsync fe on=pack"));
        }
    }

    fn classify(&self, first_v: i64, step: i64, n: i64, ind: &str, lines: &mut Vec<String>) {
        if self.zero_classifier {
            // IEEE specials precede the zero-factor shortcut: Inf/NaN*0 is
            // NaN and sets EXP2D sticky; finite*0 produces 1 below tau=8.
            for j in 0..n {
                let v = first_v + step * j;
                lines.push(format!("{ind}vmul v{}, v{v}, k0", v + 1));
            }
            for j in 0..n {
                let v = first_v + step * j;
                lines.push(format!("{ind}vexp2d v{}, v{}, k0", v + step - 1, v + 1));
            }
            if !self.pipeline {
                lines.push(format!("{ind}vsync fe on=math"));
            }
            return;
        }
        let overlap =
            self.pipeline || (self.classifier_overlap && !on("l2.packing/serial-classifier"));
        for j in 0..n {
            let v = first_v + step * j;
            lines.push(format!("{ind}vabs v{}, v{v}", v + 1));
            lines.push(format!("{ind}vmov v{}, s3", v + 2));
        }
        if !overlap {
            lines.push(format!("{ind}vsync fe on=math"));
        }
        for j in 0..n {
            lines.push(format!("{ind}vcmplt p{j}, v{}, s2", first_v + step * j + 1));
        }
        lines.push(format!("{ind}vsync fe on=math"));
        for j in 0..n {
            lines.push(format!("{ind}vmov v{}, k0 if=p{j}", first_v + step * j + 2));
        }
        if !overlap {
            lines.push(format!("{ind}vsync fe on=math"));
        }
        for j in 0..n {
            lines.push(format!(
                "{ind}vexp2d v{}, v{}, k0",
                first_v + step * j + 3,
                first_v + step * j + 2
            ));
        }
        if !self.pipeline {
            lines.push(format!("{ind}vsync fe on=math"));
        }
    }
}
