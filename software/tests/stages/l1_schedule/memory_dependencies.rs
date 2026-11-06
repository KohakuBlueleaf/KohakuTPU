use ktpu_isa::unit::vector::VecIsa;
use ktpu_l1::lower::image::CoreFacts;
use ktpu_l1::sched::preds;
use ktpu_l1::text::parse;

use super::common::card;

#[test]
fn memory_forms_keep_dependencies() {
    let machine = card();
    let isa = VecIsa::new(&machine).expect("ISA");
    let facts = CoreFacts::of(&machine, machine.vc.regs);
    let wrapped = format!(
        "vpack v0 -> l1[{}] f32 n=1\nvunpk v1 <- l1[0] f16 n=1",
        machine.vc.l1_words - 1
    );
    for (source, written, read) in [
        (
            "vpack v0 -> l1[0] f32 n=1\nvunpk v1 <- l1[1] f16 n=1",
            vec![0, 1],
            1,
        ),
        (
            "vpack v0 -> l1[0] f16 n=2 walk=(2,8)\nvunpk v1 <- l1[2] f16 n=1",
            vec![0, 2],
            2,
        ),
        (
            "vpack v0 -> l1[0] f16 n=1 rel\nvunpk v1 <- l1[1] f16 n=1",
            vec![1],
            1,
        ),
        (
            wrapped.as_str(),
            vec![machine.vc.l1_words as usize - 1, 0],
            0,
        ),
    ] {
        let run = parse(source, "dependencies").expect("image statements");
        let dependencies = preds(&run, &isa, &facts);
        for order in [[0, 1], [1, 0]] {
            let mut issued = [false; 2];
            let mut memory = vec![0; machine.vc.l1_words as usize];
            let mut unpacked = None;
            let mut permitted = true;
            for i in order {
                if dependencies[i].iter().any(|&p| !issued[p]) {
                    permitted = false;
                    break;
                }
                issued[i] = true;
                if i == 0 {
                    for &word in &written {
                        memory[word] = 9;
                    }
                } else {
                    unpacked = Some(memory[read]);
                }
            }
            if permitted {
                assert_eq!(unpacked, Some(9), "permitted order {order:?}: {source}");
            }
        }
    }
}

#[test]
fn absolute_walks_can_reorder_when_their_normalized_words_are_disjoint() {
    let machine = card();
    let isa = VecIsa::new(&machine).expect("ISA");
    let facts = CoreFacts::of(&machine, machine.vc.regs);
    for source in [
        "vpack v0 -> l1[8] f16 n=2\nvunpk v1 <- l1[12] f16 n=2",
        "vpack v0 -> l1[8] f32 n=1\nvunpk v1 <- l1[10] f16 n=1",
        "vpack v0 -> l1[8] f16 n=2 walk=(2,8)\nvunpk v1 <- l1[9] f16 n=1",
    ] {
        let run = parse(source, "independent memory").expect("image statements");
        let dependencies = preds(&run, &isa, &facts);
        assert!(
            dependencies[1].is_empty(),
            "reverse issue must remain legal: {source}"
        );
    }
}
