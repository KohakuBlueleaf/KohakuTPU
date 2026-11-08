//! Descriptor declarations replace every walk dimension.
use ktpu_l1::sched::timing::cycles_of;
use ktpu_l1::text::read;

use super::common::card;

#[test]
fn an_omitted_walk_resets_the_previous_transfer_length() {
    let machine = card();
    let source = r#"target ktpu.v9
image transfer()
    desc a0 = 4096 walk=(32,64)
    desc a0 = 4096
    vfill a0 -> l1[0]
    vsync fe on=fill
    halt
"#;
    let explicit = source.replace("desc a0 = 4096\n", "desc a0 = 4096 walk=(32,1)\n");
    let price = |text: &str| {
        let module = read(text, "descriptor reset").expect("image");
        cycles_of(
            &module,
            "transfer",
            machine.vc.regs,
            machine.vc.rf_copies,
            (20, 2),
            &machine,
        )
        .expect("core timing")
    };
    assert_eq!(price(source), price(&explicit));
}
