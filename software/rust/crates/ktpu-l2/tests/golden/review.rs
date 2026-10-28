//! Reviewed, allowlisted regeneration of the lowering comparison only.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};

use ktpu_l2::Program;
use ktpu_machine::Machine;
use serde_json::Value;

pub struct Review {
    write: bool,
    cases: BTreeSet<String>,
}

impl Review {
    pub fn environment(paths: &[PathBuf]) -> Option<Self> {
        let mode = std::env::var("KTPU_L2_REGOLD").ok()?;
        assert!(
            matches!(mode.as_str(), "audit" | "write"),
            "KTPU_L2_REGOLD=audit|write"
        );
        let cases: BTreeSet<String> = std::env::var("KTPU_L2_CASES")
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        assert!(
            mode != "write" || !cases.is_empty(),
            "write requires explicit KTPU_L2_CASES allowlist"
        );
        for name in &cases {
            assert!(
                paths
                    .iter()
                    .any(|p| p.file_stem().unwrap() == name.as_str()),
                "unknown L2 case {name}"
            );
        }
        Some(Self {
            write: mode == "write",
            cases,
        })
    }

    pub fn selected(&self, case: &Value) -> bool {
        self.write && self.cases.contains(case["case"].as_str().unwrap())
    }

    pub fn report(case: &Value, field: &str, text: &str) {
        let want = case[field].as_str().unwrap();
        if want != text {
            eprintln!(
                "L2 REGOLD {} {field}: {}",
                case["case"].as_str().unwrap(),
                super::first_diff(want, text)
            );
        }
    }

    pub fn compare(
        &self,
        path: &Path,
        case: &Value,
        fields: Vec<(&str, String)>,
        machine: &Machine,
    ) -> Result<(), String> {
        let changed: Vec<_> = fields
            .into_iter()
            .filter(|(k, v)| case[*k].as_str() != Some(v.as_str()))
            .collect();
        if changed.is_empty() {
            return Ok(());
        }
        let text = changed
            .iter()
            .find(|(k, _)| *k == "l1")
            .map(|(_, v)| v.as_str())
            .unwrap_or_else(|| case["l1"].as_str().unwrap());
        verify(text, case, machine).map_err(|e| format!("REGOLD verifier refused: {e}"))?;
        if self.selected(case) {
            let backup = std::env::var("KTPU_L2_REGOLD_BACKUP").map_err(|_| {
                "KTPU_L2_REGOLD_BACKUP: directory receiving the original goldens".to_string()
            })?;
            let backup = Path::new(&backup);
            std::fs::create_dir_all(&backup).map_err(|e| e.to_string())?;
            // Never destroy the pre-review original on a second invocation.
            let mut original = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(backup.join(path.file_name().unwrap()))
                .map_err(|e| e.to_string())?;
            original
                .write_all(&std::fs::read(path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
            let mut updated = case.clone();
            for (field, text) in changed {
                updated[field] = Value::String(text);
            }
            std::fs::write(
                path,
                format!("{}\n", serde_json::to_string_pretty(&updated).unwrap()),
            )
            .map_err(|e| e.to_string())?;
            eprintln!("L2 REGOLD wrote {}", path.display());
        }
        Ok(())
    }
}

fn verify(text: &str, case: &Value, machine: &Machine) -> Result<(), String> {
    let nodes = case["nodes"].as_u64().unwrap() as u32;
    let machine = machine.with_nodes(nodes).map_err(|e| e.to_string())?;
    let name = case["name"].as_str().unwrap();
    let source = Program::parse(case["l2"].as_str().unwrap(), super::FILE, Some(name))
        .map_err(|e| e.to_string())?;
    let caller: BTreeSet<_> = source
        .module
        .body(name, "l2")
        .map_err(|e| e.to_string())?
        .params
        .iter()
        .map(|(n, _)| n.clone())
        .collect();
    let l1 = ktpu_l1::text::read(text, "<regold.l1>").map_err(|e| e.to_string())?;
    let l1 = ktpu_l1::sched::schedule_module(&l1, &machine).map_err(|e| e.to_string())?;
    let mut binds = BTreeMap::new();
    let mut next = 0x1000000u64;
    for (n, t) in &l1.body(name, "l1").map_err(|e| e.to_string())?.params {
        if caller.contains(n) {
            binds.insert(n.clone(), next as i128);
            next += ktpu_l1::layout::buffer::Buffer::of(t)
                .map_err(|e| e.to_string())?
                .bytes(&machine)
                .map_err(|e| e.to_string())?
                .div_ceil(4096)
                * 4096;
        }
    }
    let mut arenas = Vec::new();
    for a in
        ktpu_l1::layout::scratch::arenas(&l1, name, &caller, &machine).map_err(|e| e.to_string())?
    {
        let size = a.bytes.div_ceil(4096) * 4096;
        arenas.push((a, next));
        next += size;
    }
    let programs =
        ktpu_l1::build::programs_in(&l1, name, &binds, &arenas, &machine, Some(nodes), None)
            .map_err(|e| e.to_string())?;
    let builder = ktpu_l1::build::Builder::new(&machine).map_err(|e| e.to_string())?;
    let mut bound = ktpu_verify::Nodes::new();
    for program in programs {
        bound.insert(
            program.mesh,
            builder
                .build(&program, Default::default())
                .map_err(|e| e.to_string())?
                .build(true, false)
                .bind(&[])
                .map_err(|e| e.to_string())?,
        );
    }
    let target = ktpu_verify::Target::from_machine(&machine, None).map_err(|e| e.to_string())?;
    ktpu_verify::gate(&[bound], &target, ktpu_verify::Mode::Refuse, None)
        .map_err(|e| e.to_string())?;
    Ok(())
}
