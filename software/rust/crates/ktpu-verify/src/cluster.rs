//! MG instruction consistency against the reference ISA and machine topology.

use ktpu_isa::layout::mask;
use ktpu_isa::unit::cluster::{ADDR_LO_BITS, BUF_ACC, BUF_L1A, BUF_L1B, DFLAG_COUNT, DFLAG_SIGNAL};
use ktpu_isa::W256;

use crate::adapter::isa::Fields;
use crate::{Target, UnitKind};

/// Check fields before the abstract sequencer consumes an instruction. Counts,
/// peer arrivals and ordering are then checked by that sequencer's counters.
pub(crate) fn word(t: &Target, source: (u32, u8, u8), raw: W256) -> Result<(), String> {
    let mesh = source.0;
    let isa = &t.isa.cluster;
    let (name, decoded) = isa.set.decode(raw).map_err(|e| e.to_string())?;
    let fmt = isa.set.by_name(name).expect("decoded format");
    let f = Fields::from(decoded);
    let g = |n| f.g(n);
    let sink = [
        "addr", "addr_hi", "dnode", "dst_x", "dst_y", "dbuf", "dflags", "dack_x", "dack_y",
        "dmesh", "dfin", "credit",
    ];
    let used: &[&str] = match name {
        "FILL" => &[
            "addr", "addr_hi", "n", "sel", "eoff", "fbank", "recv", "npeer", "peers",
        ],
        "GEMM" => &[
            "gm", "gn", "nk", "anchor", "acc", "aoff", "boff", "abank", "bbank", "emit",
        ],
        "DRAIN" => &["n", "fuse", "anchor"],
        "POST" => &["dst_x", "dst_y", "n"],
        _ => return Err(format!("unknown cluster opcode {name}")),
    };
    for field in &fmt.fields {
        let is_sink =
            (name == "DRAIN" || (name == "GEMM" && g("emit") != 0)) && sink.contains(&field.name);
        if field.konst.is_none()
            && !used.contains(&field.name)
            && !is_sink
            && g(field.name) != field.default.unwrap_or(0)
        {
            return Err(format!(
                "{name}: unused {}={} is not its ISA default",
                field.name,
                g(field.name)
            ));
        }
    }
    let padding = fmt.width - fmt.fields.iter().map(|f| f.width).sum::<u32>();
    if !raw.is_zero_in(0, padding) {
        return Err(format!("{name}: nonzero reserved padding"));
    }
    let bank = t.facts.mg_bank_entries;
    match name {
        "FILL" => {
            if g("n") > isa.cfg.max_fill as i64 || g("eoff") + g("n") > bank {
                return Err("FILL count/offset exceeds the configured bank or max_fill".into());
            }
            let entries = if g("sel") == 0 {
                t.facts.mg_l1_a_entries
            } else {
                t.facts.mg_l1_b_entries
            };
            if (g("fbank") + 1) * bank > entries {
                return Err("FILL bank exceeds side L1".into());
            }
            if g("recv") != 0 && (g("addr") != 0 || g("addr_hi") != 0 || g("npeer") != 0) {
                return Err("receive FILL carries a memory address or shared peers".into());
            }
            if g("npeer") > isa.cfg.max_peers as i64 {
                return Err("FILL peer count exceeds max_peers".into());
            }
            let width = isa.cfg.coord_bits();
            let peers = g("peers") as u64;
            if peers >> (g("npeer") as u32 * width) != 0 {
                return Err("FILL carries peers past npeer".into());
            }
            let mut seen = Vec::new();
            for i in 0..g("npeer") as u32 {
                let c = coord((peers >> (i * width)) & mask(width), isa.cfg.pos_bits);
                if t.mesh(mesh).is_none_or(|m| !m.mg.contains(&c)) || seen.contains(&c) {
                    return Err(format!("FILL peer {c:?} is absent or repeated"));
                }
                seen.push(c);
            }
        }
        "GEMM" => {
            if g("gm") == 0
                || g("gn") == 0
                || g("nk") == 0
                || !isa.legal_nk(g("nk") as u32)
                || g("gm") * g("gn") > t.facts.mg_tiles
            {
                return Err(
                    "GEMM shape exceeds the accumulator or has an illegal K issue count".into(),
                );
            }
            for (side, count, off, b, entries) in [
                ("A", g("gm"), g("aoff"), g("abank"), t.facts.mg_l1_a_entries),
                ("B", g("gn"), g("boff"), g("bbank"), t.facts.mg_l1_b_entries),
            ] {
                if count * g("nk") > bank || off >= bank || (b + 1) * bank > entries {
                    return Err(format!("GEMM {side} extent/bank exceeds local memory"));
                }
            }
        }
        "DRAIN" if g("n") > t.facts.mg_tiles => return Err("DRAIN exceeds the accumulator".into()),
        "POST" => {
            let c = (g("dst_x") as u8, g("dst_y") as u8);
            if t.mesh(mesh).and_then(|m| m.kind_at(c)).is_none() {
                return Err(format!("POST destination {c:?} is absent"));
            }
        }
        _ => {}
    }
    if name == "DRAIN" || (name == "GEMM" && g("emit") != 0) {
        if g("dflags") & !i64::from(DFLAG_SIGNAL | DFLAG_COUNT) != 0 {
            return Err("unknown drain flags".into());
        }
        if g("dnode") == 0 {
            if sink[2..].iter().any(|n| g(n) != 0) {
                return Err("memory drain carries unit destination/flags/credit".into());
            }
        } else {
            let local = (g("dst_x") as u8, g("dst_y") as u8);
            if (g("dfin") as u64) >> isa.cfg.coord_bits() != 0 {
                return Err("drain final coordinate exceeds the ISA coordinate width".into());
            }
            let (dst_mesh, c) = if g("dfin") != 0 {
                if t.mesh(mesh).is_none_or(|m| !m.mem_ports.contains(&local)) {
                    return Err("remote drain does not address a local memory port".into());
                }
                (g("dmesh") as u32, coord(g("dfin") as u64, isa.cfg.pos_bits))
            } else {
                if g("dmesh") != 0 {
                    return Err("drain mesh without a final coordinate".into());
                }
                (mesh, local)
            };
            let kind = t.mesh(dst_mesh).and_then(|m| m.kind_at(c));
            if kind == Some(UnitKind::Mg) && (dst_mesh, c.0, c.1) == source {
                return Err("self-addressed cluster drain deadlocks the receive port".into());
            }
            // CU_DATA's zero-coordinate sentinel addresses the node processor.
            let node = t
                .mesh(dst_mesh)
                .is_some_and(|m| c == (0, 0) || m.mem_ports.contains(&c));
            if kind.is_none() && !node {
                return Err(format!("drain destination {dst_mesh}:{c:?} is absent"));
            }
            let facts = &t.facts;
            let limit = match kind {
                Some(UnitKind::Mg) => Some(match g("dbuf") {
                    b if b == i64::from(BUF_L1A) => facts.mg_l1_a_entries * facts.mg_entry_granules,
                    b if b == i64::from(BUF_L1B) => facts.mg_l1_b_entries * facts.mg_entry_granules,
                    b if b == i64::from(BUF_ACC) => facts.mg_tiles * facts.mg_subtile_granules,
                    _ => return Err("drain names no cluster buffer".into()),
                }),
                Some(UnitKind::Vc) => Some(facts.vc_l1_words),
                _ => None,
            };
            if let Some(limit) = limit {
                let addr = g("addr") as u64 | ((g("addr_hi") as u64) << ADDR_LO_BITS);
                let off = addr / facts.word_bytes as u64;
                let mut extent = if name == "GEMM" {
                    g("gm") * g("gn")
                } else {
                    g("n")
                };
                let peer = kind == Some(UnitKind::Mg) && g("dbuf") == i64::from(BUF_ACC);
                if peer {
                    if name == "GEMM" || g("fuse") != 0 {
                        return Err(
                            "accumulator peer transfer requires a plain DRAIN, not FP16 emission"
                                .into(),
                        );
                    }
                    extent *= facts.mg_subtile_granules;
                }
                if !addr.is_multiple_of(facts.word_bytes as u64)
                    || off + extent as u64 > limit as u64
                    || (peer && !off.is_multiple_of(facts.mg_subtile_granules as u64))
                {
                    return Err(
                        "drain offset/extent exceeds or misaligns the destination buffer".into(),
                    );
                }
            }
            if g("dflags") & i64::from(DFLAG_SIGNAL) != 0 {
                let ack = (g("dack_x") as u8, g("dack_y") as u8);
                if ack != (0, 0)
                    && t.mesh(mesh)
                        .is_none_or(|m| m.kind_at(ack).is_none() && !m.mem_ports.contains(&ack))
                {
                    return Err("drain acknowledgement destination is absent".into());
                }
            }
        }
    }
    Ok(())
}

fn coord(v: u64, p: u32) -> (u8, u8) {
    ((v & mask(p)) as u8, ((v >> p) & mask(p)) as u8)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ktpu_machine::Machine;

    #[test]
    fn limits_follow_machine_facts_and_node_sentinel_is_valid() {
        let m = Machine::named("multimesh_v9t6").unwrap();
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let xy = t.meshes[0].mg[0];
        let source = (0, xy.0, xy.1);
        let fill = t.isa.cluster.fill.encode(&[("n", 17)]).unwrap();
        assert!(word(&t, source, fill).is_ok());
        for fact in ["mg.max_fill", "mg.l1_bank_entries"] {
            let small = m.with_fact(fact, 16.into()).unwrap();
            let small = Target::from_machine(&small, Some(1)).unwrap();
            assert!(word(&small, source, fill).is_err());
        }
        let node = t
            .isa
            .cluster
            .drain
            .encode(&[("n", 1), ("dnode", 1)])
            .unwrap();
        assert!(word(&t, source, node).is_ok());
        let drain = t.isa.cluster.drain.encode(&[("n", 5)]).unwrap();
        assert!(word(&t, source, drain).is_ok());
        let small = m.with_fact("mg.tiles", 4.into()).unwrap();
        assert!(word(
            &Target::from_machine(&small, Some(1)).unwrap(),
            source,
            drain
        )
        .is_err());
        let own = t
            .isa
            .cluster
            .drain
            .encode(&[
                ("n", 1),
                ("dnode", 1),
                ("dst_x", i64::from(xy.0)),
                ("dst_y", i64::from(xy.1)),
                ("dbuf", i64::from(BUF_ACC)),
            ])
            .unwrap();
        assert!(word(&t, source, own).is_err());
        let other = t.meshes[0].mg[1];
        let emission = t
            .isa
            .cluster
            .gemm
            .encode(&[
                ("gm", 1),
                ("gn", 1),
                ("emit", 1),
                ("dnode", 1),
                ("dst_x", i64::from(other.0)),
                ("dst_y", i64::from(other.1)),
                ("dbuf", i64::from(BUF_ACC)),
            ])
            .unwrap();
        assert!(word(&t, source, emission)
            .unwrap_err()
            .contains("plain DRAIN"));
        let vc = t.meshes[0].vc[0];
        for addr in [
            1,
            t.facts.vc_l1_words * t.facts.word_bytes,
            (1i64 << ADDR_LO_BITS) + t.facts.word_bytes,
        ] {
            let mut args = vec![
                ("n", 1),
                ("dnode", 1),
                ("dst_x", i64::from(vc.0)),
                ("dst_y", i64::from(vc.1)),
            ];
            args.extend(t.isa.cluster.split_addr(addr as u64).unwrap());
            assert!(word(&t, source, t.isa.cluster.drain.encode(&args).unwrap()).is_err());
        }
    }
}
