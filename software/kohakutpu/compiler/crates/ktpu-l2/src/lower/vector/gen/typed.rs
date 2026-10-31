//! Statement constructors and ordered symbolic instruction events.

use ktpu_l1::text::{Stmt, Term};

pub type Result<T> = std::result::Result<T, String>;

/// A generator symbol; its allocation order is independent of operand occurrences.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Symbol(pub u32);

impl Symbol {
    pub fn name(&self) -> String {
        format!("%{}", self.0)
    }

    pub fn operand(&self, select: Select) -> Term {
        reg(&self.name(), select)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Select {
    Whole,
    Chunk(u64),
    Stride(u64),
}

pub fn reg(name: &str, select: Select) -> Term {
    match select {
        Select::Whole => Term::name(name),
        Select::Chunk(b) => Term::View(name.into(), vec![Term::int(b.into())]),
        Select::Stride(b) => Term::View(
            name.into(),
            vec![Term::Slice(Some(Box::new(Term::int(b.into()))), None)],
        ),
    }
}

pub fn kw(name: &str, value: Term) -> Term {
    Term::Assign(Box::new(Term::name(name)), Box::new(value))
}

pub fn call_kw(name: &str, value: Term) -> Term {
    Term::Kw(name.into(), Box::new(value))
}

pub fn pair(a: i64, b: i64) -> Term {
    Term::Tuple(vec![Term::dec(a.into()), Term::dec(b.into())])
}

pub fn walk(dims: &[(i64, i64)]) -> Result<Term> {
    match dims {
        [] => Err("a descriptor walk has at least one dimension".into()),
        [(s, n)] => Ok(pair(*s, *n)),
        _ => Ok(Term::Tuple(dims.iter().map(|&(s, n)| pair(s, n)).collect())),
    }
}

pub fn float(value: f64) -> Result<Term> {
    if !value.is_finite() {
        return Err("a generated constant is finite".into());
    }
    Ok(if value.is_sign_negative() {
        Term::Neg(Box::new(Term::Float(-value)))
    } else {
        Term::Float(value)
    })
}

/// Positional arguments followed by options in their emission order.
pub fn instruction(op: &str, args: Vec<Term>, options: Vec<Term>) -> Stmt {
    let mut st = Stmt::new(op, args);
    st.args.extend(options);
    st
}

pub fn math(op: &str, dest: Term, sources: Vec<Term>, options: Vec<Term>) -> Stmt {
    let mut args = vec![dest];
    args.extend(sources);
    let mut st = instruction(op, args, options);
    st.commas = true;
    st
}

pub fn crossbar(mode: &str, source: Term, k: i64) -> Term {
    Term::Call(mode.into(), vec![source, Term::dec(k.into())])
}

pub fn bcast(source: Term, lane: i64, shift: i64) -> Term {
    Term::Call(
        "bcast".into(),
        vec![
            source,
            call_kw("lane", Term::dec(lane.into())),
            call_kw("sh", Term::dec(shift.into())),
        ],
    )
}

pub fn l1(word: i64) -> Term {
    Term::View("l1".into(), vec![Term::dec(word.into())])
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Memory {
    Unpack,
    Pack,
    Fill,
    Drain,
}

pub fn memory(
    kind: Memory,
    operand: Term,
    word: i64,
    mode: Option<&str>,
    rel: bool,
    options: Vec<Term>,
) -> Result<Stmt> {
    let (op, arrow, needs_mode) = match kind {
        Memory::Unpack => ("vunpk", "<-", true),
        Memory::Pack => ("vpack", "->", true),
        Memory::Fill => ("vfill", "->", false),
        Memory::Drain => ("vdrain", "<-", false),
    };
    if mode.is_some() != needs_mode {
        return Err("only pack and unpack take a mode".into());
    }
    let mut args = vec![operand, Term::Arrow(arrow.into()), l1(word)];
    if let Some(mode) = mode {
        args.push(Term::name(mode));
    }
    if rel {
        args.push(Term::name("rel"));
    }
    Ok(instruction(op, args, options))
}

pub fn seti(register: &str, value: Term) -> Stmt {
    Stmt::new("seti", vec![kw(register, value)])
}

pub fn block(op: &str, register: &str, body: Vec<Stmt>) -> Result<Stmt> {
    if !matches!(op, "loop" | "skipz") || body.is_empty() {
        return Err("a generated block is a nonempty loop or skipz".into());
    }
    let mut st = Stmt::new(op, vec![Term::name(register)]);
    st.body = body;
    Ok(st)
}

pub fn sync(waiter: &str, on: &str, slack: Option<Term>) -> Stmt {
    let mut options = vec![kw("on", Term::name(on))];
    if let Some(slack) = slack {
        options.push(kw("slack", slack));
    }
    instruction("vsync", vec![Term::name(waiter)], options)
}

pub fn wait(engine: &str, counter: &str, count: i64) -> Stmt {
    instruction(
        "vwait",
        vec![Term::name(engine), Term::name(counter)],
        vec![kw("n", Term::dec(count.into()))],
    )
}

pub fn post(peer: &str, count: i64) -> Stmt {
    instruction(
        "vpost",
        vec![Term::name(peer)],
        vec![kw("n", Term::dec(count.into()))],
    )
}

pub fn peer(offset: i64, count: bool) -> Term {
    let mut args = vec![Term::dec(offset.into())];
    if count {
        args.push(call_kw("count", Term::int(1)));
    }
    Term::Call("peer".into(), args)
}

pub fn desc(register: &str, base: Term, dims: &[(i64, i64)]) -> Result<Stmt> {
    Ok(Stmt::new(
        "desc",
        vec![kw(register, base), kw("walk", walk(dims)?)],
    ))
}

pub fn ainc(register: &str, increment: i64) -> Stmt {
    Stmt::new(
        "ainc",
        vec![Term::AddAssign(
            Box::new(Term::name(register)),
            Box::new(Term::dec(increment.into())),
        )],
    )
}

/// Symbols retain duplicates and unused destinations, including compare results.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Op { stmt: Stmt, syms: Vec<Symbol> },
    Fixed(Stmt),
    Open,
    Close,
}

impl Event {
    pub fn is_open(&self) -> bool {
        matches!(self, Self::Open)
    }
    pub fn is_block_edge(&self) -> bool {
        matches!(self, Self::Open | Self::Close)
    }
}

/// Explicit blocks become statement bodies after allocation; symbolic operands stay unresolved.
pub fn nest(events: &[Event]) -> Result<Vec<Stmt>> {
    let mut stack: Vec<Vec<Stmt>> = vec![Vec::new()];
    for event in events {
        match event {
            Event::Op { stmt, .. } | Event::Fixed(stmt) => {
                stack.last_mut().unwrap().push(stmt.clone())
            }
            Event::Open => {
                let parent = stack.last().unwrap().last().ok_or("block has no header")?;
                if !matches!(parent.op.as_str(), "loop" | "skipz") || !parent.body.is_empty() {
                    return Err("block header must be an empty loop or skipz".into());
                }
                stack.push(Vec::new());
            }
            Event::Close => {
                if stack.len() == 1 {
                    return Err("unmatched block close".into());
                }
                let body = stack.pop().unwrap();
                if body.is_empty() {
                    return Err("empty generated block".into());
                }
                stack.last_mut().unwrap().last_mut().unwrap().body = body;
            }
        }
    }
    if stack.len() != 1 {
        return Err("unclosed generated block".into());
    }
    Ok(stack.pop().unwrap())
}
