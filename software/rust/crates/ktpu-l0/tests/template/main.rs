//! Host-side metadata boundaries and reusable bindings, without a compiler.

use std::collections::BTreeMap;

use ktpu_l0::format::template::CountPath;
use ktpu_l0::format::template::{Bounds, ParameterTable, Params, Template, ValuePatch};
use ktpu_l0::format::{Op, Step, UNBOUND};
use ktpu_l0::{Buffer, Kind, Package, Reloc, Word, WordOps};

fn template() -> Template {
    Template {
        package: Package {
            payloads: vec![Word::from_u64(u64::MAX)],
            buffers: vec![Buffer {
                name: "a".into(),
                size: 256,
                default: 4096,
                kind: Kind::Temp,
                mesh: 0,
            }],
            ..Package::default()
        },
        table: ParameterTable {
            environment: None,
            buffer_sizes: None,
            parameters: BTreeMap::from([("n".into(), Bounds { lo: 0, hi: 256 })]),
            sites: vec![ValuePatch {
                name: "count".into(),
                payload: 0,
                step: None,
                bit: 60,
                width: 8,
                shift: 0,
                constant: 0,
                coefficients: BTreeMap::from([("n".into(), 1)]),
                buffer: None,
            }],
        },
    }
}

#[test]
fn template_repeat_checks_final_count_and_parameter_address() {
    for from_site in [false, true] {
        let mut increment = Word::ZERO;
        increment.set(40, 8, 34);
        increment.set(64, 64, 4);
        let mut t = template();
        t.package.payloads = vec![Word::ZERO, increment];
        t.package.buffers[0].size = 4;
        t.package.steps = vec![Step::new(Op::Repeat, 0, 1, (1 << 48) | (1 << 32), 0)];
        let mut site = t.table.sites[0].clone();
        site.step = Some(0);
        site.bit = 96;
        site.width = 16;
        site.constant = 2;
        site.coefficients.clear();
        t.table.parameters.clear();
        t.table.sites = vec![site.clone()];
        if from_site {
            site.name = "address".into();
            site.step = None;
            site.bit = 0;
            site.width = 34;
            site.constant = 0;
            site.buffer = Some(0);
            t.table.sites.push(site);
        } else {
            t.package.relocs.push(Reloc {
                payload: 0,
                bit: 0,
                width: 34,
                shift: 0,
                buffer: 0,
                addend: 0,
            });
        }
        assert_eq!(t.bind(&[], &Params::new()).unwrap_err().clause, "RUL.039");
        #[cfg(feature = "mutants")]
        ktpu_l0::mutant::with("l0.template/late-repeat-check", || {
            assert!(t.bind(&[], &Params::new()).is_ok())
        });
        t.package.buffers[0].size = 8;
        assert!(t.bind(&[], &Params::new()).is_ok());
        assert_eq!(
            t.bind(&[(1 << 34) - 4], &Params::new()).unwrap_err().clause,
            "L0.STEP.041"
        );
        #[cfg(feature = "mutants")]
        ktpu_l0::mutant::with("l0.template/late-repeat-check", || {
            assert!(t.bind(&[(1 << 34) - 4], &Params::new()).is_ok())
        });
        t.package.steps[0].arg = (1 << 48) | (2 << 32);
        t.table.sites[0].constant = 1;
        assert!(t.bind(&[(1 << 34) - 4], &Params::new()).is_ok());
    }
}

#[test]
fn relocated_repeat_checks_final_address_and_split_field_carry() {
    let mut increment = Word::ZERO;
    increment.set(40, 8, 32);
    increment.set(64, 64, 32);
    let mut p = Package {
        steps: vec![Step::new(Op::Repeat, 0, 1, (2u64 << 32) | (1u64 << 48), 0)],
        payloads: vec![Word::ZERO, increment],
        buffers: vec![Buffer {
            name: "scratch".into(),
            size: 64,
            default: 0,
            kind: Kind::Temp,
            mesh: 0,
        }],
        relocs: vec![
            Reloc {
                payload: 0,
                bit: 0,
                width: 32,
                shift: 0,
                buffer: 0,
                addend: 0,
            },
            Reloc {
                payload: 0,
                bit: 32,
                width: 16,
                shift: 32,
                buffer: 0,
                addend: 0,
            },
        ],
        ..Package::default()
    };
    assert!(p.bind(&[0x1_0000_0000]).is_ok());
    assert_eq!(p.bind(&[0xffff_fff0]).unwrap_err().clause, "L0.STEP.041");
    p.buffers[0].size = 32;
    assert_eq!(p.bind(&[0]).unwrap_err().clause, "RUL.039");
    #[cfg(feature = "mutants")]
    ktpu_l0::mutant::with("l0.bind/repeat-wrap", || {
        assert!(p.bind(&[0xffff_fff0]).is_ok());
        assert!(p.bind(&[0]).is_ok());
    });
    p.steps = vec![Step::new(Op::Patch, 0, 0, (32 << 8) | (1 << 26), 0)];
    p.buffers[0].size = 64;
    assert!(p.bind(&[0x1_0000_0000]).is_ok());
    assert_eq!(p.bind(&[0xffff_fff0]).unwrap_err().clause, "L0.STEP.054");
    #[cfg(feature = "mutants")]
    ktpu_l0::mutant::with("l0.bind/unchecked", || {
        assert!(p.bind(&[0xffff_fff0]).is_ok())
    });
}

fn params(n: i64) -> Params {
    BTreeMap::from([("n".into(), n)])
}

#[test]
fn repeat_bind_boundaries_and_no_source_mutation() {
    let t = template();
    let saved = t.clone();
    for n in [0, 1, 254, 255] {
        let out = t.bind(&[], &params(n)).unwrap();
        assert_eq!(out.payloads[0].get(60, 8), n as u64);
        assert_eq!(out.payloads[0].get(0, 60), (1 << 60) - 1);
        assert!(out.buffers.is_empty());
    }
    let e = t.bind(&[], &params(256)).unwrap_err();
    assert_eq!(e.clause, "L0.BIND.037");
    assert!(e.msg.contains("count[0]") && e.msg.contains("256"));
    assert_eq!(t, saved);
}

#[test]
fn class_lower_upper_missing_unknown_and_inverted_refuse() {
    let mut t = template();
    for p in [
        params(-1),
        params(257),
        Params::new(),
        BTreeMap::from([("x".into(), 1)]),
    ] {
        assert_eq!(t.bind(&[], &p).unwrap_err().clause, "L0.BIND.038");
    }
    t.table.parameters.get_mut("n").unwrap().lo = 257;
    assert_eq!(t.bind(&[], &params(256)).unwrap_err().clause, "L0.BIND.038");
    t = template();
    t.table.sites[0].coefficients.insert("other".into(), 2);
    assert_eq!(t.bind(&[], &params(2)).unwrap_err().clause, "L0.BIND.038");
}

#[test]
fn address_bounds_zero_default_sentinel_and_overflow() {
    let mut t = template();
    t.table.sites[0].buffer = Some(0);
    t.table.sites[0].width = 64;
    t.table.sites[0].bit = 0;
    for (bindings, base) in [
        (vec![], 4096),
        (vec![UNBOUND], 4096),
        (vec![0], 0),
        (vec![8192], 8192),
    ] {
        for n in [0, 255] {
            assert_eq!(
                t.bind(&bindings, &params(n)).unwrap().payloads[0].get(0, 64),
                base + n as u64
            );
        }
    }
    assert_eq!(t.bind(&[], &params(256)).unwrap_err().clause, "L0.BIND.039");
    t.table.sites[0].constant = -1;
    assert_eq!(t.bind(&[], &params(0)).unwrap_err().clause, "L0.BIND.039");
    t.table.sites[0].constant = 0;
    assert_eq!(
        t.bind(&[u64::MAX - 1], &params(2)).unwrap_err().clause,
        "L0.BIND.037"
    );
    t.table.sites[0].buffer = Some(1);
    assert_eq!(t.bind(&[], &params(0)).unwrap_err().clause, "L0.BIND.039");
}

#[test]
fn negative_values_and_checked_affine_overflow_refuse() {
    let mut t = template();
    t.table.sites[0].constant = -1;
    assert_eq!(t.bind(&[], &params(0)).unwrap_err().clause, "L0.BIND.037");
    t.table.parameters.clear();
    t.table.sites[0].coefficients.clear();
    let mut p = Params::new();
    for name in ["a", "b", "c"] {
        t.table.parameters.insert(
            name.into(),
            Bounds {
                lo: i64::MIN,
                hi: i64::MAX,
            },
        );
        t.table.sites[0].coefficients.insert(name.into(), i64::MIN);
        p.insert(name.into(), i64::MIN);
    }
    let e = t.bind(&[], &p).unwrap_err();
    assert_eq!(e.clause, "L0.BIND.037");
    assert!(e.msg.contains("arithmetic overflow"));
}

#[test]
fn malformed_sites_and_overlapping_writes_refuse_without_panicking() {
    let t = template();
    let mut sites = Vec::new();
    for (bit, width, shift, payload) in [
        (u32::MAX, 8, 0, 0),
        (0, 0, 0, 0),
        (0, 65, 0, 0),
        (0, 8, 64, 0),
        (0, 64, 1, 0),
        (0, 8, 0, usize::MAX),
    ] {
        let mut s = t.table.sites[0].clone();
        s.bit = bit;
        s.width = width;
        s.shift = shift;
        s.payload = payload;
        sites.push(s);
    }
    for s in sites {
        let mut bad = t.clone();
        bad.table.sites[0] = s;
        assert_eq!(bad.bind(&[], &params(1)).unwrap_err().clause, "L0.BIND.037");
    }
    let mut bad = t.clone();
    bad.table.sites.push(bad.table.sites[0].clone());
    assert_eq!(bad.bind(&[], &params(1)).unwrap_err().clause, "L0.BIND.037");
    let mut bad = t;
    bad.package.relocs.push(Reloc {
        payload: 0,
        bit: 60,
        width: 4,
        shift: 0,
        buffer: 0,
        addend: 0,
    });
    assert_eq!(bad.bind(&[], &params(1)).unwrap_err().clause, "L0.BIND.037");
}

#[test]
fn split_address_top_segment_checks_width_and_preserves_neighbors() {
    let mut t = template();
    t.table.sites[0].buffer = Some(0);
    let mut high = t.table.sites[0].clone();
    high.name = "high".into();
    high.bit = 200;
    high.shift = 8;
    t.table.sites.push(high);
    let out = t.bind(&[0x1200], &params(0x34)).unwrap();
    assert_eq!(out.payloads[0].get(60, 8), 0x34);
    assert_eq!(out.payloads[0].get(200, 8), 0x12);
    assert_eq!(
        t.bind(&[0x10000], &params(0)).unwrap_err().clause,
        "L0.BIND.037"
    );
}

fn step_template(op: Op) -> Template {
    let mut t = template();
    t.package.payloads = vec![Word::ZERO; 256];
    t.package.steps = vec![Step::new(op, 0, 1, 0, 0), Step::bare(Op::End)];
    t.table.sites[0].step = Some(0);
    t.table.sites[0].bit = 32;
    t.table.sites[0].width = 32;
    if op == Op::Repeat {
        t.table.sites[0].bit = 96;
        t.table.sites[0].width = 16;
        t.package.steps[0].arg = 1 << 32;
    }
    t
}

#[test]
fn step_fields_preserve_topology_and_namespaces() {
    for op in [Op::Dispatch, Op::Fetch, Op::Await, Op::Mwait, Op::Repeat] {
        let mut t = step_template(op);
        let mut payload = t.table.sites[0].clone();
        payload.step = None;
        t.table.sites.push(payload);
        let before = t.clone();
        let bound = t.bind(&[], &params(9)).unwrap();
        assert_eq!(t, before);
        assert_eq!(bound.steps[0].op, op);
        assert_eq!(bound.steps[1], Step::bare(Op::End));
        assert_eq!(
            bound.payloads[0].get(t.table.sites[0].bit, t.table.sites[0].width),
            9
        );
        if op == Op::Repeat {
            assert_eq!(bound.steps[0].arg, 9 << 32);
            assert_eq!(bound.steps[0].count, 1);
        } else {
            assert_eq!(bound.steps[0].count, 9);
            assert_eq!(bound.steps[0].arg, 0);
        }
    }
}

#[test]
fn step_refuses_topology_overlap_and_invalid_counts() {
    let t = step_template(Op::Dispatch);
    for (bit, width, index, buffer, payload, shift) in [
        (0, 8, 0, None, 0, 0),
        (32, 16, 0, None, 0, 0),
        (32, 32, usize::MAX, None, 0, 0),
        (32, 32, 0, Some(0), 0, 0),
        (32, 32, 0, None, 1, 0),
        (32, 32, 0, None, 0, 1),
    ] {
        let mut bad = t.clone();
        let site = &mut bad.table.sites[0];
        site.bit = bit;
        site.width = width;
        site.step = Some(index);
        site.buffer = buffer;
        site.payload = payload;
        site.shift = shift;
        assert_eq!(bad.bind(&[], &params(2)).unwrap_err().clause, "L0.BIND.041");
    }
    let mut bad = t.clone();
    bad.table.sites.push(bad.table.sites[0].clone());
    assert_eq!(bad.bind(&[], &params(2)).unwrap_err().clause, "L0.BIND.037");
    for first in [255, u64::MAX] {
        let mut bad = t.clone();
        bad.package.steps[0].arg = first;
        assert_eq!(bad.bind(&[], &params(2)).unwrap_err().clause, "L0.BIND.042");
    }
    let t = step_template(Op::Repeat);
    assert_eq!(t.bind(&[], &params(0)).unwrap_err().clause, "L0.BIND.042");
}

#[test]
fn register_path_cost_target_and_refusals() {
    let t = step_template(Op::Dispatch);
    let direct = t.bind(&[], &params(9)).unwrap();
    let out = t
        .bind_with_path(&[], &params(9), CountPath::Register)
        .unwrap();
    assert_eq!(out.steps.len(), direct.steps.len() + 2);
    assert_eq!(out.payloads, direct.payloads);
    assert_eq!(out.steps[0], Step::new(Op::Alu, 15, 0, 9, 2));
    assert_eq!(out.steps[1], Step::new(Op::Patch, 15, 2, 0x1002020, 0));
    assert_eq!(out.steps[2].count, 0);
    for op in [Op::Load, Op::Alu, Op::Jump, Op::Patch] {
        let mut bad = t.clone();
        bad.package.steps.push(Step::bare(op));
        assert_eq!(
            bad.bind_with_path(&[], &params(9), CountPath::Register)
                .unwrap_err()
                .clause,
            "L0.BIND.043"
        );
    }
    #[cfg(feature = "mutants")]
    {
        assert_ne!(
            ktpu_l0::mutant::with("l0.template/step", || t.bind(&[], &params(9)).unwrap()),
            direct
        );
        assert_ne!(
            ktpu_l0::mutant::with("l0.template/register_target", || t
                .bind_with_path(&[], &params(9), CountPath::Register)
                .unwrap()),
            out
        );
        let mut bad = t;
        bad.package.steps[0].arg = 255;
        assert!(bad.bind(&[], &params(9)).is_err());
        assert!(
            ktpu_l0::mutant::with("l0.template/step_extent", || bad.bind(&[], &params(9))).is_ok()
        );
    }
}

#[test]
fn metadata_round_trip_and_constant_sites_without_parameters() {
    let mut t = template();
    let json = serde_json::to_string(&t.table).unwrap();
    assert_eq!(
        serde_json::from_str::<ParameterTable>(&json).unwrap(),
        t.table
    );
    t.table.parameters.clear();
    t.table.sites[0].coefficients.clear();
    t.table.sites[0].constant = 19;
    assert_eq!(
        t.bind(&[], &Params::new()).unwrap().payloads[0].get(60, 8),
        19
    );
}
