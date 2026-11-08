//! Whole-head owners share waves while retaining each head's packed operands.
use super::*;

#[test]
fn whole_head_owners_share_waves_without_sharing_weights() {
    let m = machine();
    for depth in [0, 2] {
        let args = json!({
            "mm": {"made_by": {"gen": "matmul.plan", "args": {
                "bt": 6, "m": 7, "n": 64, "k": 64, "shared": false,
                "pad_m": false, "dense_n": true
            }}},
            "e": {"nodes": [{"load": 0}, {"const": 0.125},
                {"op": "mul", "args": [0, 1]}], "root": 2},
            "extras": [],
            "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 64, "rows": 48},
            "ring_depth": depth
        });
        let reference = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("ordered product");
        let mut req = request(&m);
        req.force = Some("groups-tile-4x64".into());
        let separate = call_split("matmul.mm_kernel", &args, &req, &m).expect("separate batches");
        req.force = Some("groups-batch-4x64".into());
        let batched = call_split("matmul.mm_kernel", &args, &req, &m).expect("owner batch");
        validate_ownership(&batched.split.output).expect("physical ownership");
        let (want, _) = simulate_matmul(&reference);
        let (actual, writes) = simulate_matmul(&batched.kernel);
        assert_eq!(actual, want);
        assert!(writes.iter().all(|&count| count == 1));
        assert!(batched.split.cost.makespan < separate.split.cost.makespan);
        req.force = None;
        let automatic = call_split("matmul.mm_kernel", &args, &req, &m).expect("price selection");
        assert!(automatic.split.cost.makespan <= batched.split.cost.makespan);
    }
}
