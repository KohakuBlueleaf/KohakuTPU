//! The candidate search of a vector instance's image (L2.SEARCH.001-.004):
//! every band shape and the modulo schedule, the fewest cycles under the
//! core's timing that encodes into the image room. Candidates lower and time
//! in parallel; the choice is the first by (cycles, order tried), so it does
//! not depend on the thread count.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use indexmap::IndexMap;
use ktpu_l1::text::{Env, Module};
use rayon::prelude::*;

use super::ctx::Ctx;
use candidate::lower_band;
use ktpu_l1::sched::schedule::whole;
mod candidate;
use super::modulo as MO;
use super::range::{self, Ranges};
use super::sign::ZeroSign;
use super::{banks, encode, numbers, ops_of, renamed, split, Plan, RPar, VErr, VResult};
use crate::ir::{Body, Node, Result};
use crate::lower::fills::throttled;
use crate::lower::sync::Access;
use crate::mutant::on;
use crate::target::{Target, IMEM_ROOM};

/// How a stream loop lowers: "band" (one trip in one L1 tile), "modulo"
/// (trips software-pipelined) or "auto" (the fewer modelled cycles).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Strategy {
    Auto,
    Band,
    Modulo,
}

impl Strategy {
    pub fn parse(s: &str) -> Option<Strategy> {
        match s {
            "auto" => Some(Strategy::Auto),
            "band" => Some(Strategy::Band),
            "modulo" => Some(Strategy::Modulo),
            _ => None,
        }
    }
}

/// The most choices kept (by one candidate's image, name blanked).
const CHOSEN_KEPT: usize = 4096;
/// A search's result under the name `\0`: text, continuations, cycles.
type Chosen = (String, Vec<(String, String)>, i64);

/// Continuations named after `from` under the name `to`.
fn parts_named(parts: &[(String, String)], from: &str, to: &str) -> Vec<(String, String)> {
    parts
        .iter()
        .map(|(name, text)| {
            let name = format!("{to}{}", &name[from.len()..]);
            let text = renamed(text, &name);
            (name, text)
        })
        .collect()
}

type Key = (
    String,
    Option<i64>,
    u32,
    u32,
    (i64, i64),
    Strategy,
    i64,
    (Option<bool>, Option<bool>),
);

/// A band image as its fit sees it: the machine's digest, the name-blanked
/// text (the name only selects the image) and the params.
type Image = ([u8; 32], String, usize);

/// Each band image's scheduled IMEM words (None: it does not encode), each
/// made by the first thread to ask.
#[derive(Debug, Default)]
pub struct Fits {
    whole: Mutex<HashMap<Image, Arc<OnceLock<bool>>>>,
    /// Images scheduled and encoded so far.
    made: AtomicUsize,
}

impl Fits {
    /// Whether `module`, throttled, stands within the image room at price 0,
    /// each distinct image made once (`l2.vector/fit-each`: every call apart).
    pub fn whole(&self, module: &Module, nparams: usize, t: &Target) -> bool {
        let make = || {
            self.made.fetch_add(1, Ordering::Relaxed);
            crate::work::FITTED.fetch_add(1, Ordering::Relaxed);
            throttled(module, t)
                .and_then(|m| whole(&m, m.vcopies, &t.machine))
                .unwrap_or(false)
        };
        if on("l2.vector/fit-each") {
            return make();
        }
        let key = (
            t.machine.digest_bytes(),
            renamed(&module.source, "\0"),
            nparams,
        );
        let cell = self.whole.lock().unwrap().entry(key).or_default().clone();
        *cell.get_or_init(make)
    }

    /// Distinct images scheduled and encoded so far.
    pub fn made(&self) -> usize {
        self.made.load(Ordering::Relaxed)
    }
}

/// A search's settings and the choices it remembers.
#[derive(Debug)]
pub struct Search {
    pub strategy: Strategy,
    /// Every lowering's `hold` (constants past the scalar file held in
    /// registers) and `pin` (rows unpacked before the loop kept in registers)
    /// when given; the search tries both where they apply when not.
    pub hold: Option<bool>,
    pub pin: Option<bool>,
    /// What range analysis may prove of EXP2 / EXP2D arguments, dropping
    /// the guard checks it covers.
    pub ranges: Ranges,
    /// How the lowering moves a value it does not change (ISA.VC.058).
    pub zero_sign: ZeroSign,
    chosen: Mutex<IndexMap<Key, Chosen>>,
    recording: bool,
    frozen: AtomicBool,
    retile_choices: Mutex<Vec<(i64, i64)>>,
    /// Distinct candidate images timed so far.
    timed: AtomicUsize,
    /// Each band image's IMEM words, for its serial fallback.
    pub fits: Fits,
}

impl Default for Search {
    fn default() -> Search {
        Search::new(Strategy::Auto)
    }
}

/// The chosen image: its text, run parameters, cycles and accesses.
#[derive(Clone, Debug)]
pub struct Best {
    pub text: String,
    /// `(name, text)` of each continuation of a split image (else empty).
    pub parts: Vec<(String, String)>,
    pub params: Vec<String>,
    pub cycles: i64,
    pub accesses: Vec<Access>,
}

type Cand = (std::sync::Arc<Module>, Vec<String>, Vec<Access>);

impl Search {
    pub fn new(strategy: Strategy) -> Search {
        Search {
            strategy,
            hold: None,
            pin: None,
            ranges: Ranges::DEFAULT,
            zero_sign: ZeroSign::DEFAULT,
            chosen: Mutex::new(IndexMap::new()),
            recording: false,
            frozen: AtomicBool::new(false),
            retile_choices: Mutex::new(Vec::new()),
            timed: AtomicUsize::new(0),
            fits: Fits::default(),
        }
    }

    /// Record representative image and retile choices without cache eviction.
    pub fn recording() -> Self {
        Self {
            recording: true,
            ..Self::default()
        }
    }

    /// Subsequent compiles may only use the representative's cached images.
    pub fn freeze(&self) {
        self.frozen.store(true, Ordering::Relaxed);
    }

    pub fn frozen(&self) -> bool {
        self.frozen.load(Ordering::Relaxed)
    }

    fn pressure_fallback(&self) -> bool {
        !self.recording && !self.frozen()
    }

    pub fn retile_choice(&self, index: usize) -> Option<(i64, i64)> {
        self.retile_choices.lock().unwrap().get(index).copied()
    }

    pub fn remember_retile(&self, index: usize, columns: i64, rows: i64) {
        if self.recording && !self.frozen() {
            let mut choices = self.retile_choices.lock().unwrap();
            if choices.len() <= index {
                choices.resize(index + 1, (0, 0));
            }
            choices[index] = (columns, rows);
        }
    }

    pub fn retile_count(&self) -> usize {
        self.retile_choices.lock().unwrap().len()
    }

    #[allow(clippy::too_many_arguments)]
    fn band(
        &self,
        body: &Body,
        par: &RPar,
        env: &Env,
        name: &str,
        t: &Target,
        band: usize,
        remat: bool,
        ahead: bool,
        plan: &Plan,
        land: Option<i64>,
        hold: bool,
        pin: bool,
    ) -> VResult<Cand> {
        lower_band(
            body, par, env, name, t, band, remat, ahead, plan, land, hold, pin, &self.fits,
        )
    }

    /// This search with every lowering's `hold` and `pin` given.
    pub fn forced(mut self, hold: Option<bool>, pin: Option<bool>) -> Search {
        (self.hold, self.pin) = (hold, pin);
        self
    }

    /// This search with range analysis `ranges`.
    pub fn ranged(mut self, ranges: Ranges) -> Search {
        self.ranges = ranges;
        self
    }

    /// This search with zero-sign handling `zero_sign`.
    pub fn signed(mut self, zero_sign: ZeroSign) -> Search {
        self.zero_sign = zero_sign;
        self
    }

    fn pin_given(&self) -> Option<bool> {
        self.pin.filter(|_| !on("l2.vector/pin-unforced"))
    }

    fn hold_given(&self) -> Option<bool> {
        self.hold.filter(|_| !on("l2.vector/hold-unforced"))
    }

    /// Distinct candidate images timed so far.
    pub fn timed(&self) -> usize {
        self.timed.load(Ordering::Relaxed)
    }

    /// Every choice remembered, oldest first: `(image, name blanked; cycles)`.
    pub fn chosen(&self) -> Vec<(String, i64)> {
        let chosen = self.chosen.lock().unwrap();
        chosen.values().map(|c| (c.0.clone(), c.2)).collect()
    }

    /// The search's choice, remembered by the image one candidate makes: a
    /// shape seen before is not searched again.
    #[allow(clippy::too_many_arguments)]
    pub fn best(
        &self,
        body: &Body,
        par: &RPar,
        env: &Env,
        name: &str,
        plan: &Plan,
        land: Option<i64>,
        t: &Target,
    ) -> Result<Best> {
        let par = &RPar {
            guards: range::guards(par, self.ranges),
            zero_sign: self.zero_sign,
            ..par.clone()
        };
        if par.st.kw("classed").is_some() {
            let band = match par.st.kw("class_band") {
                Some(value) => crate::ir::const_of(body, &par.st, value, env)?,
                None => 1,
            };
            if ![1, 2, 4, 8].contains(&band) {
                return Err(value_error(body, par, "class_band is 1, 2, 4 or 8".into()));
            }
            let band = if on("l2.class/serial-band") {
                1
            } else {
                band as usize
            };
            let (module, params, accesses) = lower_band(
                body, par, env, name, t, band, false, false, plan, land, true, true, &self.fits,
            )
            .map_err(|e| match e {
                VErr::Fail(e) => e,
                VErr::Pressure => {
                    value_error(body, par, "class band exceeds register pressure".into())
                }
                VErr::Value(m) => value_error(body, par, m),
            })?;
            return Ok(Best {
                text: module.source.clone(),
                parts: Vec::new(),
                params,
                accesses,
                cycles: 0,
            });
        }
        let (hold, pin) = (
            self.hold_given().unwrap_or(true),
            self.pin_given().unwrap_or(true),
        );
        let probe = self.band(
            body, par, env, name, t, 1, false, true, plan, land, hold, pin,
        );
        let probe = match probe {
            Ok(p) => p,
            Err(VErr::Pressure) if !self.pressure_fallback() => {
                return Err(ktpu_l1::Error::new(
                    "L2.TEMPLATE.CHOICE",
                    "template probe exceeds register pressure and has no replayable cache key",
                ));
            }
            Err(VErr::Pressure) => return self.search(body, par, env, name, plan, land, t),
            Err(VErr::Fail(e)) => return Err(e),
            Err(VErr::Value(m)) => return Err(value_error(body, par, m)),
        };
        let key: Key = (
            renamed(&probe.0.source, "\0"),
            land,
            body.vregs,
            body.vcopies,
            body.fill,
            self.strategy,
            IMEM_ROOM,
            (self.hold, self.pin),
        );
        let got = match on("l2.vector/search-again") {
            true => None,
            false => self.chosen.lock().unwrap().get(&key).cloned(),
        };
        if let Some((text, parts, cycles)) = got {
            let mut ch = self.chosen.lock().unwrap();
            if let Some(i) = ch.get_index_of(&key) {
                let last = ch.len() - 1;
                ch.move_index(i, last);
            }
            return Ok(Best {
                text: renamed(&text, name),
                parts: parts_named(&parts, "\0", name),
                params: probe.1,
                cycles,
                accesses: probe.2,
            });
        }
        if self.frozen() {
            return Err(ktpu_l1::Error::new(
                "L2.TEMPLATE.CHOICE",
                format!("{name}: no representative image for this probe; split the class"),
            ));
        }
        let best = self.search(body, par, env, name, plan, land, t)?;
        let mut ch = self.chosen.lock().unwrap();
        let parts = parts_named(&best.parts, name, "\0");
        ch.insert(key, (renamed(&best.text, "\0"), parts, best.cycles));
        if !self.recording && ch.len() > CHOSEN_KEPT {
            ch.shift_remove_index(0);
        }
        Ok(best)
    }

    #[allow(clippy::too_many_arguments)]
    fn search(
        &self,
        body: &Body,
        par: &RPar,
        env: &Env,
        name: &str,
        plan: &Plan,
        land: Option<i64>,
        t: &Target,
    ) -> Result<Best> {
        let mut shapes: Vec<(usize, bool, bool, bool, bool)> = Vec::new();
        if self.strategy != Strategy::Modulo {
            let (pre, lp) = split(body, par)?;
            let mut all = pre.clone();
            if let Some(lp) = &lp {
                all.extend(ops_of(&lp.body));
            }
            let many = numbers(&all, body, &par.guards).len() as i64 > t.stk_s();
            let rows = pre.iter().any(|u| {
                matches!(u.as_ref(), Node::Op(o) if o.op == "unpack" && o.ty.as_ref().map(|x| x.shape.len() == 2).unwrap_or(false))
            });
            let holds: &[bool] = if many { &[true, false] } else { &[true] };
            let pins: &[bool] = if rows && lp.is_some() {
                &[true, false]
            } else {
                &[true]
            };
            for band in [1, 2, 4, 8, 16] {
                for remat in [false, true] {
                    for ahead in [true, false] {
                        for &hold in holds {
                            for &pin in pins {
                                let (hold, pin) = (
                                    self.hold_given().unwrap_or(hold),
                                    self.pin_given().unwrap_or(pin),
                                );
                                shapes.push((band, remat, ahead, hold, pin));
                            }
                        }
                    }
                }
            }
        }
        // Timing is a function of image text; duplicate text shares the first Module.
        // `l2.vector/search-each` times every candidate apart.
        let each = on("l2.vector/search-each");
        let timed = |module: &std::sync::Arc<Module>| -> Result<(std::sync::Arc<Module>, i64)> {
            let c = encode::cycles(
                module,
                name,
                body.vregs,
                body.vcopies,
                body.fill,
                &t.machine,
            )?;
            self.timed.fetch_add(1, Ordering::Relaxed);
            crate::work::TIMED.fetch_add(1, Ordering::Relaxed);
            Ok((
                module.clone(),
                if on("l2.vector/slowest-first") { -c } else { c },
            ))
        };
        let active = crate::mutant::current();
        let lowered: Vec<VResult<Cand>> = shapes
            .par_iter()
            .map(|&(band, remat, ahead, hold, pin)| {
                crate::mutant::scoped(active, || {
                    self.band(
                        body, par, env, name, t, band, remat, ahead, plan, land, hold, pin,
                    )
                })
            })
            .collect();
        // `slot[i]`: candidate i's place in `texts`, the texts to time.
        let mut texts: Vec<&std::sync::Arc<Module>> = Vec::new();
        let mut first: IndexMap<&str, usize> = IndexMap::new();
        let mut slot: Vec<usize> = Vec::new();
        for got in &lowered {
            let Ok(c) = got else {
                slot.push(usize::MAX);
                continue;
            };
            let at = match first.get(c.0.source.as_str()) {
                Some(&k) if !each => k,
                _ => {
                    first.entry(c.0.source.as_str()).or_insert(texts.len());
                    texts.push(&c.0);
                    texts.len() - 1
                }
            };
            slot.push(at);
        }
        let timings: Vec<Result<(std::sync::Arc<Module>, i64)>> = texts
            .par_iter()
            .map(|text| crate::mutant::scoped(active, || timed(text)))
            .collect();
        let mut cands: Vec<(Cand, i64)> = Vec::new();
        let mut timing_refusal = None;
        for (got, &k) in lowered.iter().zip(&slot) {
            match got {
                Ok(c) => match &timings[k] {
                    Ok((_, cycles)) => cands.push((c.clone(), *cycles)),
                    Err(e) if e.clause == "ISA.VC.043" => {
                        timing_refusal = Some(e.clone());
                    }
                    Err(e) => return Err(e.clone()),
                },
                Err(VErr::Pressure) => continue,
                Err(VErr::Fail(e)) => return Err(e.clone()),
                Err(VErr::Value(m)) => return Err(value_error(body, par, m.clone())),
            }
        }
        let mut read: IndexMap<String, std::sync::Arc<Module>> = IndexMap::new();
        for (text, got) in texts.iter().zip(timings) {
            if let Ok((module, _)) = got {
                read.entry(text.source.clone()).or_insert(module);
            }
        }
        let mut why = None;
        if self.strategy != Strategy::Band && !on("l2.vector/no-modulo") {
            match candidate::modulo(
                body,
                par,
                env,
                name,
                plan,
                land,
                t,
            ) {
                Ok(Ok(c)) => {
                    let cycles = match cands.iter().find(|(d, _)| d.0.source == c.0.source) {
                        Some((_, cycles)) if !each => *cycles,
                        _ => {
                            let (module, cycles) = timed(&c.0)?;
                            read.entry(c.0.source.clone()).or_insert(module);
                            cycles
                        }
                    };
                    cands.push((c, cycles));
                }
                Ok(Err(w)) => why = Some(w),
                Err(VErr::Pressure) => {}
                Err(VErr::Fail(e)) => return Err(e),
                Err(VErr::Value(m)) => return Err(value_error(body, par, m)),
            }
        }
        if cands.is_empty() && !on("l2.vector/registers-unrefused") {
            if let Some(e) = timing_refusal {
                return Err(e);
            }
            let extra = match (self.strategy, why) {
                (Strategy::Modulo, Some(w)) => format!(" (modulo: {w})"),
                (Strategy::Modulo, None) => " (modulo: None)".to_string(),
                _ => String::new(),
            };
            return Err(body.reader.fail(
                &par.st,
                "L2.VC.017",
                format!(
                    "the stream loop needs more than {} vector registers{extra}",
                    body.vregs
                ),
            ));
        }
        cands.sort_by_key(|c| c.1);
        let mut whys: Vec<String> = Vec::new();
        let mut tried: Vec<(String, usize)> = Vec::new();
        // The first image that fits, past candidates its schedule outgrows,
        // and the cheapest candidate that fits only cut into continuations.
        let (mut fit, mut cut): (Option<Best>, Option<Best>) = (None, None);
        let splits = t.read.split_every_image || crate::lower::readmap::reads_mapped(&par.st);
        for ((candidate, params, accesses), cycles) in cands {
            let text = &candidate.source;
            // A text tried before encodes as it did: its words are in `whys`.
            let key = (text.clone(), params.len());
            if !each && tried.contains(&key) {
                continue;
            }
            tried.push(key);
            let module = match read.get(text) {
                Some(m) if !each => m,
                _ => &candidate,
            };
            let n = params.len();
            let (fill, copies) = (body.fill, body.vcopies);
            let image = throttled(module, t)?;
            match super::decompose::split(&image, name, n, t, cycles, splits, fill, copies) {
                Err(e) => {
                    let w = e.message.lines().next().unwrap_or("").to_string();
                    if !whys.contains(&w) {
                        whys.push(w);
                    }
                }
                Ok(split) if split.parts.is_empty() => {
                    let best = Best {
                        text: text.clone(),
                        parts: split.parts,
                        params,
                        cycles: split.cycles,
                        accesses,
                    };
                    if on("l2.vector/first-fit") || self.fits.whole(module, n, t) {
                        fit = Some(best);
                        break;
                    }
                    fit.get_or_insert(best);
                }
                Ok(split) => {
                    if cut.as_ref().is_none_or(|b| split.cycles < b.cycles) {
                        let parts = split.parts.iter().map(|p| p.1.as_str());
                        cut = Some(Best {
                            text: parts.collect::<Vec<_>>().join("\n"),
                            parts: split.parts,
                            params,
                            cycles: split.cycles,
                            accesses,
                        });
                    }
                }
            }
        }
        match (fit, cut) {
            (Some(f), Some(c)) => return Ok(if c.cycles < f.cycles { c } else { f }),
            (Some(best), None) | (None, Some(best)) => return Ok(best),
            (None, None) => {}
        }
        Err(body.reader.fail(
            &par.st,
            "L2.VC.017",
            format!("no image of the stream loop encodes: {}", whys.join("; ")),
        ))
    }
}

/// A shape the lowering cannot take (Python's ValueError): no position.
fn value_error(_body: &Body, _par: &RPar, m: String) -> ktpu_l1::Error {
    ktpu_l1::Error::new("L2.REF.002", m)
}

/// The first line of an error as Python's `str(TextError)` prints it.
fn first_line(e: &ktpu_l1::Error) -> String {
    let full = if e.line > 0 {
        format!("{}:{}:{}: {}", e.file, e.line, e.col, e.message)
    } else {
        format!("{}: {}", e.file, e.message)
    };
    full.lines().next().unwrap_or("").to_string()
}
