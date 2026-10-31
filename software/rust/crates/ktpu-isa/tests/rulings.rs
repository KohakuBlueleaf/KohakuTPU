//! Boundary and machine-variant checks for the owner rulings.
use ktpu_isa::{spec, Machine};
use serde_json::{json, Value};

fn run(m: &Machine, runner: &str, input: &Value, mu: Option<&str>) -> Value {
    spec::run(runner, input, Some(m), mu).unwrap()
}

#[test]
fn fill_boundary_follows_each_bank_capacity() {
    let base = Machine::named("multimesh_v9t6").unwrap();
    for capacity in [64, 128, 256] {
        let m = base
            .with_fact("mg.l1_bank_entries", json!(capacity))
            .unwrap();
        let legal = json!({"op": "Fill", "args": [4096, 8], "kw": {"eoff": capacity - 8}});
        let bad = json!({"op": "Fill", "args": [4096, 8], "kw": {"eoff": capacity - 7}});
        assert!(run(&m, "isa.cluster.word", &legal, None)
            .get("payload")
            .is_some());
        assert_eq!(
            run(&m, "isa.cluster.word", &bad, None),
            json!({"refused": "ValueError"})
        );
        if cfg!(feature = "mutants") {
            assert!(
                run(&m, "isa.cluster.word", &bad, Some("rul.048/fill-wraps"))
                    .get("payload")
                    .is_some()
            );
        }
    }
}

#[test]
fn every_unchecked_hardware_alignment_is_refused_by_the_stack() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let good = json!({"fn": "gather", "args": [4096, [32768, 4], {"base": 131072, "dims": [[4,32]], "windows": [[0,0],[0,0]]}, [64, 1]]});
    assert!(run(&m, "isa.mover.move", &good, None)
        .get("writes")
        .is_some());
    for field in ["source", "index", "pitch"] {
        let mut bad = good.clone();
        match field {
            "source" => bad["args"][0] = json!(4097),
            "index" => bad["args"][1][0] = json!(32769),
            _ => bad["args"][3][0] = json!(48),
        }
        assert_eq!(
            run(&m, "isa.mover.move", &bad, None),
            json!({"refused": "PackageError"}),
            "{field}"
        );
        if cfg!(feature = "mutants") {
            assert!(
                run(&m, "isa.mover.move", &bad, Some("rul.050/unaligned"))
                    .get("writes")
                    .is_some(),
                "{field}"
            );
        }
    }
}

#[test]
fn fault_codes_are_the_selected_machine_fact() {
    let m = Machine::named("multimesh_v9t6")
        .unwrap()
        .with_fact("vc.faults", json!([5, 9]))
        .unwrap();
    assert_eq!(ktpu_isa::spec::fault_codes(&m), vec![5, 9]);
}

#[test]
fn mode_lookup_uses_the_machine_table_and_refuses_absent_codes() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let isa = ktpu_isa::MoverIsa::of(&m);
    assert_eq!(
        isa.mode(m.mover.modes.scatter as u64),
        Some(ktpu_isa::unit::mover::MoveMode::Scatter)
    );
    assert_eq!(isa.mode(7), None);
    assert_eq!(isa.mode(u64::MAX), None);
}
