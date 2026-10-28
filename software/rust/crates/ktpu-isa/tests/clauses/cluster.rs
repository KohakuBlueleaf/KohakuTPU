//! `docs/spec/stack/isa/cluster.md`.

use super::{at, isa, machine, refused};
use ktpu_isa::unit::cluster::{Drain, Fill, Gemm, Post, ANCHOR};
use ktpu_isa::{ClusterInst, Coord, W256};

fn op(w: W256) -> u64 {
    w.get(252, 4)
}

#[test]
fn isa_mg_001_units_from_the_machine() {
    let m = machine();
    assert_eq!(m.units(ktpu_isa::UnitKind::Mg).len() as u32, m.mg.count);
}

#[test]
fn isa_mg_002_one_payload_opcode_at_the_top() {
    let i = isa();
    assert_eq!(i.cluster.fill.width, machine().die.noc.data_bits);
    let fill = ClusterInst::Fill(Fill {
        n: 1,
        ..Fill::default()
    })
    .encode(&i.cluster)
    .unwrap();
    let gemm = ClusterInst::Gemm(Gemm {
        gm: 1,
        gn: 1,
        ..Gemm::default()
    })
    .encode(&i.cluster)
    .unwrap();
    let drain = ClusterInst::Drain(Drain::default())
        .encode(&i.cluster)
        .unwrap();
    assert_eq!([op(fill), op(gemm), op(drain)], [1, 2, 3]);
}

#[test]
fn isa_mg_003_post_is_opcode_4() {
    let i = isa();
    let w = ClusterInst::Post(Post {
        dst: Coord(2, 1),
        n: 512,
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!(op(w), 4);
    assert_eq!((w.get(202, 16), w.get(107, 4), w.get(103, 4)), (512, 2, 1));
}

#[test]
fn isa_mg_004_silicon_field_positions() {
    let i = isa();
    for f in [
        &i.cluster.fill,
        &i.cluster.gemm,
        &i.cluster.drain,
        &i.cluster.post,
    ] {
        at(f, "addr", 251, 218);
        at(f, "n", 217, 202);
        at(f, "sel", 201, 201);
        at(f, "acc", 200, 200);
        at(f, "gm", 199, 192);
        at(f, "gn", 191, 184);
        at(f, "nk", 183, 176);
        at(f, "anchor", 175, 168);
        at(f, "emit", 116, 116);
        at(f, "fuse", 115, 115);
        at(f, "abank", 114, 114);
        at(f, "bbank", 113, 113);
        at(f, "fbank", 112, 112);
        at(f, "addr_hi", 68, 63);
    }
}

#[test]
fn isa_mg_005_remaining_field_positions_and_zero_tail() {
    let i = isa();
    let f = &i.cluster.drain;
    at(f, "peers", 167, 144);
    at(f, "npeer", 143, 142);
    at(f, "recv", 141, 141);
    at(f, "eoff", 140, 133);
    at(f, "dnode", 111, 111);
    at(f, "dst_x", 110, 107);
    at(f, "dst_y", 106, 103);
    at(f, "dbuf", 102, 95);
    at(f, "dflags", 94, 87);
    at(f, "dack_y", 86, 83);
    at(f, "dack_x", 82, 79);
    at(f, "dmesh", 78, 77);
    at(f, "dfin", 76, 69);
    at(f, "credit", 62, 62);
    let d = Drain {
        addr: (1 << 40) - 1,
        n: 0xFFFF,
        fuse: true,
        dst: Some(Coord(15, 15)),
        dflags: 255,
        ack: Coord(15, 15),
        buf: 255,
        mesh: 3,
        fin: Some(Coord(15, 15)),
        credit: true,
    };
    let w = ClusterInst::Drain(d).encode(&i.cluster).unwrap();
    assert!(w.is_zero_in(0, 62));
}

#[test]
fn isa_mg_006_defaults() {
    let i = isa();
    let w = ClusterInst::Drain(Drain::default())
        .encode(&i.cluster)
        .unwrap();
    let want = W256::ZERO
        .with(252, 4, 3)
        .with(176, 8, 1)
        .with(168, 8, ANCHOR as u64);
    assert_eq!(w, want);
}

#[test]
fn isa_mg_007_address_split() {
    let i = isa();
    let a = 0xAB_CDEF_0123u64;
    let w = ClusterInst::Fill(Fill {
        addr: a,
        n: 1,
        ..Fill::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!(w.get(218, 34) | w.get(63, 6) << 34, a);
}

#[test]
fn isa_mg_008_coordinate_byte() {
    assert_eq!(isa().cluster.coord_byte(Coord(3, 2)), 0x23);
}

#[test]
fn isa_mg_010_fill_fields() {
    let i = isa();
    let f = Fill {
        addr: 0x1000,
        n: 76,
        sel: 1,
        eoff: 128,
        fbank: 1,
        ..Fill::default()
    };
    let w = ClusterInst::Fill(f).encode(&i.cluster).unwrap();
    assert_eq!(
        (w.get(202, 16), w.get(201, 1), w.get(133, 8), w.get(112, 1)),
        (76, 1, 128, 1)
    );
}

#[test]
fn isa_mg_013_recv_fill() {
    let i = isa();
    let w = ClusterInst::Fill(Fill {
        n: 32,
        recv: true,
        ..Fill::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!((w.get(141, 1), w.get(218, 34)), (1, 0));
    assert!(i.cluster.addresses(w)[0].value == 0);
}

#[test]
fn isa_mg_014_shared_fill_peers() {
    let i = isa();
    let peers = vec![Coord(1, 0), Coord(1, 2), Coord(2, 1)];
    let w = ClusterInst::Fill(Fill {
        n: 8,
        peers: peers.clone(),
        ..Fill::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!((w.get(144, 24), w.get(142, 2)), (0x12_21_01, 3));
    let four = Fill {
        n: 8,
        peers: [peers, vec![Coord(0, 0)]].concat(),
        ..Fill::default()
    };
    refused(ClusterInst::Fill(four).encode(&i.cluster), "ISA.MG.014");
}

#[test]
fn isa_mg_018_fill_count_limit() {
    let i = isa();
    let max = machine().mg.max_fill;
    assert!(ClusterInst::Fill(Fill {
        n: max,
        ..Fill::default()
    })
    .encode(&i.cluster)
    .is_ok());
    refused(
        ClusterInst::Fill(Fill {
            n: max + 1,
            ..Fill::default()
        })
        .encode(&i.cluster),
        "ISA.MG.018",
    );
}

#[test]
fn isa_mg_022_nk_one_or_whole_issues() {
    let i = isa();
    for nk in [1, 2, 4, 64] {
        assert!(i.cluster.legal_nk(nk));
    }
    for nk in [3, 5, 7] {
        assert!(!i.cluster.legal_nk(nk));
    }
}

#[test]
fn isa_mg_099_odd_nk_refused() {
    let i = isa();
    refused(
        ClusterInst::Gemm(Gemm {
            gm: 1,
            gn: 1,
            nk: 3,
            ..Gemm::default()
        })
        .encode(&i.cluster),
        "ISA.MG.099",
    );
}

#[test]
fn isa_mg_023_anchor_40() {
    let i = isa();
    let w = ClusterInst::Gemm(Gemm {
        gm: 2,
        gn: 2,
        ..Gemm::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!(w.get(168, 8), 40);
}

#[test]
fn isa_mg_026_gemm_carries_an_address_only_when_it_emits() {
    let i = isa();
    let g = Gemm {
        gm: 1,
        gn: 1,
        addr: 0x4000,
        ..Gemm::default()
    };
    let w = ClusterInst::Gemm(g.clone()).encode(&i.cluster).unwrap();
    assert_eq!((w.get(218, 34), w.get(116, 1)), (0, 0));
    assert!(i.cluster.addresses(w).is_empty());
    let w = ClusterInst::Gemm(Gemm { emit: true, ..g })
        .encode(&i.cluster)
        .unwrap();
    assert_eq!((w.get(218, 34), w.get(116, 1)), (0x4000, 1));
    assert_eq!(i.cluster.addresses(w)[0].value, 0x4000);
}

#[test]
fn isa_mg_030_fused_drain() {
    let i = isa();
    let w = ClusterInst::Drain(Drain {
        addr: 0x2000,
        n: 240,
        fuse: true,
        ..Drain::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!(
        (w.get(115, 1), w.get(202, 16), w.get(218, 34)),
        (1, 240, 0x2000)
    );
}

#[test]
fn isa_mg_033_drain_to_a_unit() {
    let i = isa();
    let d = Drain {
        addr: 0x40,
        n: 4,
        dst: Some(Coord(1, 3)),
        buf: 2,
        dflags: 3,
        ack: Coord(4, 5),
        credit: true,
        ..Drain::default()
    };
    let w = ClusterInst::Drain(d).encode(&i.cluster).unwrap();
    assert_eq!(
        [
            w.get(111, 1),
            w.get(107, 4),
            w.get(103, 4),
            w.get(95, 8),
            w.get(87, 8),
            w.get(79, 4),
            w.get(83, 4),
            w.get(62, 1)
        ],
        [1, 1, 3, 2, 3, 4, 5, 1]
    );
    assert!(i.cluster.addresses(w).is_empty());
}

#[test]
fn isa_mg_034_drain_to_another_mesh() {
    let i = isa();
    let d = Drain {
        n: 1,
        dst: Some(Coord(0, 1)),
        mesh: 3,
        fin: Some(Coord(2, 2)),
        ..Drain::default()
    };
    let w = ClusterInst::Drain(d).encode(&i.cluster).unwrap();
    assert_eq!((w.get(77, 2), w.get(69, 8)), (3, 0x22));
}

#[test]
fn isa_mg_094_entry_offsets() {
    let i = isa();
    at(&i.cluster.gemm, "aoff", 132, 125);
    at(&i.cluster.gemm, "boff", 124, 117);
    let w = ClusterInst::Gemm(Gemm {
        gm: 1,
        gn: 1,
        aoff: 16,
        boff: 200,
        ..Gemm::default()
    })
    .encode(&i.cluster)
    .unwrap();
    assert_eq!((w.get(125, 8), w.get(117, 8)), (16, 200));
}

#[test]
fn isa_mg_097_wide_address_refused() {
    let i = isa();
    refused(
        ClusterInst::Fill(Fill {
            addr: 1 << 40,
            n: 1,
            ..Fill::default()
        })
        .encode(&i.cluster),
        "ISA.MG.097",
    );
    refused(
        ClusterInst::Drain(Drain {
            addr: 1 << 41,
            ..Drain::default()
        })
        .encode(&i.cluster),
        "ISA.MG.097",
    );
}
