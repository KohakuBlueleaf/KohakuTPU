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
