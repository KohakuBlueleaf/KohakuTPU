//! Images by name: each planned once, encoded per entry state and arguments, and the words a body uses.

use super::*;

/// A `desc`, or a `when` holding nothing else.
pub(super) fn descs_only(st: &Stmt) -> bool {
    if st.op == "desc" {
        return true;
    }
    st.op == "when" && st.target.is_none() && st.body.iter().all(descs_only)
}

/// Every string a term holds: a superset of the names it reads.
pub(super) fn term_words(t: &Term, out: &mut BTreeSet<String>) {
    let mut s = |x: &str| {
        out.insert(x.to_string());
    };
    match t {
        Term::Name(n) | Term::Str(n) | Term::Arrow(n) => s(n),
        Term::Int(_, style) => s(match style {
            crate::text::IntStyle::Dec => "dec",
            crate::text::IntStyle::Hex => "hex",
            crate::text::IntStyle::Size => "size",
        }),
        Term::Float(_) | Term::Dims(_) | Term::NewAxis => {}
        Term::BinOp(op, a, b) => {
            s(&op.to_string());
            term_words(a, out);
            term_words(b, out);
        }
        Term::Compare(op, a, b) => {
            s(op);
            term_words(a, out);
            term_words(b, out);
        }
        Term::Neg(x) | Term::Not(x) | Term::Pos(x) => term_words(x, out),
        Term::Offset(a, b)
        | Term::Placed(a, b)
        | Term::AddAssign(a, b)
        | Term::Span(a, b)
        | Term::Assign(a, b)
        | Term::Member(a, b) => {
            term_words(a, out);
            term_words(b, out);
        }
        Term::Computed(p, i, sl) => {
            s(p);
            term_words(i, out);
            for x in sl.iter().flatten() {
                term_words(x, out);
            }
        }
        Term::Typed(n, x) | Term::Kw(n, x) => {
            s(n);
            term_words(x, out);
        }
        Term::Call(n, xs) | Term::View(n, xs) => {
            s(n);
            xs.iter().for_each(|x| term_words(x, out));
        }
        Term::Slice(lo, hi) => {
            for x in [lo, hi].into_iter().flatten() {
                term_words(x, out);
            }
        }
        Term::Tuple(xs) => xs.iter().for_each(|x| term_words(x, out)),
    }
}

pub(super) fn stmt_words(stmts: &[Stmt], out: &mut BTreeSet<String>) {
    for st in stmts {
        out.insert(st.op.clone());
        if let Some(t) = &st.target {
            match t {
                Term::Name(n) => {
                    out.insert(n.clone());
                }
                other => term_words(other, out),
            }
        }
        for a in &st.args {
            term_words(a, out);
        }
        if let Some(a) = &st.annot {
            term_words(a, out);
        }
        stmt_words(&st.body, out);
    }
}

/// An image's leading descriptor statements, the rest, and the parameters the
/// rest reads (the encoding's key).
pub(super) struct Plan {
    prefix: Vec<Stmt>,
    rest: Vec<Stmt>,
    keys: Vec<usize>,
}

/// Encoded image words, shared by every run of one encoding.
pub type ImageWords = Rc<Vec<u64>>;

/// `module`'s images, each encoded once per value of the parameters its
/// instructions read (L1.IMG.003). `units` resolves unit names.
pub struct Images<'a> {
    pub module: &'a Module,
    pub entry: State,
    pub units: Option<&'a dyn Units>,
    pub isa: VecIsa,
    pub facts: CoreFacts,
    reader: Reader,
    macros: indexmap::IndexMap<String, (Vec<String>, Vec<Stmt>)>,
    plans: HashMap<String, Plan>,
    encoded: HashMap<(String, Vec<i128>), (ImageWords, Descs)>,
}

impl<'a> Images<'a> {
    pub fn new(
        module: &'a Module,
        machine: &Machine,
        entry: State,
        units: Option<&'a dyn Units>,
    ) -> Result<Images<'a>> {
        Ok(Images {
            module,
            entry,
            units,
            isa: isa_for(machine, module.vregs)?,
            facts: CoreFacts::of(
                machine,
                if on("l1.img/regs32") {
                    32
                } else {
                    module.vregs
                },
            ),
            reader: module.reader(),
            macros: module.macro_table(),
            plans: HashMap::new(),
            encoded: HashMap::new(),
        })
    }

    fn plan(&mut self, d: &Definition) {
        if self.plans.contains_key(&d.name) {
            return;
        }
        let n = d.stmts.iter().take_while(|s| descs_only(s)).count();
        let (prefix, rest) = (d.stmts[..n].to_vec(), d.stmts[n..].to_vec());
        let mut todo = BTreeSet::new();
        stmt_words(&rest, &mut todo);
        let mut seen: BTreeSet<String> = BTreeSet::new();
        while let Some(w) = todo.pop_first() {
            if let Some((_, body)) = self.macros.get(&w) {
                if !seen.contains(&w) {
                    seen.insert(w.clone());
                    let mut more = BTreeSet::new();
                    stmt_words(body, &mut more);
                    todo.extend(more.into_iter().filter(|x| !seen.contains(x)));
                }
            }
            seen.insert(w);
        }
        let keys = d
            .params
            .iter()
            .enumerate()
            .filter(|(_, p)| seen.contains(*p) || on("l1.img/key-all-params"))
            .map(|(i, _)| i)
            .collect();
        self.plans
            .insert(d.name.clone(), Plan { prefix, rest, keys });
    }

    /// `(words, descs)` of image `name` for constant `args`: the IMEM words,
    /// and the descriptor fields its leading `desc` statements set.
    pub fn call(&mut self, st: &Stmt, name: &str, args: &[i128]) -> Result<(ImageWords, Descs)> {
        let reversed: Vec<i128>;
        let args = if on("l1.img/args-reversed") {
            reversed = args.iter().rev().copied().collect();
            &reversed[..]
        } else {
            args
        };
        let module = self.module;
        let Some(d) = module.images.get(name) else {
            let mut have: Vec<&String> = module.images.keys().collect();
            have.sort();
            return Err(self.reader.fail(
                st,
                "L1.IMG.032",
                format!("no image {name} in {}; it has {have:?}", module.file),
            ));
        };
        if args.len() != d.params.len() {
            return Err(self.reader.fail(
                st,
                "L1.IMG.032",
                format!(
                    "image {name} takes {} arguments, got {}",
                    d.params.len(),
                    args.len()
                ),
            ));
        }
        self.plan(d);
        let plan = &self.plans[name];
        let env: Env = d
            .params
            .iter()
            .zip(args)
            .map(|(p, a)| (p.clone(), Val::Int(*a)))
            .collect();
        let mut expand = Expander::new(&self.macros, &self.reader, true);
        let mut descs = Descs::new();
        for s in expand.stmts(&plan.prefix, &env)? {
            descs.extend(desc(&self.reader, &s, &self.facts, self.units)?);
        }
        let key = (
            name.to_string(),
            plan.keys.iter().map(|i| args[*i]).collect::<Vec<_>>(),
        );
        if !self.encoded.contains_key(&key) {
            let mut stmts = expand.stmts(&plan.rest, &env)?;
            let mut lead = Descs::new();
            while stmts.first().is_some_and(|s| s.op == "desc") {
                let s = stmts.remove(0);
                lead.extend(desc(&self.reader, &s, &self.facts, self.units)?);
            }
            if let Some(s) = stmts.iter().find(|s| s.op == "desc") {
                return Err(self.reader.fail(
                    s,
                    "L1.IMG.033",
                    "an image's descriptors lead it: they are set before its RUN",
                ));
            }
            let mut enc =
                Encoder::new(&self.reader, &self.isa, &self.facts, self.entry, self.units);
            let words = enc.encode(&stmts)?;
            self.encoded.insert(key.clone(), (Rc::new(words), lead));
        }
        let (words, lead) = &self.encoded[&key];
        let mut all = descs;
        all.extend(lead.iter().map(|(k, v)| (*k, *v)));
        Ok((words.clone(), all))
    }
}

/// `(words, descs)` of `module`'s image `name` for constant `args`, one call.
pub fn image(
    module: &Module,
    machine: &Machine,
    name: &str,
    args: &[i128],
    entry: State,
) -> Result<(Vec<u64>, Descs)> {
    let mut images = Images::new(module, machine, entry, None)?;
    let st = Stmt::new("run", Vec::new());
    let (words, descs) = images.call(&st, name, args)?;
    Ok(((*words).clone(), descs))
}
