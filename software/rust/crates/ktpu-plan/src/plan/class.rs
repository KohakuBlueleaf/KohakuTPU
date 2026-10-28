//! The Python exception class each refusal is raised as: a caller catches
//! by class (the seam takes `lower.Refused` as "not this device", `kp`
//! callers `Unsupported`), so the class is part of a refusal, beside its
//! words. Classes by clause family: `TG.PLAN` / `TG.RUN` `Refused`,
//! `API.FORM` `FormError` (`forms.FormError`), the generators' checks
//! (`API.ROW` but .005, `L2.REL`) `ValueError`, the other `API` clauses
//! `kp`'s `Unsupported`.

use crate::mutant;

pub fn of(clause: &str) -> &'static str {
    if clause.starts_with("TG.PLAN") {
        return if mutant::on("tg.lower/refused_other") {
            "Other"
        } else {
            "Refused"
        };
    }
    match clause {
        c if c.starts_with("TG.RUN") => "Refused",
        c if c.starts_with("API.FORM") => "FormError",
        "API.ROW.005" => "Unsupported",
        c if c.starts_with("API.ROW") || c.starts_with("L2.REL") => "ValueError",
        c if c.starts_with("API.") => "Unsupported",
        _ => "Refused",
    }
}

/// A refusal as Python prints the exception: `Class: message`.
pub fn text(r: &ktpu_machine::Refusal) -> String {
    format!("{}: {}", of(&r.clause), r.message)
}
