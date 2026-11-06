use std::collections::BTreeSet;

use ktpu_l1::layout::scratch::arenas;
use ktpu_l1::lower::image::{image, UNKNOWN};
use ktpu_l1::reach::{mag_pieces, vector_requests};
use ktpu_l1::text::read;

use super::common::card;

#[test]
fn fill_requests() {
    let module = read(
        "image load()\n    desc a0 = 4096 walk=(32, 256)\n    vfill a0 -> l1[0]\n    halt\n",
        "fill requests",
    )
    .expect("image");
    for present in [false, true] {
        let machine = card()
            .with_fact("flaws.F-014.present", present.into())
            .expect("flaw setting");
        let (_, descriptors) = image(&module, &machine, "load", &[], UNKNOWN).expect("lower");
        let mut addresses = vec![descriptors[&(0, 0)] as i64];
        for f in (1..=4).rev() {
            let value = descriptors[&(0, f)];
            let stride = ((value >> 16) as i64) << 46 >> 46;
            let count = (value & 0xffff).max(1);
            addresses = addresses
                .into_iter()
                .flat_map(|base| (0..count).map(move |i| base + i as i64 * stride))
                .collect();
        }
        assert_eq!(
            addresses,
            (0..256).map(|i| 4096 + i * 32).collect::<Vec<_>>()
        );
        let inner = (descriptors[&(0, 1)] & 0xffff).max(1) as usize;
        let requests = vector_requests(&addresses, 0, inner);
        let words: Vec<_> = requests.iter().map(|r| r.1).collect();
        assert_eq!(
            words,
            if present {
                vec![128, 128]
            } else {
                vec![255, 1]
            }
        );
        let single = requests
            .iter()
            .flat_map(|&(at, n)| mag_pieces(at as u64, n as u64 * 32))
            .any(|(_, beats)| beats == 1);
        assert_eq!(single, !present);
    }
}

#[test]
fn scratch_alignment() {
    let module = read(
        "fn scratch.l1(a: u8[64] @dram, b: u8[64] @dram)\n    barrier\n",
        "scratch",
    )
    .expect("L1");
    for present in [false, true] {
        let machine = card()
            .with_fact("flaws.F-014.present", present.into())
            .expect("flaw setting")
            .with_fact("card.dram.line_bytes", 64.into())
            .expect("write line");
        let allocated = arenas(&module, "scratch", &BTreeSet::new(), &machine).expect("arena");
        let second = allocated[0].members[1].1;
        assert_eq!(second, if present { 128 } else { 64 });
        assert_eq!(allocated[0].bytes, if present { 256 } else { 128 });
    }
}
