//! The machine schema: the only hardware facts the stack reads.
//!
//! A machine file (`machines/<name>.json`, `docs/spec/stack/machine.md`)
//! holds `schema`, `name`, `image`, `machine` (the facts) and `sources`.
//! [`Machine`] derefs to the typed facts (`m.vc.lanes`), reads any fact by
//! dotted path ([`Machine::get`]) and names where it came from
//! ([`Machine::source`]). [`Machine::digest`] keys the caches.

pub mod canon;
pub mod card;
pub mod fill;
pub mod mutant;
pub mod packing;
pub mod pairing;
pub mod refusal;
pub mod schema;

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ops::Deref;
use std::path::{Path, PathBuf};

pub use fill::fill_lost;
pub use pairing::{Pairing, PAIRING};
pub use refusal::{refuse, Refusal};
pub use schema::*;

/// The schema definition this build reads: `src/schema/*.rs` in file-name
/// order (a test holds the list to the directory).
pub const SCHEMA_SOURCES: [(&str, &str); 6] = [
    ("card.rs", include_str!("schema/card.rs")),
    ("die.rs", include_str!("schema/die.rs")),
    ("firmware.rs", include_str!("schema/firmware.rs")),
    ("kinds.rs", include_str!("schema/kinds.rs")),
    ("mod.rs", include_str!("schema/mod.rs")),
    ("system.rs", include_str!("schema/system.rs")),
];

/// The version of a schema definition: the first 16 hex digits of sha256
/// over `texts` with `//` comments and all whitespace removed, one line per
/// file. The machine generator and the Python reader compute the same over
/// the same files (`kohakutpu.language.machine.schema_version`).
pub fn schema_version_of(texts: &[&str]) -> String {
    let files: Vec<String> = texts
        .iter()
        .map(|t| {
            t.lines()
                .map(|l| l.split("//").next().unwrap_or(""))
                .flat_map(|l| l.split_whitespace())
                .collect()
        })
        .collect();
    let digest = Sha256::digest(files.join("\n").as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// The schema version this build reads: [`schema_version_of`] its
/// [`SCHEMA_SOURCES`]. A machine file names the version it was written for.
pub fn schema_version() -> &'static str {
    static V: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    V.get_or_init(|| {
        let texts: Vec<&str> = SCHEMA_SOURCES.iter().map(|(_, t)| *t).collect();
        schema_version_of(&texts)
    })
}

#[derive(Debug, thiserror::Error)]
pub enum MachineError {
    #[error("{path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("machine file: {0}")]
    Json(#[from] serde_json::Error),
    #[error("machine file: schema {found}, this reader takes {}", schema_version())]
    Schema { found: Value },
    #[error("machine file: no {0:?}")]
    Missing(&'static str),
    #[error("no machine file {name:?} in {dir}; have {have:?}")]
    Unknown {
        name: String,
        dir: PathBuf,
        have: Vec<String>,
    },
    #[error("machine has no fact {0:?}")]
    NoFact(String),
    #[error("machine conversion policy: {0}")]
    Conversion(String),
}

impl From<MachineError> for Refusal {
    fn from(e: MachineError) -> Refusal {
        Refusal::new("MACHINE", e.to_string())
    }
}

/// The unit kinds a die holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum UnitKind {
    /// Matmul cluster.
    Mg,
    /// Vector core.
    Vc,
    /// Streaming Row Unit.
    Sr,
    Simt,
}

impl UnitKind {
    pub const ALL: [UnitKind; 4] = [UnitKind::Mg, UnitKind::Vc, UnitKind::Sr, UnitKind::Simt];

    /// The kind's name in the machine file and in L1 text (`mg`).
    pub fn name(self) -> &'static str {
        match self {
            UnitKind::Mg => "mg",
            UnitKind::Vc => "vc",
            UnitKind::Sr => "sr",
            UnitKind::Simt => "simt",
        }
    }

    /// The firmware's unit type tag (`MG`).
    pub fn tag(self) -> &'static str {
        match self {
            UnitKind::Mg => "MG",
            UnitKind::Vc => "VC",
            UnitKind::Sr => "SR",
            UnitKind::Simt => "SIMT",
        }
    }

    /// The kind named `name`, either spelling (`mg` or `MG`).
    pub fn parse(name: &str) -> Option<UnitKind> {
        UnitKind::ALL
            .into_iter()
            .find(|k| k.name() == name || k.tag() == name)
    }
}

/// One machine file, loaded and checked.
#[derive(Clone, Debug)]
pub struct Machine {
    pub name: String,
    pub image: String,
    facts: Facts,
    sources: BTreeMap<String, String>,
    raw: Value,
    keyed: Value,
    digest: [u8; 32],
    doc: Value,
}

impl Deref for Machine {
    type Target = Facts;
    fn deref(&self) -> &Facts {
        &self.facts
    }
}

impl Machine {
    /// The machine file at `path`.
    pub fn load(path: impl AsRef<Path>) -> Result<Machine, MachineError> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path).map_err(|source| MachineError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        Machine::from_json(&text)
    }

    /// The machine file `name` in [`machines_dir`].
    pub fn named(name: &str) -> Result<Machine, MachineError> {
        let dir = machines_dir();
        let fallback = (name.is_empty() && mutant::on("rul.001/unbuilt"))
            .then(|| names(&dir).into_iter().next())
            .flatten();
        let name = fallback.as_deref().unwrap_or(name);
        let path = dir.join(format!("{name}.json"));
        if !path.exists() {
            return Err(MachineError::Unknown {
                name: name.to_string(),
                have: names(&dir),
                dir,
            });
        }
        Machine::load(path)
    }

    /// A machine file's text.
    pub fn from_json(text: &str) -> Result<Machine, MachineError> {
        Machine::from_value(serde_json::from_str(text)?)
    }

    /// This machine with the fact at dotted `path` (which must exist) set to
    /// `value`: a variant for a test or a smaller card. The digest follows.
    pub fn with_fact(&self, path: &str, value: Value) -> Result<Machine, MachineError> {
        let mut doc = self.doc.clone();
        let mut node = &mut doc["machine"];
        for key in path.split('.') {
            node = node
                .get_mut(key)
                .ok_or_else(|| MachineError::NoFact(path.to_string()))?;
        }
        *node = value;
        Machine::from_value(doc)
    }

    /// This machine as a card of `nodes` identical dies in a chain
    /// (`card.nodes`, `card.chain`).
    pub fn with_nodes(&self, nodes: u32) -> Result<Machine, MachineError> {
        self.with_fact("card.nodes", Value::from(nodes))?
            .with_fact("card.chain", Value::from((0..nodes).collect::<Vec<_>>()))
    }

    /// The stack's staging room a node: half the staging store (the
    /// firmware queues hold the other half).
    pub fn stage_room(&self) -> i64 {
        (self.die.stage_bytes().expect("a staging store's bytes") / 2) as i64
    }

    /// Bytes `values` MXFP7 values take: whole entries of `mg.lanes` x
    /// `mg.kblock` values, each `mg.entry_bits` in whole words.
    pub fn mx7_bytes(&self, values: i64) -> i64 {
        let word = self.die.word_bytes as i64;
        let entry = (self.mg.entry_bits as i64 + 8 * word - 1) / (8 * word) * word;
        values / (self.mg.lanes as i64 * self.mg.kblock as i64) * entry
    }

    /// Vector cores a vector_mx7 quant runs on
    /// (`conversion.fp16_mx7.vector_cores`, 0: every core).
    pub fn quant_cores(&self) -> i64 {
        match self.conversion.fp16_mx7.vector_cores {
            0 => self.units(UnitKind::Vc).len() as i64,
            n => n,
        }
    }

    fn from_value(doc: Value) -> Result<Machine, MachineError> {
        let schema = doc.get("schema").cloned().unwrap_or(Value::Null);
        if schema.as_str() != Some(schema_version()) {
            return Err(MachineError::Schema { found: schema });
        }
        let text_of = |key: &'static str| -> Result<String, MachineError> {
            doc.get(key)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or(MachineError::Missing(key))
        };
        let name = text_of("name")?;
        let image = text_of("image")?;
        let mut raw = doc
            .get("machine")
            .cloned()
            .ok_or(MachineError::Missing("machine"))?;
        if raw.get("vc").is_none() && mutant::on("rul.001/missing-core") {
            let original = std::fs::read_to_string(machines_dir().join(format!("{name}.json")))
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok());
            if let Some(original) = original {
                raw["vc"] = original["machine"]["vc"].clone();
            }
        }
        let mut facts: Facts = serde_json::from_value(raw.clone())?;
        let p = &facts.conversion.fp16_mx7;
        if !matches!(p.version, 1 | 2)
            || !matches!(p.vector_slots, 1 | 2)
            || p.vector_cores < 0
            || p.allowed_backends.is_empty()
            || !p.allowed_backends.contains(&p.preferred_backend)
        {
            return Err(MachineError::Conversion(
                "expected version 1/2, valid vector schedule and an allowed preferred backend"
                    .into(),
            ));
        }
        if mutant::on("rul.001/registers") {
            facts.vc.regs /= 2;
            facts.vc.rf_copies *= 2;
        }
        let sources: BTreeMap<String, String> = match doc.get("sources") {
            Some(s) => serde_json::from_value(s.clone())?,
            None => return Err(MachineError::Missing("sources")),
        };
        let keyed = serde_json::to_value(&facts)?;
        let digest = Sha256::digest(digest_input(&raw, &keyed).as_bytes()).into();
        if mutant::on("machine.key/reads-runtime") {
            facts.fw.package.magic ^= *facts.node.spad_bytes.runtime();
        }
        Ok(Machine {
            name,
            image,
            facts,
            sources,
            raw,
            keyed,
            digest,
            doc,
        })
    }

    /// The typed facts.
    pub fn facts(&self) -> &Facts {
        &self.facts
    }

    /// sha256 of the canonical JSON ([`canon::canonical`]) of the facts a
    /// compile path can read, every field but a [`Runtime`] one, lowercase
    /// hex. Name, image and provenance are not in it, so a firmware rebuild
    /// that moves only a runtime fact keeps every compiled artifact.
    pub fn digest(&self) -> String {
        self.digest.iter().map(|b| format!("{b:02x}")).collect()
    }

    /// The digest's bytes.
    pub fn digest_bytes(&self) -> [u8; 32] {
        self.digest
    }

    /// The value at dotted `path` (`"vc.lanes"`), or None; a [`Runtime`] fact
    /// is None, so a path cannot read past the digest.
    pub fn get(&self, path: &str) -> Option<&Value> {
        let walk = |root| {
            path.split('.')
                .try_fold(root, |node: &Value, key| node.get(key))
        };
        let got = walk(&self.keyed)?;
        (!got.is_null() || walk(&self.raw)?.is_null()).then_some(got)
    }

    /// The value at dotted `path`, a [`Runtime`] fact too. A compile path
    /// never calls this (`compile_reads_no_runtime_fact`).
    pub fn runtime_fact(&self, path: &str) -> Option<&Value> {
        path.split('.')
            .try_fold(&self.raw, |node, key| node.get(key))
    }

    /// Where the fact at `path` comes from (a `file:line` or a derivation).
    pub fn source(&self, path: &str) -> Option<&str> {
        self.sources.get(path).map(String::as_str)
    }

    /// The units of `kind`, in unit-index order.
    pub fn units(&self, kind: UnitKind) -> &[Coord] {
        let u = &self.facts.die.units;
        match kind {
            UnitKind::Mg => &u.mg,
            UnitKind::Vc => &u.vc,
            UnitKind::Sr => &u.sr,
            UnitKind::Simt => &u.simt,
        }
    }

    /// Unit `index` of `kind`, or None past the last.
    pub fn unit(&self, kind: UnitKind, index: usize) -> Option<Coord> {
        self.units(kind).get(index).copied()
    }

    /// The kinds this die has at least one unit of.
    pub fn kinds(&self) -> Vec<UnitKind> {
        UnitKind::ALL
            .into_iter()
            .filter(|k| !self.units(*k).is_empty())
            .collect()
    }

    /// The SRU facts, or None on a die without SR units.
    pub fn sr_facts(&self) -> Option<SrFacts> {
        self.facts.sr.facts()
    }

    /// The node count of the card (its meshes).
    pub fn nodes(&self) -> u32 {
        self.facts.card.nodes
    }
}

/// What [`Machine::digest`] hashes: the canonical JSON of the `keyed` facts.
fn digest_input(raw: &Value, keyed: &Value) -> String {
    if mutant::on("machine.key/whole-file") {
        return canon::canonical(raw);
    }
    if mutant::on("machine.key/drops-fact") {
        let mut less = keyed.clone();
        less["vc"]["sregs"] = Value::Null;
        return canon::canonical(&less);
    }
    if mutant::on("machine.key/drops-policy") {
        let mut less = keyed.clone();
        less["conversion"] = Value::Null;
        return canon::canonical(&less);
    }
    canon::canonical(keyed)
}

/// Where machine files live: `KTPU_MACHINES`, else the repository's
/// `machines/`.
pub fn machines_dir() -> PathBuf {
    match std::env::var_os("KTPU_MACHINES") {
        Some(dir) => PathBuf::from(dir),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../machines"),
    }
}

/// Every machine file name in `dir` (`measured.json` is not one).
pub fn names(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            (p.extension()? == "json")
                .then(|| p.file_stem()?.to_str().map(str::to_string))
                .flatten()
        })
        .filter(|n| n != "measured")
        .collect();
    out.sort();
    out
}

/// Each machine file in `dir` this build's schema cannot read, and why: a
/// file newer than the build (a field it does not know, a schema version
/// it does not take) shows here, not deep inside a compile.
pub fn unreadable(dir: &Path) -> Vec<(String, MachineError)> {
    if mutant::on("machine.check/off") {
        return Vec::new();
    }
    names(dir)
        .into_iter()
        .filter_map(|n| {
            let err = Machine::load(dir.join(format!("{n}.json"))).err()?;
            Some((n, err))
        })
        .collect()
}

/// What to tell a user whose build cannot read the machine files `bad`
/// ([`unreadable`] of `dir`): every file and its error, and the rebuild.
pub fn stale_message(dir: &Path, bad: &[(String, MachineError)]) -> String {
    let each: Vec<String> = bad.iter().map(|(n, e)| format!("  {n}: {e}")).collect();
    format!(
        "this build of the Rust stack reads machine schema {} as it stood when it \
         was built, and cannot read {} machine file(s) in {}:\n{}\nthe machine files are \
         newer than this build: rebuild it (scripts/py/build_rs.py)",
        schema_version(),
        bad.len(),
        dir.display(),
        each.join("\n")
    )
}
