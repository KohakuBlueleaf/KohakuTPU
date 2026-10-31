// Pest pairs to `Stmt` / `Term`: included in each grammar's module of
// `parse.rs`, where `Rule` is that grammar's.

/// One logical line's statement (its position the caller's), or where the
/// grammar stops reading it.
pub(super) fn read(text: &str) -> std::result::Result<Stmt, LineColLocation> {
    let mut pairs = Ktpu::parse(Rule::line, text).map_err(|e| e.line_col)?;
    let top = pairs.next().unwrap().into_inner().next().unwrap();
    let mut st = Stmt::new("", Vec::new());
    match top.as_rule() {
        Rule::binding => {
            let mut it = top.into_inner();
            st.target = Some(Term::Name(it.next().unwrap().as_str().to_string()));
            it.next(); // eq
            command(it.next().unwrap(), &mut st);
        }
        Rule::computed_binding => {
            let mut it = top.into_inner();
            let prefix = it.next().unwrap().as_str().to_string();
            let index = expr(it.next().unwrap());
            st.target = Some(Term::Computed(prefix, Box::new(index), None));
            it.next(); // eq
            command(it.next().unwrap(), &mut st);
        }
        _ => command(top, &mut st),
    }
    Ok(st)
}

fn command(p: Pair<Rule>, st: &mut Stmt) {
    for part in p.into_inner() {
        match part.as_rule() {
            Rule::op => st.op = part.as_str().to_string(),
            Rule::args => {
                for a in part.into_inner() {
                    match a.as_rule() {
                        Rule::comma => st.commas = true,
                        _ => st.args.push(arg(a)),
                    }
                }
            }
            Rule::annot => st.annot = Some(term(part.into_inner().next().unwrap())),
            _ => unreachable!("command part {:?}", part.as_rule()),
        }
    }
}

fn arg(p: Pair<Rule>) -> Term {
    let mut it = p.into_inner();
    let first = it.next().unwrap();
    if first.as_rule() == Rule::arrow {
        return Term::Arrow(first.as_str().to_string());
    }
    let lhs = term(first);
    let Some(tail) = it.next() else {
        return lhs;
    };
    let rule = tail.as_rule();
    let rhs = term(tail.into_inner().next_back().unwrap());
    let (a, b) = (Box::new(lhs), Box::new(rhs));
    match rule {
        Rule::assign_tail => Term::Assign(a, b),
        Rule::add_tail => Term::AddAssign(a, b),
        _ => Term::Member(a, b),
    }
}

fn term(p: Pair<Rule>) -> Term {
    let mut it = p.into_inner();
    let lhs = expr(it.next().unwrap());
    let Some(tail) = it.next() else {
        return lhs;
    };
    match tail.as_rule() {
        Rule::span_tail => Term::Span(
            Box::new(lhs),
            Box::new(expr(tail.into_inner().next().unwrap())),
        ),
        _ => {
            let mut t = tail.into_inner();
            let op = t.next().unwrap().as_str().to_string();
            Term::Compare(op, Box::new(lhs), Box::new(expr(t.next().unwrap())))
        }
    }
}

fn expr(p: Pair<Rule>) -> Term {
    let mut it = p.into_inner();
    let mut acc = product(it.next().unwrap());
    while let Some(op) = it.next() {
        let rhs = product(it.next().unwrap());
        acc = if op.as_str() == "+" && on("l1.text/plus-binop") {
            Term::BinOp('+', Box::new(acc), Box::new(rhs))
        } else if op.as_str() == "+" {
            Term::Offset(Box::new(acc), Box::new(rhs))
        } else {
            Term::BinOp('-', Box::new(acc), Box::new(rhs))
        };
    }
    acc
}

fn product(p: Pair<Rule>) -> Term {
    let mut it = p.into_inner();
    let mut acc = unary(it.next().unwrap());
    while let Some(op) = it.next() {
        let rhs = unary(it.next().unwrap());
        let c = op.as_str().chars().next().unwrap();
        acc = Term::BinOp(c, Box::new(acc), Box::new(rhs));
    }
    acc
}

fn unary(p: Pair<Rule>) -> Term {
    let mut it = p.into_inner();
    let inner = it.next().unwrap();
    match inner.as_rule() {
        Rule::neg | Rule::pos | Rule::notp => {
            let rule = inner.as_rule();
            let x = Box::new(unary(inner.into_inner().nth(1).unwrap()));
            match rule {
                Rule::neg => Term::Neg(x),
                Rule::pos => Term::Pos(x),
                _ => Term::Not(x),
            }
        }
        Rule::placed => {
            let mut it = inner.into_inner();
            let x = atom(it.next().unwrap());
            Term::Placed(Box::new(x), Box::new(atom(it.next().unwrap())))
        }
        _ => {
            let x = atom(inner);
            match it.next() {
                Some(tail) => Term::Placed(
                    Box::new(x),
                    Box::new(atom(tail.into_inner().next().unwrap())),
                ),
                None => x,
            }
        }
    }
}

fn digits(s: &str) -> String {
    s.chars().filter(|c| *c != '_').collect()
}

fn atom(p: Pair<Rule>) -> Term {
    let inner = p.into_inner().next().unwrap();
    let s = inner.as_str();
    match inner.as_rule() {
        Rule::hex => Term::Int(
            i128::from_str_radix(&digits(&s[2..]), 16).unwrap_or(i128::MAX),
            IntStyle::Hex,
        ),
        Rule::size => {
            let d = digits(s);
            let shift = match d.as_bytes()[d.len() - 1] {
                b'K' => 10,
                b'M' => 20,
                _ => 30,
            };
            let v: i128 = d[..d.len() - 1].parse().unwrap_or(0);
            if on("l1.text/size-decimal") {
                return Term::Int(v * 1000i128.pow(shift / 10), IntStyle::Size);
            }
            Term::Int(v << shift, IntStyle::Size)
        }
        Rule::int => Term::Int(digits(s).parse().unwrap_or(i128::MAX), IntStyle::Dec),
        Rule::float => Term::Float(digits(s).parse().unwrap_or(f64::NAN)),
        Rule::dims => Term::Dims(s.split('x').map(|v| v.parse().unwrap_or(0)).collect()),
        Rule::string => Term::Str(s[1..s.len() - 1].to_string()),
        Rule::name => Term::Name(s.to_string()),
        Rule::call => {
            let mut it = inner.into_inner();
            let name = it.next().unwrap().as_str().to_string();
            let args = it
                .next()
                .map(|c| c.into_inner().map(carg).collect())
                .unwrap_or_default();
            Term::Call(name, args)
        }
        Rule::view => {
            let mut it = inner.into_inner();
            let name = it.next().unwrap().as_str().to_string();
            Term::View(name, it.next().map(slices).unwrap_or_default())
        }
        Rule::computed => {
            let mut it = inner.into_inner();
            let prefix = it.next().unwrap().as_str().to_string();
            let index = expr(it.next().unwrap());
            // `v{i}[]` is a view of no slices; `v{i}` none.
            let view = if s.trim_end().ends_with(']') {
                Some(it.next().map(slices).unwrap_or_default())
            } else {
                None
            };
            Term::Computed(prefix, Box::new(index), view)
        }
        Rule::paren => {
            let mut items = Vec::new();
            let mut commas = false;
            for x in inner.into_inner() {
                match x.as_rule() {
                    Rule::tcomma => commas = true,
                    _ => items.push(term(x)),
                }
            }
            if commas || items.is_empty() {
                Term::Tuple(items)
            } else {
                items.pop().unwrap()
            }
        }
        r => unreachable!("atom {r:?}"),
    }
}

fn carg(p: Pair<Rule>) -> Term {
    let inner = p.into_inner().next().unwrap();
    match inner.as_rule() {
        Rule::typed => {
            let mut it = inner.into_inner();
            let n = it.next().unwrap().as_str().to_string();
            Term::Typed(n, Box::new(term(it.next().unwrap())))
        }
        Rule::kwarg => {
            let mut it = inner.into_inner();
            let n = it.next().unwrap().as_str().to_string();
            it.next(); // eq
            Term::Kw(n, Box::new(term(it.next().unwrap())))
        }
        _ => term(inner),
    }
}

fn slices(p: Pair<Rule>) -> Vec<Term> {
    p.into_inner().map(slice).collect()
}

fn slice(p: Pair<Rule>) -> Term {
    let inner = p.into_inner().next().unwrap();
    match inner.as_rule() {
        Rule::newaxis => Term::NewAxis,
        Rule::colon_first => {
            let hi = inner.into_inner().nth(1).map(|t| Box::new(term(t)));
            Term::Slice(None, hi)
        }
        _ => {
            let mut it = inner.into_inner();
            let lo = term(it.next().unwrap());
            match it.next() {
                None => lo,
                Some(_) => Term::Slice(Some(Box::new(lo)), it.next().map(|t| Box::new(term(t)))),
            }
        }
    }
}
