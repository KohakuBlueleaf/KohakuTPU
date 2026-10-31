//! graph -> L2 -> passes -> L1 -> schedule -> L0 -> verify, each stage
//! through its cache; `compile_many` over a rayon pool.
//!
//! A stage's key is its input's text form (L2 / L1 text, graph JSON) with
//! the kernel name, the machine digest and the stage's version, so an entry
//! is reused whatever produced its input. A hit hands the next stage text; it
//! is parsed only when that stage misses.

pub mod cache;
pub mod stage;

use crate::output::{Compiled, Finding, Input, PackageOut, Refused, Scratch, Verdict};
use cache::{Cache, Key};
use ktpu_machine::{Machine, Refusal};
use rayon::prelude::*;
use stage::{Backend, Stage};
use std::collections::BTreeMap;

/// A stage's output: its text form, and the typed value when this compile
/// made it (a cache hit has only the text).
struct Lazy<T> {
    text: String,
    typed: Option<T>,
}

impl<T> Lazy<T> {
    fn get(&mut self, parse: impl FnOnce(&str) -> Result<T, Refusal>) -> Result<&T, Refusal> {
        if self.typed.is_none() {
            self.typed = Some(parse(&self.text)?);
        }
        Ok(self.typed.as_ref().expect("set above"))
    }
}

/// One compile: the backend, machine and cache it runs against.
struct Run<'a, B: Backend> {
    b: &'a B,
    m: &'a Machine,
    cache: &'a Cache,
    digest: [u8; 32],
}

fn at(stage: Stage) -> impl Fn(Refusal) -> Refused {
    move |refusal| Refused { stage, refusal }
}

impl<B: Backend> Run<'_, B> {
    /// The cached output of `stage` for `input`, or `make`'s (stored).
    fn cached<U>(
        &self,
        stage: Stage,
        input: &[u8],
        make: impl FnOnce() -> Result<(Vec<u8>, U), Refusal>,
    ) -> Result<(Vec<u8>, Option<U>), Refused> {
        let Some(version) = self.b.version(stage) else {
            let (bytes, typed) = make().map_err(at(stage))?;
            return Ok((bytes, Some(typed)));
        };
        let key = Key::of(stage.name(), &version, &self.digest, input);
        // An I/O error reading is a miss (an entry another process holds
        // open); writing, a later miss (another process's entry stays).
        let hit = match self.cache.get(stage.name(), &key) {
            Ok(hit) => hit,
            Err(e) if crate::mutant::active() == Some(CACHE_ERRORS_REFUSE) => {
                return Err(at(stage)(Refusal::new("STACK.CACHE", e.to_string())));
            }
            Err(_) => None,
        };
        if let Some(hit) = hit {
            return match refusal_of(&hit) {
                Some(r) => Err(at(stage)(r)),
                None => Ok((hit, None)),
            };
        }
        let (out, entry) = match make() {
            Ok((bytes, typed)) => {
                let entry = bytes.clone();
                (Ok((bytes, Some(typed))), entry)
            }
            Err(r) => {
                let entry = refusal_entry(&r);
                (Err(at(stage)(r)), entry)
            }
        };
        if let Err(e) = self.cache.put(stage.name(), &key, &entry) {
            if crate::mutant::active() == Some(CACHE_ERRORS_REFUSE) {
                return Err(at(stage)(Refusal::new("STACK.CACHE", e.to_string())));
            }
        }
        out
    }

    /// A stage whose output is text; `input` is its key input (`keyed`).
    fn text<U>(
        &self,
        stage: Stage,
        input: &[u8],
        make: impl FnOnce() -> Result<(String, U), Refusal>,
    ) -> Result<Lazy<U>, Refused> {
        let (bytes, typed) = self.cached(stage, input, || {
            make().map(|(text, typed)| (text.into_bytes(), typed))
        })?;
        let text = String::from_utf8(bytes).map_err(|_| Refused {
            stage,
            refusal: Refusal::new("STACK.CACHE", "a cached text is not UTF-8"),
        })?;
        Ok(Lazy { text, typed })
    }

    fn compile(&self, input: &Input, name: Option<&str>) -> Result<Compiled, Refused> {
        let (l2, name, l1, caller) = match input {
            Input::L1(text) => {
                let name = name.ok_or_else(|| Refused {
                    stage: Stage::Order,
                    refusal: Refusal::new("STACK.INPUT", "an L1 input names the fn it builds"),
                })?;
                let l1 = Lazy {
                    text: text.clone(),
                    typed: None,
                };
                (String::new(), name.to_string(), l1, None)
            }
            _ => {
                let (l2, name, l1, caller) = self.to_l1(input, name)?;
                (l2, name, l1, Some(caller))
            }
        };
        self.packages(l2, name, l1, caller)
    }

    /// Graph or L2 input through the passes, the lowering and the image
    /// scheduler: ``(L2 text, fn name, L1, the fn's parameters)``.
    fn to_l1(&self, input: &Input, name: Option<&str>) -> Result<ToL1<B::L1>, Refused> {
        let (b, m) = (self.b, self.m);
        let mut l2: Lazy<B::L2> = match input {
            Input::L2(text) => Lazy {
                text: text.clone(),
                typed: None,
            },
            Input::L1(_) => unreachable!("an L1 input skips the L2 stages"),
            Input::Graph(json) => self.text(Stage::Graph, &keyed(json, ""), || {
                let t = b.graph_to_l2(json, m)?;
                Ok((b.print_l2(&t), t))
            })?,
        };
        let name = match name {
            Some(n) => n.to_string(),
            None => {
                let t = l2.get(|s| b.parse_l2(s)).map_err(at(Stage::Passes))?;
                b.l2_name(t).map_err(at(Stage::Passes))?
            }
        };
        let caller = {
            let t = l2.get(|s| b.parse_l2(s)).map_err(at(Stage::Passes))?;
            b.caller(t, &name).map_err(at(Stage::Passes))?
        };
        let key = keyed(&l2.text, &name);
        let mut passed: Lazy<B::L2> = self.text(Stage::Passes, &key, || {
            let t = b.passes(l2.get(|s| b.parse_l2(s))?, &name, m)?;
            Ok((b.print_l2(&t), t))
        })?;
        let key = keyed(&passed.text, &name);
        let mut lowered: Lazy<B::L1> = self.text(Stage::Lower, &key, || {
            let t = b.lower(passed.get(|s| b.parse_l2(s))?, &name, m)?;
            Ok((b.print_l1(&t), t))
        })?;
        let key = keyed(&lowered.text, &name);
        let l1: Lazy<B::L1> = self.text(Stage::Schedule, &key, || {
            let t = b.schedule(lowered.get(|s| b.parse_l1(s))?, m)?;
            Ok((b.print_l1(&t), t))
        })?;
        Ok((l2.text, name, l1, caller))
    }

    /// L1 to each node's package, its arenas and the checker's verdict.
    fn packages(
        &self,
        l2: String,
        name: String,
        mut l1: Lazy<B::L1>,
        caller: Option<Vec<String>>,
    ) -> Result<Compiled, Refused> {
        let (b, m) = (self.b, self.m);
        let caller = caller.as_deref();
        // The programs, built once if the order or the verify stage misses.
        let mut progs: Option<B::Programs> = None;
        // The arenas follow from the caller's parameters: part of the key.
        let mut key = keyed(&l1.text, &name);
        if let Some(c) = caller {
            key.extend(c.join("\0").bytes());
        }
        let order = self.cached(Stage::Order, &key, || {
            let p = built(b, m, &name, caller, &mut progs, &mut l1)?;
            let out = Ordered {
                packages: b.order(p, m)?,
                scratch: b.scratch(p),
            };
            Ok((json(&out), ()))
        });
        // Not relocatable (STACK.RELOC): no packages; `bound` says why.
        let (ordered, bound) = match order {
            Ok((bytes, _)) => (unjson(Stage::Order, &bytes)?, None),
            Err(r) if r.refusal.clause == "STACK.RELOC" => {
                (Ordered::default(), Some(r.refusal.message.clone()))
            }
            Err(r) => return Err(r),
        };
        let (bytes, _) = self.cached(Stage::Verify, &key, || {
            let p = built(b, m, &name, caller, &mut progs, &mut l1)?;
            Ok((json(&verdict(b, b.check(p, m)?)), ()))
        })?;
        let verdict: Verdict = unjson(Stage::Verify, &bytes)?;
        Ok(Compiled {
            name,
            l2,
            l1: l1.text,
            packages: ordered.packages,
            scratch: ordered.scratch,
            verdict,
            bound,
        })
    }
}

/// `to_l1`'s output: the L2 text, the fn name, its L1 and its parameters.
type ToL1<L1> = (String, String, Lazy<L1>, Vec<String>);

/// The order stage's output.
#[derive(Default, serde::Serialize, serde::Deserialize)]
struct Ordered {
    packages: BTreeMap<u32, PackageOut>,
    scratch: Vec<Scratch>,
}

fn built<'p, B: Backend>(
    b: &B,
    m: &Machine,
    name: &str,
    caller: Option<&[String]>,
    progs: &'p mut Option<B::Programs>,
    l1: &mut Lazy<B::L1>,
) -> Result<&'p B::Programs, Refusal> {
    if progs.is_none() {
        *progs = Some(b.programs(l1.get(|s| b.parse_l1(s))?, name, caller, m)?);
    }
    Ok(progs.as_ref().expect("set above"))
}

/// The mutant under which a cache entry that cannot be read or written
/// refuses the compile.
pub const CACHE_ERRORS_REFUSE: &str = "ktpu.pipeline/cache_errors_refuse";

/// A cached refusal: a NUL (no stage output, text or JSON, starts with one),
/// `refused`, then the clause and the message NUL-separated.
const REFUSED: &[u8] = b"\0refused\0";

fn refusal_entry(r: &Refusal) -> Vec<u8> {
    let mut out = REFUSED.to_vec();
    out.extend(r.clause.bytes());
    out.push(0);
    out.extend(r.message.bytes());
    out
}

fn refusal_of(entry: &[u8]) -> Option<Refusal> {
    let rest = entry.strip_prefix(REFUSED)?;
    let text = String::from_utf8_lossy(rest);
    let (clause, message) = text.split_once('\0')?;
    Some(Refusal::new(clause, message))
}

/// A stage input with the kernel name it is compiled for.
fn keyed(text: &str, name: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len() + name.len() + 1);
    out.extend_from_slice(name.as_bytes());
    out.push(0);
    out.extend_from_slice(text.as_bytes());
    out
}

fn verdict<B: Backend>(b: &B, findings: Vec<Finding>) -> Verdict {
    Verdict {
        refused: findings.iter().any(|f| b.refuses(f)),
        findings,
    }
}

fn json<T: serde::Serialize>(v: &T) -> Vec<u8> {
    serde_json::to_vec(v).expect("plain data serialises")
}

fn unjson<T: serde::de::DeserializeOwned>(stage: Stage, bytes: &[u8]) -> Result<T, Refused> {
    serde_json::from_slice(bytes).map_err(|e| Refused {
        stage,
        refusal: Refusal::new("STACK.CACHE", format!("a cached entry does not read: {e}")),
    })
}

/// `input` through every stage of `b` for machine `m`. `name` picks the
/// kernel (the module's first fn when None).
pub fn compile<B: Backend>(
    b: &B,
    input: &Input,
    name: Option<&str>,
    m: &Machine,
    cache: &Cache,
) -> Result<Compiled, Refused> {
    let run = Run {
        b,
        m,
        cache,
        digest: m.digest_bytes(),
    };
    run.compile(input, name)
}

/// Every input compiled in parallel on the current rayon pool; result `i` is
/// input `i`'s, whatever the thread count.
pub fn compile_many<B: Backend>(
    b: &B,
    inputs: &[(Input, Option<String>)],
    m: &Machine,
    cache: &Cache,
) -> Vec<Result<Compiled, Refused>> {
    inputs
        .par_iter()
        .map(|(input, name)| compile(b, input, name.as_deref(), m, cache))
        .collect()
}

/// [`compile_many`] with each input on its own machine.
pub fn compile_each<B: Backend>(
    b: &B,
    inputs: &[(Input, Option<String>, &Machine)],
    cache: &Cache,
) -> Vec<Result<Compiled, Refused>> {
    inputs
        .par_iter()
        .map(|(input, name, m)| compile(b, input, name.as_deref(), m, cache))
        .collect()
}
