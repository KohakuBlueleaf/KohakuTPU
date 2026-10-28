//! docs/spec/stack/l0/boot-block.md

use super::*;
use ktpu_l0::boot::BootArgs;
use ktpu_l0::units::machine_units;

#[test]
fn l0_boot_001_block_is_37_words() {
    let b = BootArgs::new(0x80_0000_0000, 1).pack(&f()).unwrap();
    assert_eq!(b.len(), (9 + 16 + 3 * 4) * 8);
}

#[test]
fn rul_024_region_table_after_the_units() {
    let mut a = BootArgs::new(0, 1);
    a.regions = vec![ktpu_l0::boot::Region {
        base: 0x10_0000,
        size: 0x4000,
        use_: 1,
    }];
    let b = a.pack(&f()).unwrap();
    let w: Vec<u64> = (0..37).map(|k| q(&b, 8 * k)).collect();
    assert_eq!(w[1], 3);
    assert_eq!(&w[25..28], [0x10_0000, 0x4000, 1]);
    assert!(w[28..].iter().all(|&x| x == 0));
    a.regions = vec![a.regions[0]; 5];
    assert_eq!(a.pack(&f()).unwrap_err().clause, "RUL.024");
}

#[test]
fn l0_boot_004_words_in_order() {
    let mut a = BootArgs::new(0x80_0000_0000, 9);
    a.mesh = 1;
    a.scan = 3;
    a.cq_depth = 16;
    a.flags = 2;
    a.units = vec![0x4D47_0000_0101, 0x5643_0000_0102];
    let b = a.pack(&f()).unwrap();
    let w: Vec<u64> = (0..25).map(|k| q(&b, 8 * k)).collect();
    assert_eq!(&b[..7], b"KABOOT1");
    assert_eq!(
        &w[..11],
        [
            0x0031_544F_4F42_414B,
            3,
            0x80_0000_0000,
            1,
            3,
            9,
            16,
            2,
            2,
            0x4D47_0000_0101,
            0x5643_0000_0102
        ]
    );
    assert!(w[11..].iter().all(|&x| x == 0));
}

#[test]
fn l0_boot_008_default_cq_depth_and_rul_033_no_default_timeout() {
    let a = BootArgs::new(0, 7);
    assert_eq!((a.timeout, a.cq_depth), (7, 0));
    let got = ktpu_l0::spec::boot::run(&serde_json::json!({"args": {"queue": 0}}), None).unwrap();
    assert_eq!(got, serde_json::json!({"refused": "TypeError"}));
}

#[test]
fn l0_boot_014_more_than_16_units_refused() {
    let mut a = BootArgs::new(0, 1);
    a.units = vec![1; 17];
    assert_eq!(a.pack(&f()).unwrap_err().clause, "L0.BOOT.014");
    a.units.pop();
    assert!(a.pack(&f()).is_ok());
}

#[test]
fn l0_boot_016_unit_words_of_the_machine() {
    let got = machine_units(&f()).unwrap();
    assert_eq!(
        got,
        [
            0x4D47_0000_0101,
            0x4D47_0000_0201,
            0x5643_0000_0102,
            0x5643_0000_0202
        ]
    );
}
