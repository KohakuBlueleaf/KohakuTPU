use ktpu_isa::layout::value;
use ktpu_l1::lower::image::{CoreFacts, Encoder, UNKNOWN};
use ktpu_l1::text::{parse, Reader};

use super::common::card;

#[test]
fn skipped_increment_does_not_initialize_relative_transfers() {
    let machine = card();
    let isa = ktpu_isa::unit::vector::VecIsa::new(&machine).expect("ISA");
    let facts = CoreFacts::of(&machine, machine.vc.regs);
    for transfer in ["vfill a0 -> l1[0] rel", "vdrain a0 <- l1[0] rel"] {
        let source = format!("skipz s0\n    ainc a0 += 32\n{transfer}\nhalt\n");
        let reader = Reader::new(&source, "increment");
        let stmts = parse(&source, "increment").expect("image");
        let words = Encoder::new(&reader, &isa, &facts, UNKNOWN, None)
            .encode(&stmts)
            .expect("encoded image");
        let mut pc = 0;
        let mut increment = 96;
        let mut address = 4096;
        while pc < words.len() {
            let (op, fields) = isa.decode_word(words[pc]).expect("instruction");
            pc += 1;
            match op {
                "AINC" => increment = value(&fields, "inc") as i64,
                "VSKIPZ" => pc += value(&fields, "skip") as usize,
                "VFILL" | "VDRAIN" => address += increment,
                "VHALT" => break,
                _ => panic!("unexpected operation {op}"),
            }
        }
        assert_eq!(address, 4096, "s0=0 leaves the descriptor base unchanged");
    }
}
