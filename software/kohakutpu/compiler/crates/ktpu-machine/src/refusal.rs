//! A stage's refusal: the clause id it breaks and what was asked.

use std::fmt;

/// An `Err` value of every stage: `clause` is a spec clause id
/// (`"ISA.MG.018"`), `message` says what was asked and what fits.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Refusal {
    pub clause: String,
    pub message: String,
}

impl Refusal {
    pub fn new(clause: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            clause: clause.into(),
            message: message.into(),
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.clause, self.message)
    }
}

impl std::error::Error for Refusal {}

/// `Err(Refusal::new(clause, message))`.
pub fn refuse<T>(clause: &str, message: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal::new(clause, message))
}
