//! The storages a plan runs against and the steps it takes
//! (`runtime/storage.py`, `runtime/forms.py`): each base storage's record
//! (valid forms, side forms, host bytes, element type), simulated as the
//! Python driver keeps it, every allocation, form, table, write and launch
//! a step at a symbolic address.

use std::collections::BTreeMap;

use serde_json::{json, Value};

use crate::input::{Buffer, Digest, State};
use crate::program::{Addr, Array, Call, Launch, Step};

pub fn flat() -> Value {
    json!(["flat"])
}

pub fn f16() -> Value {
    json!(["f16"])
}

/// `forms.rows_key`: rows of n f16 values np apart.
pub fn rows_key(n: i64, np: i64) -> Value {
    if crate::mutant::on("tg.run/no_padded_out") {
        return json!(["rows", n, np + 32]);
    }
    json!(["rows", n, np])
}

/// One base storage's record.
#[derive(Clone, Debug, PartialEq)]
pub struct Store {
    pub nbytes: i64,
    pub dtype: Option<String>,
    pub valid: Vec<Value>,
    /// side forms allocated: (key, bytes), in allocation order
    pub forms: Vec<(Value, i64)>,
    pub host: Option<Digest>,
    /// a storage over another's form (`owned=False`): its flat bytes are there
    pub alias: Option<Addr>,
    pub live: bool,
}

impl Store {
    pub fn new(nbytes: i64) -> Store {
        Store {
            nbytes,
            dtype: None,
            valid: vec![flat()],
            forms: Vec::new(),
            host: None,
            alias: None,
            live: true,
        }
    }

    pub fn has(&self, key: &Value) -> bool {
        self.valid.contains(key)
    }

    fn record(&self) -> Buffer {
        let mut valid = self.valid.clone();
        valid.sort_by_key(repr);
        let mut forms = self.forms.clone();
        forms.sort_by_key(|(k, n)| repr(&json!([k, n])));
        Buffer {
            nbytes: self.nbytes,
            dtype: self.dtype.clone(),
            valid,
            forms,
            host: self.host.clone(),
        }
    }
}

/// Python's `repr` order of the plain JSON a record sorts by.
pub(crate) fn repr(v: &Value) -> String {
    match v {
        Value::String(s) => format!("'{s}'"),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("'{k}': {}", repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Null => "None".into(),
        n => n.to_string(),
    }
}

/// A view of a storage: its base, byte offset, size (`StorageView`).
#[derive(Clone, Debug, PartialEq)]
pub struct View {
    pub base: String,
    pub offset: i64,
    pub nbytes: i64,
}

/// The run's storages and the steps taken.
#[derive(Clone, Debug, Default)]
pub struct Exec {
    pub stores: BTreeMap<String, Store>,
    pub steps: Vec<Step>,
    /// the named paths taken (`plan_dump.PATHS`, `rows.partials`)
    pub paths: std::collections::BTreeSet<&'static str>,
    ntmp: usize,
    nraw: usize,
}

impl Exec {
    /// The storages of `state`, temporaries named from `tmp` on.
    pub fn new(state: &State) -> Exec {
        let mut stores = BTreeMap::new();
        for (name, b) in &state.buffers {
            stores.insert(
                name.clone(),
                Store {
                    nbytes: b.nbytes,
                    dtype: b.dtype.clone(),
                    valid: b.valid.clone(),
                    forms: b.forms.clone(),
                    host: b.host.clone(),
                    alias: None,
                    live: true,
                },
            );
        }
        Exec {
            stores,
            ..Exec::default()
        }
    }

    /// The bases' records, as the golden's `end`.
    pub fn records(&self, names: &[String]) -> BTreeMap<String, Buffer> {
        names
            .iter()
            .filter_map(|n| self.stores.get(n).map(|s| (n.clone(), s.record())))
            .collect()
    }

    pub fn store(&self, name: &str) -> &Store {
        &self.stores[name]
    }

    pub fn store_mut(&mut self, name: &str) -> &mut Store {
        self.stores.get_mut(name).expect("a live storage")
    }

    /// `Storage(dev, nbytes)`: a temporary.
    pub fn storage(&mut self, nbytes: i64) -> String {
        self.ntmp += 1;
        let name = format!("tmp{}", self.ntmp);
        self.stores.insert(name.clone(), Store::new(nbytes));
        self.steps.push(Step::Alloc {
            buf: name.clone(),
            nbytes,
            alias: None,
        });
        name
    }

    /// `Storage(dev, nbytes, addr=at, owned=False)`: a storage over `at`.
    pub fn alias(&mut self, at: Addr, nbytes: i64) -> String {
        self.ntmp += 1;
        let name = format!("tmp{}", self.ntmp);
        let mut s = Store::new(nbytes);
        s.alias = Some(at.clone());
        self.stores.insert(name.clone(), s);
        self.steps.push(Step::Alloc {
            buf: name.clone(),
            nbytes,
            alias: Some(at),
        });
        name
    }

    /// `dev.alloc` outside a storage: a raw temporary.
    pub fn raw(&mut self, nbytes: i64) -> String {
        self.nraw += 1;
        let name = format!("raw{}", self.nraw);
        let mut s = Store::new(nbytes);
        s.live = true;
        self.stores.insert(name.clone(), s);
        self.steps.push(Step::Alloc {
            buf: name.clone(),
            nbytes,
            alias: None,
        });
        name
    }

    pub fn free(&mut self, name: &str) {
        self.steps.push(Step::Free { buf: name.into() });
        if let Some(s) = self.stores.get_mut(name) {
            s.live = false;
            s.valid.clear();
            s.forms.clear();
        }
    }

    /// `st.form(key, nbytes)`: the side form, allocated when new or too
    /// small (its contents then not valid).
    pub fn form(&mut self, buf: &str, key: &Value, nbytes: i64) -> Addr {
        if key == &flat() {
            return self.addr(buf, 0);
        }
        let s = self.store_mut(buf);
        let have = s.forms.iter().position(|(k, _)| k == key);
        let fresh = match have {
            Some(i) if s.forms[i].1 < nbytes => {
                s.forms.remove(i);
                s.valid.retain(|k| k != key);
                true
            }
            Some(_) => false,
            None => true,
        };
        if fresh {
            s.forms.push((key.clone(), nbytes));
            self.steps.push(Step::Form {
                buf: buf.into(),
                form: key.clone(),
                nbytes,
            });
        }
        Addr {
            buf: buf.into(),
            form: key.clone(),
            offset: 0,
        }
    }

    /// The address `offset` bytes into `buf`'s row-major bytes: an alias's
    /// are the form it stands over.
    pub fn addr(&self, buf: &str, offset: i64) -> Addr {
        match &self.stores[buf].alias {
            Some(a) => Addr {
                offset: a.offset + offset,
                ..a.clone()
            },
            None => Addr {
                buf: buf.into(),
                form: flat(),
                offset,
            },
        }
    }

    pub fn wrote(&mut self, buf: &str, key: &Value, keep_host: bool) {
        self.steps.push(Step::Wrote {
            buf: buf.into(),
            form: key.clone(),
            keep_host,
        });
        let s = self.store_mut(buf);
        s.valid = vec![key.clone()];
        if !keep_host {
            s.host = None;
        }
    }

    pub fn made(&mut self, buf: &str, key: &Value) {
        if crate::mutant::on("api.forms/made_drop") {
            return;
        }
        self.steps.push(Step::Made {
            buf: buf.into(),
            form: key.clone(),
        });
        let s = self.store_mut(buf);
        if !s.valid.contains(key) {
            s.valid.push(key.clone());
        }
    }

    /// `forms.table`: a constant uploaded once by content.
    pub fn table(&mut self, key: &str, array: Array) -> Addr {
        let name = format!("table:{key}");
        let at = Addr {
            buf: name,
            form: flat(),
            offset: 0,
        };
        self.steps.push(Step::Table {
            at: at.clone(),
            array,
        });
        at
    }

    /// Host bytes written at `at`.
    pub fn write(&mut self, at: Addr, bytes: i64, sha256: String) {
        self.steps.push(Step::Write { at, bytes, sha256 });
    }

    /// `n` of `buf`'s host values (`dtype`) cast to f16 and written at `at`.
    pub fn cast(&mut self, at: Addr, buf: &str, dtype: &str, n: i64) {
        self.steps.push(Step::Cast {
            at,
            buf: buf.into(),
            dtype: crate::run::np_name(dtype),
            n,
        });
    }

    pub fn launch(&mut self, call: Call, kind: &str, info: Value, binds: BTreeMap<String, Addr>) {
        if info.get("blocks").is_some() {
            self.paths.insert("rows.partials");
        }
        self.steps.push(Step::Launch(Box::new(Launch {
            call: Some(call),
            name: String::new(),
            kind: kind.into(),
            info,
            consts: json!({}),
            binds,
            graph: None,
        })));
    }
}
