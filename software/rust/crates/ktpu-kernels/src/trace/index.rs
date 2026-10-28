//! Index arithmetic as the tracer records it (DSL.IDX): an int, or an
//! affine form `const + sum(coef * atom)` whose terms keep the order they
//! were first added in. An atom is a variable or a floor division, modulo
//! or product the form cannot hold; atoms compare by identity, so two
//! atoms made apart are two terms.

use ktpu_graph as g;
use std::rc::Rc;

/// An int, or an affine form (DSL.IDX.001).
#[derive(Clone, Debug)]
pub enum Idx {
    Int(i64),
    Sym(Rc<Sym>),
}

/// `konst + sum(coef * atom)`, zero coefficients dropped.
#[derive(Clone, Debug)]
pub struct Sym {
    pub terms: Vec<(Atom, i64)>,
    pub konst: i64,
}

/// A variable or an operation the affine form cannot hold (DSL.IDX.007).
#[derive(Clone, Debug)]
pub struct Atom(Rc<AtomKind>);

#[derive(Debug)]
pub enum AtomKind {
    Var(String),
    /// `(a op b)`: op `/` (floor division), `%` or `*`.
    Bin(char, Idx, Idx),
}

impl Atom {
    fn same(&self, other: &Atom) -> bool {
        Rc::ptr_eq(&self.0, &other.0)
    }

    pub fn kind(&self) -> &AtomKind {
        &self.0
    }
}

impl From<i64> for Idx {
    fn from(v: i64) -> Idx {
        Idx::Int(v)
    }
}

impl From<&Idx> for Idx {
    fn from(v: &Idx) -> Idx {
        v.clone()
    }
}

/// A new variable `name`.
pub fn var(name: &str) -> Idx {
    let atom = Atom(Rc::new(AtomKind::Var(name.to_string())));
    Idx::Sym(Rc::new(Sym {
        terms: vec![(atom, 1)],
        konst: 0,
    }))
}

fn sym_of(x: &Idx) -> Rc<Sym> {
    match x {
        Idx::Sym(s) => s.clone(),
        Idx::Int(v) => Rc::new(Sym {
            terms: vec![],
            konst: *v,
        }),
    }
}

/// A form from terms, zero coefficients dropped; an int when no term is
/// left (`done`), else the form.
fn make(terms: Vec<(Atom, i64)>, konst: i64, done: bool) -> Idx {
    let terms: Vec<_> = terms.into_iter().filter(|(_, c)| *c != 0).collect();
    if done && terms.is_empty() {
        Idx::Int(konst)
    } else {
        Idx::Sym(Rc::new(Sym { terms, konst }))
    }
}

fn atom(op: char, a: Idx, b: Idx) -> Idx {
    let at = Atom(Rc::new(AtomKind::Bin(op, a, b)));
    Idx::Sym(Rc::new(Sym {
        terms: vec![(at, 1)],
        konst: 0,
    }))
}

fn floor_div(a: i64, b: i64) -> i64 {
    let q = a / b;
    if (a % b != 0) && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

fn floor_mod(a: i64, b: i64) -> i64 {
    a - b * floor_div(a, b)
}

impl Idx {
    pub fn int(&self) -> Option<i64> {
        match self {
            Idx::Int(v) => Some(*v),
            Idx::Sym(_) => None,
        }
    }

    pub fn is_sym(&self) -> bool {
        matches!(self, Idx::Sym(_))
    }

    /// `self + o`: the left form's terms first, then the right's new ones.
    pub fn add(&self, o: impl Into<Idx>) -> Idx {
        let o = o.into();
        match (self, &o) {
            (Idx::Int(a), Idx::Int(b)) => Idx::Int(a + b),
            (Idx::Int(_), Idx::Sym(_)) => o.add(self),
            (Idx::Sym(s), _) => {
                let r = sym_of(&o);
                let mut terms = s.terms.clone();
                for (a, c) in &r.terms {
                    match terms.iter_mut().find(|(t, _)| t.same(a)) {
                        Some(slot) => slot.1 += c,
                        None => terms.push((a.clone(), *c)),
                    }
                }
                make(terms, s.konst + r.konst, true)
            }
        }
    }

    pub fn neg(&self) -> Idx {
        match self {
            Idx::Int(v) => Idx::Int(-v),
            Idx::Sym(s) => make(
                s.terms.iter().map(|(a, c)| (a.clone(), -c)).collect(),
                -s.konst,
                true,
            ),
        }
    }

    /// `self - o` (`s + (-o)`; `int - s` is `int + (-s)`).
    pub fn sub(&self, o: impl Into<Idx>) -> Idx {
        let o = o.into();
        match (self, &o) {
            (Idx::Int(a), Idx::Int(b)) => Idx::Int(a - b),
            (Idx::Int(a), Idx::Sym(_)) => {
                let lead = Idx::Sym(sym_of(&Idx::Int(*a)));
                lead.add(o.neg())
            }
            (Idx::Sym(_), _) => self.add(o.neg()),
        }
    }

    /// `self * o`: scaled by an int, a `*` atom of two forms.
    pub fn mul(&self, o: impl Into<Idx>) -> Idx {
        let o = o.into();
        match (self, &o) {
            (Idx::Int(a), Idx::Int(b)) => Idx::Int(a * b),
            (Idx::Int(_), Idx::Sym(_)) => o.mul(self),
            (Idx::Sym(s), Idx::Int(k)) => make(
                s.terms.iter().map(|(a, c)| (a.clone(), c * k)).collect(),
                s.konst * k,
                true,
            ),
            (Idx::Sym(_), Idx::Sym(_)) => atom('*', self.clone(), o.clone()),
        }
    }

    /// Floor division (DSL.IDX.002): a form whose coefficients and constant
    /// the int divides is divided, else one `/` atom.
    pub fn div(&self, o: impl Into<Idx>) -> Idx {
        let o = o.into();
        match (self, &o) {
            (Idx::Int(a), Idx::Int(b)) => Idx::Int(floor_div(*a, *b)),
            (Idx::Int(_), Idx::Sym(_)) => atom('/', Idx::Sym(sym_of(self)), o.clone()),
            (Idx::Sym(s), Idx::Int(k)) => {
                if *k == 1 {
                    return self.clone();
                }
                if s.terms.iter().all(|(_, c)| c % k == 0) && s.konst % k == 0 {
                    return make(
                        s.terms
                            .iter()
                            .map(|(a, c)| (a.clone(), floor_div(*c, *k)))
                            .collect(),
                        floor_div(s.konst, *k),
                        true,
                    );
                }
                atom('/', self.clone(), o.clone())
            }
            (Idx::Sym(_), Idx::Sym(_)) => atom('/', self.clone(), o.clone()),
        }
    }

    /// Modulo: `% 1` is 0, any other of a form one `%` atom.
    pub fn rem(&self, o: impl Into<Idx>) -> Idx {
        let o = o.into();
        match (self, &o) {
            (Idx::Int(a), Idx::Int(b)) => Idx::Int(floor_mod(*a, *b)),
            (Idx::Int(_), Idx::Sym(_)) => atom('%', Idx::Sym(sym_of(self)), o.clone()),
            (Idx::Sym(_), Idx::Int(1)) => Idx::Int(0),
            (Idx::Sym(_), _) => atom('%', self.clone(), o.clone()),
        }
    }

    /// The graph's index (DSL.GRAPH.007).
    pub fn graph(&self) -> g::Index {
        match self {
            Idx::Int(v) => g::Index::Int(*v),
            Idx::Sym(s) => g::Index::Sym(g::Sym {
                konst: s.konst,
                terms: s.terms.iter().map(|(a, c)| (atom_graph(a), *c)).collect(),
            }),
        }
    }
}

fn atom_graph(a: &Atom) -> g::Atom {
    match a.kind() {
        AtomKind::Var(n) => g::Atom::Var(n.clone()),
        AtomKind::Bin(op, x, y) => {
            let (x, y) = (Box::new(x.graph()), Box::new(y.graph()));
            match op {
                '/' => g::Atom::Floor(x, y),
                '%' => g::Atom::Mod(x, y),
                _ => g::Atom::Mul(x, y),
            }
        }
    }
}

/// An index comparison for `when` (DSL.IDX.004, .008).
#[derive(Clone, Debug)]
pub struct Cond {
    pub op: g::CmpOp,
    pub a: Idx,
    pub b: Idx,
}

impl Cond {
    pub fn new(a: &Idx, op: g::CmpOp, b: impl Into<Idx>) -> Cond {
        Cond {
            op,
            a: a.clone(),
            b: b.into(),
        }
    }

    pub fn graph(&self) -> g::Cond {
        g::Cond {
            op: self.op,
            a: self.a.graph(),
            b: self.b.graph(),
        }
    }
}
