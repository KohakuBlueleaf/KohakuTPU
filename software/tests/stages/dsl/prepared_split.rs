//! Prepared row shares address the destination coordinates of composed maps.

use super::{machine, request, simulate_matmul};
use ktpu_kernels::call_split;
use ktpu_kernels::split::NodeSet;
use serde_json::json;

#[test]
fn row_shares_slice_the_consumer_not_the_relayout_iteration_axis() {
    let m = machine();
    let args = json!({
        "R": 40, "W": 64, "alias": {}, "composed": [0],
        "e": {"nodes": [{"load": 0}], "root": 0},
        "made": {"0": [{"made_by": {"gen": "relayout.plan", "args": {
            "at": 0, "dat": 0, "dwalk": [[40, 1], [64, 40]], "keep": false,
            "m": 2560, "src_n": 2560, "tree": true,
            "walk": [[40, 64], [64, 1]], "z0": 0}}}]},
        "operands": [
            {"dtype": "f16", "kind": "full", "offset": 0, "pitch": 64, "rows": 0}
        ],
        "out": {"dtype": "f16", "kind": "full", "offset": 0, "pitch": 64, "rows": 0}
    });
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let plan = call_split("prepared.rows_kernel", &args, &req, &m)
        .expect("consumer rows of a composed permutation");
    let (actual, writes) = simulate_matmul(&plan.kernel);
    let expected: Vec<u8> = (0..2560)
        .flat_map(|destination| {
            let source = (destination % 40) * 64 + destination / 40;
            (0x2800u16 | ((source * 37) as u16 & 0x3ff)).to_le_bytes()
        })
        .collect();
    assert_eq!(actual, expected, "each output uses its inverse-map input");
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn table_independent_relayout_prices_destination_owners_against_p1() {
    let m = machine();
    let args = json!({"pl": {"made_by": {"gen": "relayout.plan", "args": {
        "src_n": 37440, "m": 12480, "at": 0, "dat": 0,
        "walk": [[3, 64], [65, 576], [64, 1]],
        "dwalk": [[3, 4160], [65, 64], [64, 1]], "keep": false, "z0": 0, "tree": true
    }}}});
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("permuted grant");
    req.force = Some("p1".into());
    let original = ktpu_kernels::call::call_split_with_timing(
        "relayout.relayout_kernel",
        &args,
        &req,
        &m,
        true,
    )
    .expect("priced whole relayout");
    req.force = Some("destination-tiles".into());
    let distributed = call_split("relayout.relayout_kernel", &args, &req, &m)
        .expect("table-independent destination owners");
    ktpu_kernels::split::validate_ownership(&distributed.split.output)
        .expect("whole-line ownership");
    let sample = |i: i64| (i as u16).wrapping_mul(37);
    let (actual, writes) = super::simulate_named_tables(
        &distributed.kernel,
        "dst",
        &|_, _, _, _, _| 0,
        &sample,
        &[("index", vec![0xff; 32]), ("mask", vec![0xff; 256])],
    );
    let expected: Vec<u8> = (0..12480i64)
        .flat_map(|i| {
            let head = i / (65 * 64);
            let token = (i / 64) % 65;
            sample(token * 576 + head * 64 + i % 64).to_le_bytes()
        })
        .collect();
    assert_eq!(actual, expected);
    assert!(writes.iter().all(|&n| n == 1));
    assert!(distributed.split.cost.makespan < original.split.cost.makespan);
    req.force = None;
    let selected = call_split("relayout.relayout_kernel", &args, &req, &m)
        .expect("minimum complete relayout price");
    assert!(selected.split.cost.makespan <= distributed.split.cost.makespan);
}
