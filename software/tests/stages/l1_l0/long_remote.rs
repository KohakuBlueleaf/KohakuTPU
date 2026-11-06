use std::collections::BTreeMap;

use ktpu_l1::build::programs;
use ktpu_l1::text::read;

use super::common::card;

#[test]
fn remote_burst_health_follows_emitted_lengths_and_sending_mesh() {
    for present in [false, true] {
        let machine = card()
            .with_fact("flaws.F-015.present", present.into())
            .expect("flaw setting");
        for (words, expected) in [(32, false), (33, true), (64, true)] {
            for external in [false, true] {
                let desc =
                    format!("desc a0 = peer(0, mesh=1, fin=(1, 0), ack=(1, 0)) walk=(1, {words})");
                let (prefix, setup) = if external {
                    (String::new(), format!("            {desc}\n"))
                } else {
                    (format!("    {desc}\n"), String::new())
                };
                let source = format!(
                    "image drain()
{prefix}    vdrain a0 <- l1[0] to=(0, 1)
    halt
fn burst.l1()
    node 0
        send vc[0]
{setup}            run drain()
    node 1
        signal 0
"
                );
                let module = read(&source, "remote burst").expect("L1 source");
                let built = programs(&module, "burst", &BTreeMap::new(), &machine, Some(2), None)
                    .expect("node programs");
                assert_eq!(built[0].long_remote, expected, "{source}");
                assert!(
                    !built[1].long_remote,
                    "only the sending mesh loses ACK0 evidence"
                );
            }
        }
        let source = "image drain()
    vdrain a0 <- l1[0] to=(0, 1)
    halt
fn burst.l1()
    send vc[0]
        run drain()
";
        let module = read(source, "unknown descriptor").expect("L1 source");
        let built = programs(&module, "burst", &BTreeMap::new(), &machine, Some(2), None)
            .expect("node programs");
        assert!(
            built[0].long_remote,
            "an unknown remote length cannot certify ACK0"
        );
        let isa = ktpu_isa::unit::vector::VecIsa::new(&machine).expect("vector ISA");
        let (bit, width) = isa.desc.span("ad");
        let source = format!(
            "fn setup.l1()
    send vc[0] repeats=2 increments=((0, {bit}, {width}, 1),)
        desc a0 = peer(0, mesh=1, fin=(1, 0)) walk=(1, 33)
"
        );
        let module = read(&source, "descriptor setup").expect("L1 source");
        let built = programs(&module, "setup", &BTreeMap::new(), &machine, Some(2), None)
            .expect("node programs");
        assert!(
            !built[0].long_remote,
            "descriptor setup alone emits no vector burst"
        );
        let module = read(
            "fn fetched.l1()
    fetch vc[0] from 4096 n=1
",
            "opaque vector commands",
        )
        .expect("L1 source");
        let built = programs(
            &module,
            "fetched",
            &BTreeMap::new(),
            &machine,
            Some(2),
            None,
        )
        .expect("node programs");
        assert!(built[0].long_remote, "fetched DMA lengths are not static");
    }
}
