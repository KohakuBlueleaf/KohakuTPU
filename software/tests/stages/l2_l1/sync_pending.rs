//! Live dependency queries retain exhaustive wait and hoist order.

use ktpu_l2::lower::sync::{
    mem, reset_marks, schedule_exhaustive, schedule_profiled, Access, Stage,
};

fn repeated(trips: usize, buffers: usize) -> Vec<Stage> {
    let mut stages = Vec::new();
    for trip in 0..trips {
        let base = trip as i64 * 1024;
        let mut producer = Stage::new(
            vec![format!("produce {trip}")],
            vec!["mg[0]".into(), "mg[1]".into()],
            Vec::new(),
        );
        for buffer in 0..buffers {
            let name = format!("buffer_{buffer}");
            // First owner occurrence and interval order are different.
            for (unit, offset) in [("mg[0]", 384), ("mg[1]", 0), ("mg[0]", 128)] {
                producer
                    .accesses
                    .push(mem(unit, true, &name, base + offset, base + offset + 64));
            }
        }
        stages.push(producer);
        let mut consumer = Stage::new(
            vec![format!("consume {trip}")],
            vec!["vc[0]".into()],
            vec![
                mem(
                    "vc[0]",
                    false,
                    &format!("buffer_{}", buffers - 1),
                    base,
                    base + 512,
                ),
                Access::strided("vc[0]", true, "columns", base, &[(128, 2)], 64),
            ],
        );
        consumer.group = Some((trip, 0, 2));
        stages.push(consumer);
        let mut bounded = Stage::new(
            vec![format!("column {trip}")],
            vec!["vc[1]".into()],
            vec![mem("vc[1]", false, "columns", base + 64, base + 128)],
        );
        bounded.group = Some((trip, 1, 2));
        stages.push(bounded);
        let mut independent = Stage::new(
            vec![format!("independent {trip}")],
            vec!["mg[2]".into()],
            vec![mem("mg[2]", false, "separate", base, base + 64)],
        );
        independent.group = Some((trip, 1, 2));
        stages.push(independent);
    }
    stages.push(Stage::new(
        vec!["revisit completed region".into()],
        vec!["vc[2]".into()],
        vec![mem("vc[2]", false, "buffer_0", 0, 512)],
    ));
    stages
}

#[test]
fn repeated_trip_pending_queries_visit_conflicts_and_keep_the_exhaustive_schedule() {
    let small = repeated(3, 4);
    reset_marks();
    let (indexed, _) = schedule_profiled(&small);
    reset_marks();
    let (reference, _) = schedule_exhaustive(&small);
    assert_eq!(indexed, reference, "wait order, completion and hoisting");

    let mut visits = Vec::new();
    for trips in [16, 64] {
        let stages = repeated(trips, 64);
        let accesses: usize = stages.iter().map(|s| s.accesses.len()).sum();
        let (_, work) = schedule_profiled(&stages);
        assert!(
            work <= 32 * accesses + 128 * stages.len(),
            "{trips} trips visited {work} index nodes for {accesses} effects"
        );
        visits.push(work);
    }
    assert!(
        visits[1] <= 6 * visits[0],
        "quadrupling trips must not rescan the pending history: {visits:?}"
    );
}
