//! A `.ktpu` module (L1.MOD.001-.010): one text of `target`, `fn`, `image`
//! and `macro` statements. A body stays statements: its level's reader gives
//! them meaning.

use indexmap::IndexMap;

use super::parse::parse;
use super::term::{Stmt, Term};
use crate::error::{Error, Result};
use crate::mutant::on;

/// The levels a `fn NAME.LEVEL` names.
pub const LEVELS: [&str; 2] = ["l2", "l1"];
/// Vector register counts a core is built with, and its register file's read
/// copies a bank.
pub const VREGS: [u32; 3] = [32, 64, 128];
pub const VCOPIES: [u32; 3] = [2, 3, 4];

/// Diagnostics context: the source text and its file name.
#[derive(Clone, Debug, Default)]
pub struct Reader {
    pub source: String,
    pub file: String,
}

impl Reader {
    pub fn new(source: &str, file: &str) -> Reader {
        Reader {
            source: source.to_string(),
            file: file.to_string(),
        }
    }

    /// An error at `st`'s line and column.
    pub fn fail(&self, st: &Stmt, clause: &str, message: impl Into<String>) -> Error {
        Error::at(clause, message, st.line, st.col, &self.source, &self.file)
    }
}

/// One fn body: `params` are `(name, type term)`; `ret` a type term.
#[derive(Clone, Debug, PartialEq)]
pub struct Body {
    pub name: String,
    pub level: String,
    pub params: Vec<(String, Term)>,
    pub ret: Option<Term>,
    pub stmts: Vec<Stmt>,
    pub line: u32,
}

/// An `image` or a `macro`: parameter names and the statements.
#[derive(Clone, Debug, PartialEq)]
pub struct Definition {
    pub kind: String,
    pub name: String,
    pub params: Vec<String>,
    pub stmts: Vec<Stmt>,
    pub line: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Module {
    pub source: String,
    pub file: String,
    pub target: Option<String>,
    /// The vector core the images are written for: its register count and
    /// its register file's read copies a bank.
    pub vregs: u32,
    pub vcopies: u32,
    /// `(name, level) -> Body`, in text order.
    pub fns: IndexMap<(String, String), Body>,
    pub images: IndexMap<String, Definition>,
    pub macros: IndexMap<String, Definition>,
}

impl Default for Module {
    fn default() -> Module {
        Module {
            source: String::new(),
            file: "<ktpu>".into(),
            target: None,
            vregs: 32,
            vcopies: 4,
            fns: IndexMap::new(),
            images: IndexMap::new(),
            macros: IndexMap::new(),
        }
    }
}

impl Module {
    /// The `target` statement that reads back to this module's target.
    pub fn target_line(&self) -> String {
        let mut out = format!("target {}", self.target.as_deref().unwrap_or("None"));
        if self.vregs != 32 {
            out += &format!(" vregs={}", self.vregs);
        }
        if self.vcopies != 4 {
            out += &format!(" vcopies={}", self.vcopies);
        }
        out
    }

    pub fn body(&self, name: &str, level: &str) -> Result<&Body> {
        self.fns
            .get(&(name.to_string(), level.to_string()))
            .ok_or_else(|| {
                let mut have: Vec<String> =
                    self.fns.keys().map(|(n, lv)| format!("{n}.{lv}")).collect();
                have.sort();
                Error::new(
                    "L1.MOD.004",
                    format!("no fn {name}.{level} in {}; it has {have:?}", self.file),
                )
            })
    }

    pub fn levels(&self, name: &str) -> Vec<&'static str> {
        LEVELS
            .iter()
            .copied()
            .filter(|lv| self.fns.contains_key(&(name.to_string(), lv.to_string())))
            .collect()
    }

    /// The fn names, first appearance first.
    pub fn names(&self) -> Vec<String> {
        let mut seen: Vec<String> = Vec::new();
        for (n, _) in self.fns.keys() {
            if !seen.contains(n) {
                seen.push(n.clone());
            }
        }
        seen
    }

    pub fn reader(&self) -> Reader {
        Reader::new(&self.source, &self.file)
    }

    /// `name -> (params, body)` of the macros, as the expander takes them.
    pub fn macro_table(&self) -> IndexMap<String, (Vec<String>, Vec<Stmt>)> {
        self.macros
            .values()
            .map(|d| (d.name.clone(), (d.params.clone(), d.stmts.clone())))
            .collect()
    }
}

/// The parameter names of `macro NAME(a, b)` / `image NAME(a)`.
pub fn macro_params(r: &Reader, st: &Stmt, head: &Term) -> Result<Vec<String>> {
    let mut out = Vec::new();
    if on("l1.module/no-params") {
        return Ok(out);
    }
    if let Term::Call(_, args) = head {
        for p in args {
            match p {
                Term::Name(n) => out.push(n.clone()),
                _ => {
                    return Err(r.fail(
                        st,
                        "L1.MOD.005",
                        format!("a parameter is a name, got {p:?}"),
                    ))
                }
            }
        }
    }
    Ok(out)
}

/// The module of a `.ktpu` text.
pub fn read(text: &str, file: &str) -> Result<Module> {
    let r = Reader::new(text, file);
    let mut m = Module {
        source: text.to_string(),
        file: file.to_string(),
        ..Module::default()
    };
    for st in parse(text, file)? {
        let pos = st.positional();
        match st.op.as_str() {
            "target" => {
                let kw = st.kwargs();
                if m.target.is_some()
                    || pos.len() != 1
                    || pos[0].as_name().is_none()
                    || on("l1.module/target-name")
                    || kw.iter().any(|(k, _)| *k != "vregs" && *k != "vcopies")
                {
                    return Err(r.fail(
                        &st,
                        "L1.MOD.007",
                        "wanted one `target NAME [vregs=N] [vcopies=N]`",
                    ));
                }
                m.target = Some(pos[0].as_name().unwrap().to_string());
                for (key, legal) in [("vregs", VREGS), ("vcopies", VCOPIES)] {
                    if let Some(v) = st.kw(key) {
                        let n = v
                            .as_int()
                            .filter(|n| legal.contains(&(*n as u32)) && *n >= 0);
                        let Some(n) = n else {
                            return Err(r.fail(
                                &st,
                                "L1.MOD.003",
                                format!(
                                    "{key} is one of ({}, {}, {})",
                                    legal[0], legal[1], legal[2]
                                ),
                            ));
                        };
                        if key == "vregs" {
                            m.vregs = n as u32;
                        } else {
                            m.vcopies = n as u32;
                        }
                    }
                }
            }
            "fn" => {
                let body = fn_body(&r, &st, &pos)?;
                let key = (body.name.clone(), body.level.clone());
                if m.fns.contains_key(&key) {
                    return Err(r.fail(
                        &st,
                        "L1.MOD.008",
                        format!("{}.{} defined twice", body.name, body.level),
                    ));
                }
                m.fns.insert(key, body);
            }
            "image" | "macro" => {
                let name = match pos.first() {
                    Some(Term::Call(n, _)) | Some(Term::Name(n)) => n.clone(),
                    _ => {
                        return Err(r.fail(
                            &st,
                            "L1.MOD.005",
                            format!("wanted `{} NAME(params)`", st.op),
                        ))
                    }
                };
                if m.images.contains_key(&name) || m.macros.contains_key(&name) {
                    return Err(r.fail(&st, "L1.MOD.009", format!("'{name}' defined twice")));
                }
                if st.body.is_empty() {
                    return Err(r.fail(
                        &st,
                        "L1.MOD.009",
                        format!("{} {name} has no statement", st.op),
                    ));
                }
                let params = macro_params(&r, &st, pos[0])?;
                let d = Definition {
                    kind: st.op.clone(),
                    name: name.clone(),
                    params,
                    stmts: st.body.clone(),
                    line: st.line,
                };
                if st.op == "image" {
                    m.images.insert(name, d);
                } else {
                    m.macros.insert(name, d);
                }
            }
            other => {
                return Err(r.fail(
                    &st,
                    "L1.MOD.010",
                    format!("no top-level statement '{other}'; target, fn, image, macro"),
                ))
            }
        }
    }
    Ok(m)
}

fn fn_body(r: &Reader, st: &Stmt, pos: &[&Term]) -> Result<Body> {
    let form = "wanted `fn NAME.LEVEL(params) [-> type]`";
    let Some(Term::Call(head, args)) = pos.first() else {
        return Err(r.fail(st, "L1.MOD.008", form));
    };
    let (name, level) = match head.rfind('.') {
        Some(i) => (&head[..i], &head[i + 1..]),
        None => ("", head.as_str()),
    };
    let levels: &[&str] = if on("l1.module/levels") {
        &LEVELS[..1]
    } else {
        &LEVELS
    };
    if !levels.contains(&level) || name.is_empty() {
        return Err(r.fail(
            st,
            "L1.MOD.004",
            format!("a fn is named NAME.LEVEL, LEVEL one of ('l2', 'l1'); got '{head}'"),
        ));
    }
    let mut params = Vec::new();
    for a in args {
        match a {
            Term::Typed(n, t) => params.push((n.clone(), (**t).clone())),
            _ => {
                return Err(r.fail(
                    st,
                    "L1.MOD.008",
                    format!("a parameter is `name: type`, got {a:?}"),
                ))
            }
        }
    }
    let ret = if pos.len() == 3 && pos[1].is_arrow("->") {
        Some(pos[2].clone())
    } else if pos.len() != 1 {
        return Err(r.fail(st, "L1.MOD.008", form));
    } else {
        None
    };
    if st.body.is_empty() {
        return Err(r.fail(st, "L1.MOD.008", format!("fn {head} has no body")));
    }
    Ok(Body {
        name: name.to_string(),
        level: level.to_string(),
        params,
        ret,
        stmts: st.body.clone(),
        line: st.line,
    })
}
