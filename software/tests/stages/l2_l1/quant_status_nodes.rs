//! Deferred finite records remain live across sysnode scopes.

use super::{common, lower, Fixture};

#[test]
fn a_remote_quant_check_observes_the_producer_status() {
    let producer =
        "    par producer in 1..2 on node[producer]\n        quant q <- x backend=vector_mx7 status=status\n";
    let consumer = "    par consumer in 0..1 on node[consumer]\n        quant_check status\n";
    let fixture = |body: String| Fixture {
        label: "remote-quant-check".into(),
        name: "remote_quant_check".into(),
        source: format!(
            "target ktpu.v9\nfn remote_quant_check.l2(x: f16[8, 32] @dram, q: mx7a[8, 32] @dram(gm=1, nk=1))\n    status = buffer : f16[128] @dram\n{body}"
        ),
    };
    let card = common::card();
    let pair = lower(&fixture(format!("{producer}{consumer}")), &card)
        .expect("producer before its remote check");
    assert_eq!(common::compare(&pair), Ok(None));
    let nan = |memory: &mut [u8], binds: &ktpu_sim::l1::Binds| {
        let at = (binds["x"] + 2 * (7 * 32 + 17)) as usize;
        memory[at..at + 2].copy_from_slice(&0x7E00u16.to_le_bytes());
    };
    let before = common::stops(&pair, false, &nan).expect("L2 finite contract");
    let after = common::stops(&pair, true, &nan).expect("L1 finite contract");
    assert!(before.contains("non-finite"), "{before}");
    assert!(after.contains("fails its assertion"), "{after}");
    for body in [
        "    par n in 0..2 on node[n]\n        when n == 0\n            quant q <- x backend=vector_mx7 status=status\n        when n == 1\n            quant_check status\n".to_string(),
        producer.to_string(),
        format!("{consumer}{producer}"),
        format!(
            "{producer}    par overwrite in 0..1 on node[overwrite]\n        copy status <- x[0 : +4]\n{consumer}"
        ),
    ] {
        let error = lower(&fixture(body), &card).expect_err("live status contract");
        assert!(error.contains("L2.MOV.019"), "{error}");
    }
}
