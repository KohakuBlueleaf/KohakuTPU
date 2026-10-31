//! Refusals: what L0 will not write or read, by the clause that says so.

/// A refusal naming its clause id (`"L0.PKG.032"`) and why.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{clause}: {msg}")]
pub struct Refusal {
    pub clause: &'static str,
    pub msg: String,
}

impl Refusal {
    pub fn new(clause: &'static str, msg: impl Into<String>) -> Refusal {
        Refusal {
            clause,
            msg: msg.into(),
        }
    }
}

pub type Result<T> = std::result::Result<T, Refusal>;

/// `Err(Refusal)` naming `clause`, formatted like `format!`.
#[macro_export]
macro_rules! refuse {
    ($clause:expr, $($arg:tt)*) => {
        Err($crate::Refusal::new($clause, format!($($arg)*)))
    };
}
