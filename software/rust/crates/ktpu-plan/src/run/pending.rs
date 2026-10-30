//! Operands made inside the kernel that reads them (`forms.Pending`,
//! `forms.Step`, `forms.plans_of`, `forms.step_binds`, `run._f16_walk`).

use std::collections::BTreeMap;

use ktpu_kernels::gen::relayout;
use ktpu_machine::Refusal;
use serde_json::{json, Value};

use super::{item, Runner};
use crate::exec::{flat, View};
use crate::program::Addr;

pub type Walk = Vec<(i64, i64)>;

/// One relayout making a pending buffer (`forms.Step`): `n` f16 values at
/// `src` (None: the previous step's result) walked from element `at` by
/// `walk` into the `m` destination elements `dwalk` reaches from `dat`, the
/// rest 0.
#[derive(Clone, Debug, PartialEq)]
pub struct Step {
    pub src: Option<Addr>,
    pub n: i64,
    pub walk: Walk,
    pub at: i64,
    pub dwalk: Walk,
    pub dat: i64,
    pub m: i64,
}

impl Step {
    /// The `relayout.plan` call planning it (`forms.plans_of`: kept 0,
    /// from element 0, the default pull).
    pub fn made_by(&self) -> Value {
        json!({"made_by": {"gen": "relayout.plan", "args": {
            "src_n": self.n, "walk": self.walk, "at": self.at, "dwalk": self.dwalk,
            "dat": self.dat, "m": self.m, "keep": false, "z0": 0,
            "tree": relayout::PULL_TREE,
        }}})
    }
}

/// A dense f16 buffer of `nbytes` one kernel reads, not made yet: its steps
/// run inside that kernel, or as their own relayouts (`made`). `holder`
/// holds the packings made of it.
#[derive(Clone, Debug)]
pub struct Pending {
    pub nbytes: i64,
    pub steps: Vec<Step>,
    holder: String,
    storage: Option<String>,
}

/// A kernel's bound read: an address, or a pending buffer (its index in the
/// runner's; one buffer read twice is the same index).
#[derive(Clone, Debug, PartialEq)]
pub enum Bound {
    At(Addr),
    Pend(usize),
}

/// Every bind an address.
pub fn bound(binds: BTreeMap<String, Addr>) -> BTreeMap<String, Bound> {
    binds.into_iter().map(|(k, a)| (k, Bound::At(a))).collect()
}

/// `in{i}` of a binds map, by i: Python's insertion order of operands.
fn operand_order(k: &str) -> (usize, i64, String) {
    match k.strip_prefix("in").and_then(|i| i.parse::<i64>().ok()) {
        Some(i) => (0, i, String::new()),
        None => (1, 0, k.to_string()),
    }
}

impl Runner<'_> {
    /// `forms.Pending(dev, nbytes, steps)`: its holder allocated now.
    pub fn pend(&mut self, nbytes: i64, steps: Vec<Step>) -> usize {
        let holder = self.x.storage(32);
        self.pending.push(Pending {
            nbytes,
            steps,
            holder,
            storage: None,
        });
        self.pending.len() - 1
    }

    /// `Pending.made`: the buffer itself, its steps run as relayouts.
    pub fn made(&mut self, id: usize) -> Result<Addr, Refusal> {
        if let Some(s) = &self.pending[id].storage {
            return Ok(self.x.addr(s, 0));
        }
        let (nbytes, steps) = (self.pending[id].nbytes, self.pending[id].steps.clone());
        let mut prev: Option<String> = None;
        let mut temps = Vec::new();
        for (i, s) in steps.iter().enumerate() {
            let last = i == steps.len() - 1;
            let out = self
                .x
                .storage(if last { nbytes } else { (2 * s.m).max(32) });
            let src = match (&s.src, &prev) {
                (Some(a), _) => a.clone(),
                (None, Some(p)) => self.x.addr(p, 0),
                (None, None) => unreachable!("a first step reads a source"),
            };
            let dst = self.x.addr(&out, 0);
            self.relayout(src, s.n, &s.walk, s.at, &s.dwalk, s.m, dst, s.dat, false)?;
            temps.extend(prev.replace(out));
        }
        for t in temps {
            self.x.free(&t);
        }
        let p = prev.expect("a pending buffer has a step");
        self.x.wrote(&p, &flat(), false);
        self.x.store_mut(&p).dtype = Some("float16".into());
        self.pending[id].storage = Some(p.clone());
        Ok(self.x.addr(&p, 0))
    }

    /// Every pending bind made, in operand order: addresses only.
    pub fn made_all(
        &mut self,
        binds: &BTreeMap<String, Bound>,
    ) -> Result<BTreeMap<String, Addr>, Refusal> {
        let mut keys: Vec<&String> = binds.keys().collect();
        keys.sort_by_key(|k| operand_order(k));
        let mut out = BTreeMap::new();
        for k in keys {
            let a = match &binds[k] {
                Bound::At(a) => a.clone(),
                Bound::Pend(id) => self.made(*id)?,
            };
            out.insert(k.clone(), a);
        }
        Ok(out)
    }

    /// A temporary freed: a storage, or a pending buffer's holder and (once
    /// made) its storage (`Pending.free`).
    pub fn free_tmp(&mut self, t: &str) {
        let Some(p) = self.pending.iter().position(|p| p.holder == t) else {
            self.x.free(t);
            return;
        };
        self.x.free(t);
        if let Some(s) = self.pending[p].storage.clone() {
            self.x.free(&s);
        }
    }

    /// The holder naming pending buffer `id` among a run's temporaries.
    pub fn holder(&self, id: usize) -> String {
        self.pending[id].holder.clone()
    }

    /// `forms.plans_of`: the `gen::relayout` plans of `steps`, None when one
    /// is not planned (every one under the lanes-only mutant).
    pub fn plans_of(&self, steps: &[Step]) -> Result<Option<Vec<relayout::Plan>>, Refusal> {
        if ktpu_kernels::mutants::on(relayout::LANES_MUTANT) {
            return Ok(None);
        }
        let hw = relayout::Hw::of(self.m);
        let mut out = Vec::new();
        for s in steps {
            match relayout::plan(
                s.n,
                &s.walk,
                s.at,
                &s.dwalk,
                s.dat,
                s.m,
                false,
                &hw,
                0,
                relayout::PULL_TREE,
            )? {
                Ok(p) => out.push(p),
                Err(_) => return Ok(None),
            }
        }
        Ok(Some(out))
    }

    /// `forms.step_binds`: the tables of `plans` uploaded, by
    /// `relayout.step_ports` name.
    pub fn step_binds(
        &mut self,
        plans: &[relayout::Plan],
        prefix: &str,
        binds: &mut BTreeMap<String, Addr>,
    ) {
        for (i, pl) in plans.iter().enumerate() {
            let (index, mask) = self.relayout_tables(pl);
            binds.insert(format!("{prefix}index{i}"), index);
            binds.insert(format!("{prefix}mask{i}"), mask);
        }
    }

    /// `run._f16_walk`: `(address, values, walk, first, dwalk)` of `walk`
    /// from element `first` of `st` read as f16 into `dwalk` -- in its
    /// padded-rows form, strides remapped (a contiguous level of whole rows
    /// split in two, with its `dwalk` level), when that is current and no
    /// level crosses a row; else in its f16 form (made if need be; an
    /// `API.FORM` refusal when it cannot).
    #[allow(clippy::type_complexity)]
    pub fn f16_walk(
        &mut self,
        st: &str,
        dtype: &str,
        walk: &[(i64, i64)],
        first: i64,
        dwalk: &[(i64, i64)],
    ) -> Result<(Addr, i64, Walk, i64, Walk), Refusal> {
        if let Some(key) = self.padded(st) {
            let (n, np) = (key[1].as_i64().unwrap(), key[2].as_i64().unwrap());
            let (mut sw, mut sd) = (Vec::new(), Vec::new());
            for (&(c, s), &(_, d)) in walk.iter().zip(dwalk) {
                if first.rem_euclid(n) == 0 && s == 1 && c > n && c % n == 0 {
                    sw.extend([(c / n, n), (n, 1)]);
                    sd.extend([(c / n, n * d), (n, d)]);
                } else {
                    sw.push((c, s));
                    sd.push((c, d));
                }
            }
            let inner: Vec<(i64, i64)> = sw
                .iter()
                .copied()
                .filter(|(c, s)| s.rem_euclid(n) != 0 && *c > 1)
                .collect();
            let span = first.rem_euclid(n) + inner.iter().map(|(c, s)| s * (c - 1)).sum::<i64>();
            if inner.iter().all(|(_, s)| *s > 0) && span < n {
                let fbytes = self
                    .x
                    .store(st)
                    .forms
                    .iter()
                    .find(|(k, _)| *k == key)
                    .map(|(_, b)| *b)
                    .expect("a current form is allocated");
                let remap = sw
                    .iter()
                    .map(|&(c, s)| {
                        if s.rem_euclid(n) != 0 {
                            (c, s)
                        } else {
                            (c, s.div_euclid(n) * np)
                        }
                    })
                    .collect();
                let at = first.div_euclid(n) * np + first.rem_euclid(n);
                let addr = Addr {
                    buf: st.into(),
                    form: key,
                    offset: 0,
                };
                return Ok((addr, fbytes / 2, remap, at, sd));
            }
        }
        let nbytes = self.x.store(st).nbytes;
        let total = nbytes / item(dtype);
        let whole = View {
            base: st.into(),
            offset: 0,
            nbytes,
        };
        let at = self.f16_of(&whole, dtype, total)?;
        Ok((at, total, walk.to_vec(), first, dwalk.to_vec()))
    }
}

/// Whether a refusal is `forms.FormError`'s: no f16 form to read.
pub fn form_error(e: &Refusal) -> bool {
    e.clause.starts_with("API.FORM")
}
