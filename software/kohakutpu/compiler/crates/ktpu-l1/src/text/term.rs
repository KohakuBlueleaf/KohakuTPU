//! Terms and statements of the `.ktpu` text (L1.TXT.001-.013): the one
//! statement syntax every level shares. A level's reader gives them meaning.

/// How an integer was written: decimal, `0x...` or a K / M / G size.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum IntStyle {
    Dec,
    Hex,
    Size,
}

/// One term. `+` parses to `Offset`, `-` `*` `/` `%` to `BinOp`.
#[derive(Clone, Debug, PartialEq)]
pub enum Term {
    Name(String),
    Int(i128, IntStyle),
    Float(f64),
    Dims(Vec<i128>),
    Str(String),
    Neg(Box<Term>),
    Not(Box<Term>),
    Offset(Box<Term>, Box<Term>),
    /// `a - b`, `a * b`, `a / b` (floor division), `a % b`.
    BinOp(char, Box<Term>, Box<Term>),
    /// `+x`: in a slice's upper bound, an extent from the lower bound.
    Pos(Box<Term>),
    /// `TYPE @SPACE`.
    Placed(Box<Term>, Box<Term>),
    Compare(String, Box<Term>, Box<Term>),
    /// `v{expr}` with an optional view.
    Computed(String, Box<Term>, Option<Vec<Term>>),
    AddAssign(Box<Term>, Box<Term>),
    Span(Box<Term>, Box<Term>),
    Typed(String, Box<Term>),
    Kw(String, Box<Term>),
    Call(String, Vec<Term>),
    Slice(Option<Box<Term>>, Option<Box<Term>>),
    NewAxis,
    View(String, Vec<Term>),
    Tuple(Vec<Term>),
    Assign(Box<Term>, Box<Term>),
    Member(Box<Term>, Box<Term>),
    Arrow(String),
}

impl Term {
    pub fn name(s: impl Into<String>) -> Term {
        Term::Name(s.into())
    }

    pub fn int(v: i128) -> Term {
        Term::Int(v, IntStyle::Dec)
    }

    /// `v` as a decimal term: `Int`, or `Neg(Int)` when negative.
    pub fn dec(v: i128) -> Term {
        if v >= 0 {
            Term::Int(v, IntStyle::Dec)
        } else {
            Term::Neg(Box::new(Term::Int(-v, IntStyle::Dec)))
        }
    }

    pub fn as_name(&self) -> Option<&str> {
        match self {
            Term::Name(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_int(&self) -> Option<i128> {
        match self {
            Term::Int(v, _) => Some(*v),
            _ => None,
        }
    }

    /// An `Assign` whose left side is a name: a keyword argument.
    pub fn as_kwarg(&self) -> Option<(&str, &Term)> {
        match self {
            Term::Assign(a, b) => a.as_name().map(|n| (n, b.as_ref())),
            _ => None,
        }
    }

    pub fn is_arrow(&self, text: &str) -> bool {
        matches!(self, Term::Arrow(t) if t == text)
    }
}

/// One statement: `[target =] op args [: annot]` and an indented block.
/// `commas` is how its positional arguments print.
#[derive(Clone, Debug, PartialEq)]
pub struct Stmt {
    pub op: String,
    pub args: Vec<Term>,
    /// `Term::Name` or `Term::Computed`.
    pub target: Option<Term>,
    pub annot: Option<Term>,
    pub body: Vec<Stmt>,
    pub line: u32,
    pub col: u32,
    pub commas: bool,
}

impl Stmt {
    pub fn new(op: impl Into<String>, args: Vec<Term>) -> Stmt {
        Stmt {
            op: op.into(),
            args,
            target: None,
            annot: None,
            body: Vec::new(),
            line: 0,
            col: 0,
            commas: false,
        }
    }

    /// Every argument that is no keyword argument (L1.TXT.006).
    pub fn positional(&self) -> Vec<&Term> {
        let any_lhs = crate::mutant::on("l1.text/kwargs-any-lhs");
        self.args
            .iter()
            .filter(|a| a.as_kwarg().is_none() && !(any_lhs && matches!(a, Term::Assign(..))))
            .collect()
    }

    /// Keyword arguments in order; a later one of the same name wins.
    pub fn kwargs(&self) -> Vec<(&str, &Term)> {
        let mut out: Vec<(&str, &Term)> = Vec::new();
        for (k, v) in self.args.iter().filter_map(Term::as_kwarg) {
            match out.iter_mut().find(|(n, _)| *n == k) {
                Some(slot) => slot.1 = v,
                None => out.push((k, v)),
            }
        }
        out
    }

    /// The keyword argument `key`.
    pub fn kw(&self, key: &str) -> Option<&Term> {
        self.args
            .iter()
            .filter_map(Term::as_kwarg)
            .filter(|(k, _)| *k == key)
            .map(|(_, v)| v)
            .next_back()
    }

    /// The target's name, when it is a plain name.
    pub fn target_name(&self) -> Option<&str> {
        self.target.as_ref().and_then(Term::as_name)
    }
}
