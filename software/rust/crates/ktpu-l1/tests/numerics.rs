//! Numerics at the L1 image and host-layout boundaries.

use ktpu_l1::layout::buffer::Buffer;
use ktpu_l1::layout::tensor::fp16_values;
use ktpu_l1::lower::image::{image, UNKNOWN};
use ktpu_machine::Machine;
use std::collections::BTreeMap;

#[test]
fn rul_056_seti_rne_and_overflow() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let module = ktpu_l1::read(
        "target ktpu.v9\nimage k()\n    seti s0 = 1.00002288818359375\n    seti s1 = 1e39\n    halt\n",
        "<numerics>",
    ).unwrap();
    let words = || image(&module, &m, "k", &[], UNKNOWN).unwrap().0;
    let got = words();
    assert_eq!(got[1], 0x3F8001);
    assert_eq!(got[3], 0x7F8000);
    #[cfg(feature = "mutants")]
    assert_ne!(ktpu_l1::mutant::with("l1.img/e8m15", words), got);
}

#[test]
fn rul_057_host_writes_saturate_and_reads_name_the_consumer() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let b = Buffer {
        dtype: "f16".into(),
        shape: vec![4],
        space: "dram".into(),
        layout: BTreeMap::new(),
    };
    let got = b
        .pack_values(&[70000.0, -1000000.0, 65520.0, 65504.0], &m)
        .unwrap();
    assert_eq!(got, [0xFF, 0x7B, 0xFF, 0xFB, 0xFF, 0x7B, 0xFF, 0x7B]);
    let bits = [0x7C00, 1, 0x7BFF];
    assert_eq!(
        fp16_values(&bits, "vector").unwrap(),
        [f64::INFINITY, 2f64.powi(-24), 65504.0]
    );
    for consumer in ["sru", "cluster"] {
        assert_eq!(
            fp16_values(&bits, consumer).unwrap(),
            [65536.0, 0.0, 65504.0]
        );
    }
    assert!(fp16_values(&bits, "unknown").is_err());
}
