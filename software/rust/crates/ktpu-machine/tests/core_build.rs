//! Each machine file's `vc.regs` / `vc.rf_copies` are 2^V2_RW / V2_RF_COPIES
//! of its `vc.build` in V8_VEC_CORES (`scripts/tcl/v8t3/00_config.tcl`), or
//! v2_core.v's defaults where the build defines none.

use ktpu_machine::{machines_dir, names, Machine};
use std::path::{Path, PathBuf};

fn repo() -> PathBuf {
    machines_dir().join("..")
}

fn read(rel: &str) -> String {
    std::fs::read_to_string(repo().join(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// The digits after `key` in `text`, if `key` occurs.
fn number_after(text: &str, key: &str) -> Option<u32> {
    let at = text.find(key)? + key.len();
    let digits: String = text[at..]
        .trim_start()
        .chars()
        .take_while(char::is_ascii_digit)
        .collect();
    Some(digits.parse().unwrap())
}

/// The table entry of core build `build`: a base entry (`    v2 {` up to its
/// `module`) or a derived one (`V8_VEC_CORES v21 [` up to its `}]`).
fn entry<'a>(table: &'a str, build: &str) -> &'a str {
    if let Some(at) = table.find(&format!("\n    {build} {{")) {
        let end = at
            + table[at..]
                .find("module")
                .expect("an entry names its module");
        return &table[at..end];
    }
    let at = table
        .find(&format!("V8_VEC_CORES {build} ["))
        .unwrap_or_else(|| panic!("core build {build} is not in V8_VEC_CORES"));
    let end = at + table[at..].find("}]").expect("a derived entry ends in }]");
    &table[at..end]
}

/// v2_core.v's value when `define` is absent: the parameter line of the
/// `else` arm of its `ifdef`.
fn rtl_default(core: &str, define: &str) -> u32 {
    let lines: Vec<&str> = core.lines().map(str::trim).collect();
    let at = lines
        .iter()
        .position(|l| *l == format!("`ifdef {define}"))
        .unwrap_or_else(|| panic!("v2_core.v has no `ifdef {define}"));
    let arm = &lines[at..];
    let other = arm.iter().position(|l| *l == "`else").unwrap();
    number_after(arm[other + 1], "=").unwrap()
}

/// Every machine file in `dir` whose core is not its build's, as
/// `name: got (regs, copies), want (regs, copies)`; and each file's
/// `(build, regs, copies)`.
fn mismatches(dir: &Path) -> (Vec<String>, Vec<(String, u32, u32)>) {
    let table = read("scripts/tcl/v8t3/00_config.tcl");
    let core = read("src/kohakutpu/vector2/v2_core.v");
    let (rw, copies) = (
        rtl_default(&core, "V2_RW"),
        rtl_default(&core, "V2_RF_COPIES"),
    );
    assert_eq!((rw, copies), (5, 4), "v2_core.v defaults");
    let (mut bad, mut seen) = (Vec::new(), Vec::new());
    for name in names(dir) {
        let m = Machine::load(dir.join(format!("{name}.json"))).unwrap();
        let e = entry(&table, &m.vc.build);
        let want = (
            1 << number_after(e, "V2_RW=").unwrap_or(rw),
            number_after(e, "V2_RF_COPIES=").unwrap_or(copies),
        );
        let got = (m.vc.regs, m.vc.rf_copies);
        if got != want {
            bad.push(format!("{name}: got {got:?}, want {want:?}"));
        }
        seen.push((m.vc.build.clone(), got.0, got.1));
    }
    (bad, seen)
}

#[test]
fn each_machine_files_core_is_its_builds_register_count_and_copies() {
    let (bad, seen) = mismatches(&machines_dir());
    assert!(bad.is_empty(), "{bad:?}");
    // The table's two V2 builds both occur: v2 (32 / 4) and v21 (128 / 2).
    assert!(seen.contains(&("v2".into(), 32, 4)), "{seen:?}");
    assert!(seen.contains(&("v21".into(), 128, 2)), "{seen:?}");
}

#[test]
fn a_machine_file_whose_register_count_is_not_its_builds_is_named() {
    let dir = std::env::temp_dir().join(format!("ktpu-core-build-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    for name in names(&machines_dir()) {
        let file = format!("{name}.json");
        std::fs::copy(machines_dir().join(&file), dir.join(&file)).unwrap();
    }
    let path = dir.join("multimesh_v9t6.json");
    let mut doc: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    doc["machine"]["vc"]["regs"] = 64.into();
    std::fs::write(&path, doc.to_string()).unwrap();
    let (bad, _) = mismatches(&dir);
    assert_eq!(bad, ["multimesh_v9t6: got (64, 2), want (128, 2)"]);
}
