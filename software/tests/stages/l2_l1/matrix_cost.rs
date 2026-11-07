//! The emitted MG schedule owns compute service and its physical memory bus.
use ktpu_l1::text::read;
use ktpu_l2::cost;
use ktpu_l2::target::Target;

use super::common::card;

#[test]
fn matrix_service_and_shared_reads_extend_completion() {
    let machine = card();
    let target = Target::of(&machine);
    let price = |entries: usize, groups: usize, copies: usize, separate: bool| {
        let mut source = String::from(concat!(
            "target ktpu.v9\n",
            "fn resource.l1(a: mx7a[256,256] @dram(gm=64,nk=1), ",
            "b: mx7b[256,256] @dram(gn=64,nk=1))\n",
        ));
        for i in 0..copies {
            let node = if separate { i } else { 0 };
            let unit = if separate { 0 } else { i };
            if separate || i == 0 {
                source.push_str(&format!("    node {node}\n"));
            }
            source.push_str(&format!(
                "        send mg[{unit}]\n            fill A0 <- a n={entries}\n            fill B0 <- b n={entries}\n            gemm {groups}x{groups}x1 a=A0 b=B0 acc=0\n",
            ));
        }
        let module = read(&source, "MG service").expect("L1");
        cost::cycles(&module, "resource", &target).expect("resource price")
    };
    // The instruction count is identical; only the emitted service changes.
    assert!(price(255, 1, 1, false) > price(1, 1, 1, false));
    assert!(price(64, 64, 1, false) > price(64, 1, 1, false));
    // Identical fill traffic shares one return bus, not the four-sysnode bus sum.
    assert!(price(255, 1, 4, false) > price(255, 1, 4, true));
}
