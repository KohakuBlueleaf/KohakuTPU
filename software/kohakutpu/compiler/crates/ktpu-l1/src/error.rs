//! Refusals: an error names the clause it enforces and, for text, where.

use std::fmt;

/// A refusal of the L1 stack: the clause id (`"L1.NODE.021"`), the message
/// and, for text, the file, line and column with the source line and a caret.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error {
    pub clause: String,
    pub message: String,
    pub file: String,
    pub line: u32,
    pub col: u32,
    snippet: String,
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn new(clause: impl Into<String>, message: impl Into<String>) -> Error {
        Error {
            clause: clause.into(),
            message: message.into(),
            file: String::new(),
            line: 0,
            col: 0,
            snippet: String::new(),
        }
    }

    /// An error at `line`:`col` of `source` (named `file`).
    pub fn at(
        clause: impl Into<String>,
        message: impl Into<String>,
        line: u32,
        col: u32,
        source: &str,
        file: &str,
    ) -> Error {
        let mut snippet = String::new();
        if line > 0 {
            if let Some(text) = source.lines().nth(line as usize - 1) {
                let pad = " ".repeat(col.saturating_sub(1) as usize);
                snippet = format!("\n    {text}\n    {pad}^");
            }
        }
        Error {
            clause: clause.into(),
            message: message.into(),
            file: file.to_string(),
            line,
            col,
            snippet,
        }
    }
}

impl From<ktpu_machine::Refusal> for Error {
    fn from(r: ktpu_machine::Refusal) -> Error {
        Error::new(r.clause, r.message)
    }
}

impl From<ktpu_l0::Refusal> for Error {
    fn from(r: ktpu_l0::Refusal) -> Error {
        Error::new(r.clause, r.msg)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.file.is_empty() {
            return write!(f, "{}: {}", self.clause, self.message);
        }
        if self.line > 0 {
            write!(
                f,
                "{}: {}:{}:{}: {}{}",
                self.clause, self.file, self.line, self.col, self.message, self.snippet
            )
        } else {
            write!(f, "{}: {}: {}", self.clause, self.file, self.message)
        }
    }
}

impl std::error::Error for Error {}
