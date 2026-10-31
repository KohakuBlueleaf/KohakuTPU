//! What a check finds, each kind named by its clause (l1/program.md s15).

use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    Deadlock,
    Rest,
    Port,
    Race,
    Bounds,
    Route,
    Loop,
    Model,
    Bank,
    Line,
    Cluster,
    Vector,
}

impl Kind {
    pub const ALL: [Kind; 12] = [
        Kind::Deadlock,
        Kind::Rest,
        Kind::Port,
        Kind::Race,
        Kind::Bounds,
        Kind::Route,
        Kind::Loop,
        Kind::Model,
        Kind::Bank,
        Kind::Line,
        Kind::Cluster,
        Kind::Vector,
    ];

    /// The name the Python checker gives the kind.
    pub fn name(self) -> &'static str {
        match self {
            Kind::Deadlock => "deadlock",
            Kind::Rest => "rest",
            Kind::Port => "port",
            Kind::Race => "race",
            Kind::Bounds => "bounds",
            Kind::Route => "route",
            Kind::Loop => "loop",
            Kind::Model => "model",
            Kind::Bank => "bank",
            Kind::Line => "line",
            Kind::Cluster => "cluster",
            Kind::Vector => "vector",
        }
    }

    pub fn parse(s: &str) -> Option<Kind> {
        Kind::ALL.into_iter().find(|k| k.name() == s)
    }

    /// The clause defining the kind.
    pub fn clause(self) -> &'static str {
        match self {
            Kind::Deadlock => "L1.VER.006",
            Kind::Rest => "L1.VER.007",
            Kind::Port => "L1.VER.008",
            Kind::Race => "L1.VER.009",
            Kind::Bounds => "L1.VER.010",
            Kind::Route => "L1.VER.011",
            Kind::Loop => "L1.VER.012",
            Kind::Model => "L1.VER.013",
            Kind::Bank => "L1.VER.014",
            Kind::Line => "L1.VER.017",
            Kind::Cluster => "L1.VER.018",
            Kind::Vector => "L1.VER.019",
        }
    }

    /// A kind a run is refused for (L1.VER.015): `model` and `loop` are
    /// reported, not refused.
    pub fn refuses(self) -> bool {
        !matches!(self, Kind::Loop | Kind::Model | Kind::Bank)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Finding {
    pub kind: Kind,
    pub at: String,
    pub text: String,
}

impl Finding {
    pub fn new(kind: Kind, at: impl Into<String>, text: impl Into<String>) -> Self {
        Finding {
            kind,
            at: at.into(),
            text: text.into(),
        }
    }

    pub fn clause(&self) -> &'static str {
        self.kind.clause()
    }
}

impl fmt::Display for Finding {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} [{}] {}: {}",
            self.clause(),
            self.kind.name(),
            self.at,
            self.text
        )
    }
}
