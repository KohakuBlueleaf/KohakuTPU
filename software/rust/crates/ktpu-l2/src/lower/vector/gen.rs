//! Typed instruction constructors and compatibility printing.
//! The template generator is the feature-only witness reference.

pub mod predicate;
pub mod print;
pub mod typed;

#[cfg(test)]
mod tests;

#[cfg(feature = "witness")]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Code {
    Raw(String),
    Op(String, Vec<String>),
}

#[cfg(feature = "witness")]
impl Code {
    pub fn is_open(&self) -> bool {
        matches!(self, Code::Raw(s) if s == "{")
    }

    pub fn is_block_edge(&self) -> bool {
        matches!(self, Code::Raw(s) if s == "{" || s == "}")
    }
}

#[cfg(feature = "witness")]
#[derive(Default, Debug)]
pub struct Gen {
    pub n: usize,
    pub code: Vec<Code>,
}

#[cfg(feature = "witness")]
impl Gen {
    /// A fresh symbol, `%N`.
    pub fn sym(&mut self) -> String {
        self.n += 1;
        format!("%{}", self.n)
    }

    pub fn emit(&mut self, tmpl: impl Into<String>, syms: &[&str]) {
        self.emit_owned(tmpl, syms.iter().map(|s| s.to_string()).collect());
    }

    pub fn emit_owned(&mut self, tmpl: impl Into<String>, syms: Vec<String>) {
        let tmpl = tmpl.into();
        if crate::mutant::on("isa.vector/lower") && tmpl.contains("if=p1") {
            return;
        }
        self.code.push(Code::Op(tmpl, syms));
    }

    pub fn raw(&mut self, text: impl Into<String>) {
        let text = text.into();
        if crate::mutant::on("isa.vector/lower") && text.starts_with("getstk") {
            return;
        }
        self.code.push(Code::Raw(text));
    }
}
