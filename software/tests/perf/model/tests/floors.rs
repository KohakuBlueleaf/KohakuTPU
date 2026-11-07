//! Resource floors follow physical owners, while work remains card-wide.
use std::path::Path;

use ktpu_isa::Machine;
use ktpu_l1::text::read;
use ktpu_perf::model::{kernel, Params};
use serde_json::Value;

#[test]
fn floors_follow_the_busiest_physical_owner() {
    let perf = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("perf");
    let m = Machine::load(perf.join("../../../machines/multimesh_v9t8.json"))
        .expect("four-node machine");
    let params: Value = serde_json::from_str(
        &std::fs::read_to_string(perf.join("params.json")).expect("parameters"),
    )
    .expect("parameter JSON");
    let p = Params::of(&params, "value", &m);
    let measure = |owners: &[(usize, usize)]| {
        let mut source = String::from(concat!(
            "target ktpu.v9\nimage math()\n    vadd v1, v2, v3\n    halt\n\n",
            "fn owners.l1(a: mx7a[64,64] @dram(gm=16,nk=2), ",
            "b: mx7b[64,64] @dram(gn=16,nk=2), ",
            "y: f16[64,64] @dram(tile=16x16))\n",
        ));
        for &(node, trips) in owners {
            source.push_str(&format!("    node {node}\n"));
            for _ in 0..trips {
                source.push_str(concat!(
                    "        send mg[0]\n",
                    "            fill A0 <- a n=32\n",
                    "            fill B0 <- b n=32\n",
                    "            gemm 16x16x2 a=A0 b=B0 acc=0\n",
                    "            drain y n=256\n",
                    "        send vc[0]\n",
                    "            run math()\n",
                    "        move\n",
                    "            copy w(y, (8,32)) -> w(y, (8,32))\n",
                    "        barrier\n",
                ));
            }
        }
        let module = read(&source, "physical floors").expect("L1");
        kernel(&module, "owners", &m, &p).expect("timing")
    };
    for owners in [
        vec![(3, 1), (1, 1), (0, 1), (2, 1)],
        vec![(3, 3), (1, 1)],
        vec![(2, 4)],
    ] {
        let card = measure(&owners);
        let singles: Vec<_> = owners.iter().map(|&owner| measure(&[owner])).collect();
        for resource in ["mg", "vc", "read", "write"] {
            let expected = singles
                .iter()
                .map(|s| s["floor"][resource].as_f64().expect("floor"))
                .fold(0.0, f64::max);
            assert_eq!(
                card["floor"][resource].as_f64().expect("card floor"),
                expected
            );
        }
        for resource in [
            "macs",
            "math_beats",
            "read_bytes",
            "write_bytes",
            "mover_bytes",
        ] {
            let expected: f64 = singles
                .iter()
                .map(|s| s["work"][resource].as_f64().expect("work"))
                .sum();
            assert_eq!(
                card["work"][resource].as_f64().expect("card work"),
                expected
            );
        }
    }
}
