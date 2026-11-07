//! Scheduling repeated MG effects retains dependency and pipe ordering.

use std::sync::mpsc;
use std::time::Duration;

use ktpu_l2::lower::sync::{mem, schedule, Access, Stage};

#[test]
fn repeated_matrix_effects_keep_the_release_frontier() {
    let mut stages = Vec::new();
    let mut expected = Vec::new();
    let waves = 80;
    let chunks = 96;
    for wave in 0..waves {
        let mut stage = Stage::default();
        for core in 0..4 {
            let unit = format!("mg[{core}]");
            stage.units.push(unit.clone());
            let line = format!("send {unit}");
            stage.lines.push(line.clone());
            expected.push(line);
            for chunk in 0..chunks {
                let bank = chunk % 2 * 128;
                let a = (wave * 4 + core) * chunks * 128 + chunk * 128;
                let b = core * chunks * 1024 + chunk * 1024;
                stage.accesses.extend([
                    Access::new(&unit, true, (&unit, "A"), bank, bank + 1),
                    Access::new(&unit, true, (&unit, "rx"), 0, 1),
                    mem(&unit, false, "a", a, a + 128),
                    Access::new(&unit, true, (&unit, "B"), bank, bank + 8),
                    Access::new(&unit, true, (&unit, "rx"), 0, 1),
                    mem(&unit, false, "b", b, b + 1024),
                    Access::new(&unit, false, (&unit, "A"), bank, bank + 1),
                    Access::new(&unit, false, (&unit, "B"), bank, bank + 8),
                ]);
            }
            let out = (wave * 4 + core) * 256;
            stage.accesses.push(mem(&unit, true, "out", out, out + 256));
        }
        stages.push(stage);
    }
    let mut reads = Vec::new();
    for core in 0..4 {
        let out = ((waves - 1) * 4 + core) * 256;
        reads.push(mem("vc[0]", false, "out", out, out + 256));
        expected.push(format!("k{}_mg{core} = mark mg[{core}]", waves - 1));
    }
    for core in 0..4 {
        expected.push(format!("wait mg[{core}] upto=k{}_mg{core}", waves - 1));
    }
    expected.extend(["send vc[0]".into(), "barrier".into()]);
    stages.push(Stage::new(
        vec!["send vc[0]".into()],
        vec!["vc[0]".into()],
        reads,
    ));
    let state = ktpu_l2::mutant::current();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let lines = ktpu_l2::mutant::scoped(state, || schedule(&stages));
        let _ = tx.send(lines);
    });
    let lines = rx
        .recv_timeout(Duration::from_secs(2))
        .expect("repeated matrix effects must schedule within the stage admission budget");
    assert_eq!(lines, expected);
    conflict_order_and_hoist_bounds();
}

fn conflict_order_and_hoist_bounds() {
    let producer = Stage::new(
        vec!["producer".into()],
        vec!["mg[0]".into(), "mg[1]".into()],
        vec![
            mem("mg[0]", true, "x", 0, 64),
            mem("mg[1]", true, "x", 64, 128),
            mem("mg[0]", true, "x", 128, 192),
        ],
    );
    let mut consumer = Stage::new(
        vec!["consumer".into()],
        vec!["vc[0]".into()],
        vec![
            mem("vc[0]", false, "x", 64, 192),
            Access::strided("vc[0]", true, "y", 0, &[(128, 2)], 64),
        ],
    );
    consumer.group = Some((1, 0, 2));
    let mut middle = Stage::new(
        vec!["middle".into()],
        vec!["vc[1]".into()],
        vec![mem("vc[1]", false, "y", 64, 128)],
    );
    middle.group = Some((1, 1, 2));
    let mut independent = Stage::new(
        vec!["independent".into()],
        vec!["mg[2]".into()],
        vec![mem("mg[2]", true, "z", 0, 64)],
    );
    independent.group = Some((1, 1, 2));
    assert_eq!(
        schedule(&[producer, consumer, middle, independent]),
        [
            "producer",
            "k0_mg0 = mark mg[0]",
            "k0_mg1 = mark mg[1]",
            "independent",
            "wait mg[1] upto=k0_mg1",
            "wait mg[0] upto=k0_mg0",
            "consumer",
            "middle",
            "barrier",
        ]
        .map(str::to_owned)
    );
}
