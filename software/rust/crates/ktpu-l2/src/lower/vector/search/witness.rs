//! Candidate identities, immutable text references and byte comparisons.
//!
//! Run with `cargo test -p ktpu-l2 --features witness -- --ignored candidate_witness`.
//!
//! The frozen template generator is compiled only with `witness`.
//! Typed and reference searches record their enumerations independently.

use std::collections::BTreeMap;
use std::fmt;
use std::result::Result;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use super::*;

/// The band shape and constant/row residency choices.
pub type Shape = (usize, bool, bool, bool, bool);

/// A candidate's place in a search, independent of worker order.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Kind {
    Probe(Shape),
    Band(Shape),
    Modulo { ii: i64, k: i64, tail: String },
}

/// The complete search input and one candidate's choices.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Id {
    pub input: Arc<str>,
    pub kind: Kind,
}

/// One immutable legacy image, before cache lookup or encoding.
#[derive(Clone, Debug)]
pub struct Reference {
    pub id: Id,
    pub text: Arc<str>,
}

/// A comparison failure is separate from a compiler refusal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mismatch {
    pub id: Id,
    pub byte: usize,
}

/// Exact bytes, including whitespace and the final newline.
pub fn compare(reference: &Reference, candidate: &[u8]) -> Result<(), Mismatch> {
    let expected = reference.text.as_bytes();
    if expected == candidate {
        return Ok(());
    }
    let byte = expected
        .iter()
        .zip(candidate)
        .position(|(a, b)| a != b)
        .unwrap_or(expected.len().min(candidate.len()));
    Err(Mismatch {
        id: reference.id.clone(),
        byte,
    })
}

type Comparator = dyn Fn(&Reference, &[u8]) -> Result<(), Mismatch> + Send + Sync;

/// All occurrences are retained, including duplicate texts and identities.
#[derive(Clone, Debug, Default)]
pub struct Report {
    pub images: BTreeMap<Id, Vec<Reference>>,
    pub failures: Vec<Mismatch>,
    pub refused: BTreeMap<Id, Vec<String>>,
    /// Modulo encodings: word count, or the encoder's refusal.
    pub encoded: BTreeMap<Id, Vec<Result<usize, String>>>,
}

impl Report {
    /// Probe, band, explicit-tail and kernel-tail comparison counts.
    pub fn counts(&self) -> [usize; 4] {
        let mut counts = [0; 4];
        for (id, images) in &self.images {
            let i = match &id.kind {
                Kind::Probe(_) => 0,
                Kind::Band(_) => 1,
                Kind::Modulo { tail, .. } if tail == "explicit" => 2,
                Kind::Modulo { tail, .. } if tail == "kernel" => 3,
                Kind::Modulo { .. } => unreachable!("a modulo tail"),
            };
            counts[i] += images.len();
        }
        counts
    }

    pub fn check(&self) -> Result<(), &[Mismatch]> {
        if self.failures.is_empty() {
            Ok(())
        } else {
            Err(&self.failures)
        }
    }
}

pub(super) struct ModuloReference {
    report: Report,
    starts: BTreeMap<Id, usize>,
}

/// A per-search comparator and ledger, shared with its Rayon workers.
pub struct Witness {
    comparator: Box<Comparator>,
    report: Mutex<Report>,
    drop_candidate: AtomicBool,
}

impl fmt::Debug for Witness {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Witness").finish_non_exhaustive()
    }
}

impl Witness {
    pub fn new(
        comparator: impl Fn(&Reference, &[u8]) -> Result<(), Mismatch> + Send + Sync + 'static,
    ) -> Self {
        Self {
            comparator: Box::new(comparator),
            report: Mutex::new(Report::default()),
            drop_candidate: AtomicBool::new(false),
        }
    }

    /// Mutation witness: remove one successful typed band before deduplication.
    pub fn dropping_one_candidate(self) -> Self {
        self.drop_candidate.store(true, Ordering::Relaxed);
        self
    }

    pub(super) fn drop_candidate(&self) -> bool {
        self.drop_candidate.swap(false, Ordering::Relaxed)
    }

    pub(super) fn reference_band(&self, input: &Arc<str>, kind: Kind, got: &VResult<Cand>) {
        match got {
            Ok(c) => self.image(input, kind, &c.0.source),
            Err(e) => self
                .report
                .lock()
                .unwrap()
                .refused
                .entry(Id {
                    input: input.clone(),
                    kind,
                })
                .or_default()
                .push(format!("{e:?}")),
        }
    }

    pub fn report(&self) -> Report {
        self.report.lock().unwrap().clone()
    }

    pub(super) fn image(&self, input: &Arc<str>, kind: Kind, text: &str) {
        self.paired_image(input, kind, text, text);
    }

    fn paired_image(&self, input: &Arc<str>, kind: Kind, text: &str, actual: &str) {
        let reference = Reference {
            id: Id {
                input: input.clone(),
                kind,
            },
            text: Arc::from(text),
        };
        let result = (self.comparator)(&reference, actual.as_bytes());
        let mut report = self.report.lock().unwrap();
        if let Err(e) = result {
            report.failures.push(e);
        }
        report
            .images
            .entry(reference.id.clone())
            .or_default()
            .push(reference);
    }

    pub(super) fn encoded(&self, input: &Arc<str>, kind: Kind, got: &crate::ir::Result<usize>) {
        self.report
            .lock()
            .unwrap()
            .encoded
            .entry(Id {
                input: input.clone(),
                kind,
            })
            .or_default()
            .push(got.as_ref().copied().map_err(|e| e.to_string()));
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn band(
        &self,
        input: &Arc<str>,
        kind: Kind,
        got: &VResult<Cand>,
        old: &VResult<TextCand>,
        body: &Body,
        name: &str,
        t: &Target,
    ) {
        match (got, old) {
            (Ok(c), Ok(a)) => {
                assert_eq!(format!("{:?}", (&c.1, &c.2)), format!("{:?}", (&a.1, &a.2)));
                self.paired_image(input, kind, &a.0, &c.0.source);
                equivalent(&c.0, &a.0, body, name, c.1.len(), t);
            }
            (Err(e), Err(a)) => {
                assert_eq!(format!("{e:?}"), format!("{a:?}"));
                self.report
                    .lock()
                    .unwrap()
                    .refused
                    .entry(Id {
                        input: input.clone(),
                        kind,
                    })
                    .or_default()
                    .push(format!("{e:?}"));
            }
            (a, z) => panic!("band verdict mismatch: {a:?} / {z:?}"),
        }
    }

    pub(super) fn modulo_reference(&self, input: &Arc<str>, report: Report) -> ModuloReference {
        let starts: BTreeMap<Id, usize> = self
            .report
            .lock()
            .unwrap()
            .images
            .iter()
            .filter(|(id, _)| id.input == *input && matches!(id.kind, Kind::Modulo { .. }))
            .map(|(id, v)| (id.clone(), v.len()))
            .collect();
        ModuloReference { report, starts }
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn modulo_image(
        &self,
        input: &Arc<str>,
        kind: Kind,
        module: &Module,
        old: &ModuloReference,
        body: &Body,
        name: &str,
        nparams: usize,
        t: &Target,
    ) {
        let id = Id {
            input: input.clone(),
            kind: kind.clone(),
        };
        let n = self
            .report
            .lock()
            .unwrap()
            .images
            .get(&id)
            .map_or(0, Vec::len);
        let n = n - old.starts.get(&id).copied().unwrap_or(0);
        let reference = &old.report.images[&id][n];
        self.paired_image(input, kind, &reference.text, &module.source);
        equivalent(module, &reference.text, body, name, nparams, t);
    }

    pub(super) fn complete_modulo(&self, input: &Arc<str>, old: &ModuloReference) {
        let actual = self.report.lock().unwrap();
        let counts = actual
            .images
            .iter()
            .filter(|(id, _)| id.input == *input && matches!(id.kind, Kind::Modulo { .. }))
            .filter_map(|(id, v)| {
                let n = v.len() - old.starts.get(id).copied().unwrap_or(0);
                (n != 0).then(|| (id.clone(), n))
            })
            .collect::<BTreeMap<_, _>>();
        let expected = old
            .report
            .images
            .iter()
            .map(|(id, v)| (id.clone(), v.len()))
            .collect::<BTreeMap<_, _>>();
        assert_eq!(counts, expected, "every modulo candidate");
        for (id, encoded) in &old.report.encoded {
            let start = old.starts.get(id).copied().unwrap_or(0);
            assert_eq!(&actual.encoded[id][start..], encoded);
        }
    }
}

#[cfg(test)]
#[test]
fn repeated_modulo_ledger_uses_invocation_offsets() {
    let w = Witness::new(compare);
    let input: Arc<str> = "same input".into();
    let kind = Kind::Modulo {
        ii: 1,
        k: 2,
        tail: "explicit".into(),
    };
    let legacy = Witness::new(compare);
    legacy.image(&input, kind.clone(), "image k()\n    halt\n");
    legacy.encoded(&input, kind.clone(), &Ok(1));
    for _ in 0..2 {
        let reference = w.modulo_reference(&input, legacy.report());
        w.image(&input, kind.clone(), "image k()\n    halt\n");
        w.encoded(&input, kind.clone(), &Ok(1));
        w.complete_modulo(&input, &reference);
    }
    assert_eq!(w.report().counts(), [0, 0, 2, 0]);
}

fn equivalent(module: &Module, text: &str, body: &Body, name: &str, nparams: usize, t: &Target) {
    let old = encode::read_image(text, name, body.vregs).expect("legacy image parses");
    assert_eq!(
        format!("{module:?}"),
        format!("{old:?}"),
        "module/source maps"
    );
    let timing = |m| encode::cycles(m, name, body.vregs, body.vcopies, body.fill, &t.machine);
    assert_eq!(
        format!("{:?}", timing(module)),
        format!("{:?}", timing(&old)),
        "candidate cycles/refusal"
    );
    encode::compare_images(module, &old, name, nparams, &t.machine);
}

/// Input identity includes resolved IR, bindings, transfers and machine settings.
#[allow(clippy::too_many_arguments)]
pub(super) fn input(
    search: &Search,
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    plan: &Plan,
    land: Option<i64>,
    t: &Target,
) -> Arc<str> {
    let env: BTreeMap<_, _> = env.iter().collect();
    format!(
        "{body:?}\n{par:?}\n{env:?}\n{name:?}\n{plan:?}\n{land:?}\n{t:?}\n{:?}\n{:?}\n{:?}",
        search.strategy, search.hold, search.pin
    )
    .into()
}
