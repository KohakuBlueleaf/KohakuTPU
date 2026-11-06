//! Cluster products retain their exact output extent through drain lowering.

use std::collections::BTreeMap;

use ktpu::{Backend, CardStack};
use ktpu_isa::Isa;
use ktpu_l0::Stmt;
use ktpu_l1::text::read;

use super::common::card;

fn field(fields: &[(&str, i64)], name: &str) -> u64 {
    fields
        .iter()
        .find(|(n, _)| *n == name)
        .expect("encoded field")
        .1 as u64
}

#[test]
fn b_entry_products_copy_only_the_crossing_drain() {
    for entries in [63, 65] {
        for fused in [false, true] {
            let words = 4 * entries;
            let bytes = words * 32;
            let mut text = format!(
                "target ktpu.v9\nfn product.l1(x: mx7a[32, 32] @dram(gm=4, nk=1), w: mx7b[{}, 32] @dram(gn={entries}, nk=1), y: f16[64, {}] @dram(tile=16x{entries}))\n",
                4 * entries, 4 * entries,
            );
            for u in 0..2 {
                let off = u * bytes;
                let emit = if fused {
                    format!(" emit=y + {off}")
                } else {
                    String::new()
                };
                let collect = if fused { " fused" } else { "" };
                text += &format!(
                    "    send mg[{u}]\n        fill A0 <- x + {} n=4\n        fill B0 <- w n={entries}\n        gemm 4x{entries}x1 a=A0 b=B0{emit}\n        drain y + {off} n={words}{collect}\n",
                    u * 512,
                );
            }
            text += "    barrier\n";
            let module = read(&text, "product").expect("L1 product");
            for explicit in [false, true] {
                for present in [false, true] {
                    let m = card()
                        .with_fact("flaws.F-016.present", present.into())
                        .expect("flaw setting");
                    let isa = Isa::new(&m).expect("ISA");
                    let backend = CardStack {
                        binds: explicit.then(|| {
                            BTreeMap::from([
                                ("x".into(), 0x10000),
                                ("w".into(), 0x20000),
                                ("y".into(), 0x30000),
                            ])
                        }),
                        node_fed: true,
                        ..CardStack::default()
                    };
                    let p = backend
                        .programs(&module, "product", None, &m)
                        .expect("product lowers");
                    let mut drains = Vec::new();
                    let mut emits = Vec::new();
                    let mut copies = Vec::new();
                    let mut waits = Vec::new();
                    for (index, st) in p.nodes()[0].program.steps.iter().enumerate() {
                        match st {
                            Stmt::Send { coord, words, .. } => {
                                if !m.die.units.mg.iter().any(|c| (c.x(), c.y()) == *coord) {
                                    continue;
                                }
                                for w in words {
                                    let word = ktpu_isa::W256(w.0);
                                    let fmt = isa.cluster.set.find(word).expect("cluster word");
                                    let fields = fmt.decode(word).expect("cluster fields");
                                    let address =
                                        field(&fields, "addr") | field(&fields, "addr_hi") << 34;
                                    if fmt.name == "DRAIN" {
                                        drains.push((index, *coord, address));
                                    } else if fmt.name == "GEMM" && field(&fields, "emit") != 0 {
                                        emits.push(address);
                                    }
                                }
                            }
                            Stmt::Wait { coord, .. } => waits.push((index, *coord)),
                            Stmt::Move { writes, .. } => {
                                let raw: Vec<_> =
                                    writes.iter().map(|(r, v)| (*r as u32, *v)).collect();
                                let fields = isa.mover.fields(&raw);
                                let value = |name: &str| {
                                    let (_, (i, bit, width)) = fields
                                        .iter()
                                        .find(|((_, n), _)| n == name)
                                        .expect("copy walker");
                                    (raw[*i].1 >> bit) & ((1u64 << width) - 1)
                                };
                                copies.push((
                                    index,
                                    value("src.base"),
                                    value("dst.base"),
                                    value("dst.count0"),
                                ));
                            }
                            _ => {}
                        }
                    }
                    let first = drains[0].2;
                    let real = first + bytes;
                    if present {
                        let scratch = drains[1].2;
                        assert_ne!(
                            scratch, real,
                            "the page-crossing drain must not write y directly"
                        );
                        assert_eq!(scratch % (u64::from(m.mg.wburst) * 32), 0);
                        let copy = copies
                            .iter()
                            .find(|c| c.1 == scratch && c.2 == real)
                            .expect("scratch reaches the original output");
                        assert_eq!(copy.3, words, "the copy writes exactly the drained words");
                        assert!(
                            waits
                                .iter()
                                .any(|(i, c)| drains[1].0 < *i && *i < copy.0 && *c == drains[1].1),
                            "copy waits for its drain"
                        );
                        assert!(backend.scratch(&p).iter().any(|a| a.bytes >= bytes));
                    } else {
                        assert_eq!(drains[1].2, real, "flaw-free drain stays direct");
                        assert!(copies.is_empty(), "no copy on a flaw-free machine");
                        assert!(backend.scratch(&p).is_empty());
                    }
                    if fused {
                        assert_eq!(
                            emits,
                            drains.iter().map(|d| d.2).collect::<Vec<_>>(),
                            "emit and collection use the same memory destination"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn register_counted_copy_keeps_the_extent_and_live_node_registers() {
    let module = read(
        "target ktpu.v9\nfn counted.l1(y: u8[8192] @dram)\n    r0 = set 5\n    r1 = set 7\n    r2 = set 9\n    send mg[0]\n        drain y + 3968 n=r0\n    barrier\n",
        "counted",
    )
    .expect("register-counted drain");
    for present in [false, true] {
        let m = card()
            .with_fact("flaws.F-016.present", present.into())
            .expect("flaw setting");
        let isa = Isa::new(&m).expect("ISA");
        let backend = CardStack {
            binds: Some(BTreeMap::from([("y".into(), 0x10000)])),
            node_fed: true,
            ..CardStack::default()
        };
        let p = backend
            .programs(&module, "counted", None, &m)
            .expect("node program");
        let steps = &p.nodes()[0].program.steps;
        let labels: BTreeMap<_, _> = steps
            .iter()
            .enumerate()
            .filter_map(|(i, s)| {
                if let Stmt::Label(n) = s {
                    Some((n.clone(), i))
                } else {
                    None
                }
            })
            .collect();
        let mut regs = [0u64; 16];
        let mut saved = BTreeMap::new();
        let mut copied = Vec::new();
        let mut pc = 0;
        let mut visits = 0;
        while let Some(st) = steps.get(pc) {
            visits += 1;
            assert!(visits < 1000, "the finite copy must terminate");
            let mut next = pc + 1;
            match st {
                Stmt::Alu { rd, op, ra, b, imm } => {
                    let a = regs[*ra as usize];
                    let b = if *imm { *b } else { regs[*b as usize] };
                    regs[*rd as usize] = match op {
                        ktpu_l0::Alu::Mov => b,
                        ktpu_l0::Alu::Add => a.wrapping_add(b),
                        ktpu_l0::Alu::Sub => a.wrapping_sub(b),
                        ktpu_l0::Alu::And => a & b,
                        other => panic!("unexpected copy ALU {other:?}"),
                    };
                }
                Stmt::Store { pairs, from_regs } => {
                    for (at, v) in pairs {
                        saved.insert(*at, if *from_regs { regs[*v as usize] } else { *v });
                    }
                }
                Stmt::Load { reg, address, .. } => regs[*reg as usize] = saved[address],
                Stmt::Jump {
                    name,
                    cond,
                    ra,
                    b,
                    imm,
                } => {
                    let b = if *imm { *b } else { regs[*b as usize] };
                    let take = match cond {
                        ktpu_l0::Cond::Always => true,
                        ktpu_l0::Cond::Eq => regs[*ra as usize] == b,
                        other => panic!("unexpected copy condition {other:?}"),
                    };
                    if take {
                        next = labels[name];
                    }
                }
                Stmt::Move { writes, patches } => {
                    let mut raw: Vec<_> = writes.iter().map(|(r, v)| (*r as u32, *v)).collect();
                    for patch in patches {
                        let value = regs[patch.reg as usize].wrapping_add(patch.konst);
                        for &(bit, width, shift) in &patch.segments {
                            let mask = (1u64 << width) - 1;
                            raw[patch.word].1 = (raw[patch.word].1 & !(mask << bit))
                                | ((value >> shift) & mask) << bit;
                        }
                    }
                    let fields = isa.mover.fields(&raw);
                    let value = |name: &str| {
                        let (_, (i, bit, width)) = fields
                            .iter()
                            .find(|((_, n), _)| n == name)
                            .expect("copy field");
                        (raw[*i].1 >> bit) & ((1u64 << width) - 1)
                    };
                    for i in 0..value("dst.count0") {
                        copied.push((value("src.base") + 32 * i, value("dst.base") + 32 * i));
                    }
                }
                Stmt::Send { .. } | Stmt::Wait { .. } | Stmt::Label(_) | Stmt::Barrier => {}
                other => panic!("unexpected copy statement {other:?}"),
            }
            pc = next;
        }
        assert_eq!(&regs[..3], &[5, 7, 9], "copy restores live node registers");
        if present {
            let scratch = copied.first().expect("scratch must be copied").0;
            assert_eq!(
                copied,
                (0..5)
                    .map(|i| (scratch + i * 32, 0x10000 + 3968 + i * 32))
                    .collect::<Vec<_>>(),
                "only the runtime drain extent is copied"
            );
        } else {
            assert!(copied.is_empty(), "flaw-free drains need no copy");
        }
    }
}
