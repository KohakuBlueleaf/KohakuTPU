use ka_package::engine::lower;
use ka_package::format::template::{ParameterTable, Params, Template, ValuePatch};
use ka_package::format::{Op, Package, Step, Unit};
use ka_package::word::{Word, WordOps};
use ka_package::Facts;

fn repeat_source(fetch: bool) -> Package {
    Package {
        units: vec![Unit {
            type_code: 0x5643,
            x: 1,
            y: 1,
            mesh: 0,
            credit: 16,
            fetch: if fetch { 1 << 16 | 2 } else { 0 },
        }],
        steps: vec![Step::new(Op::Repeat, 0, 1, 2 << 32 | 1 << 48, 0)],
        payloads: vec![Word([250, 0, 0, 0]), Word([8 << 40, 3, 0, 0])],
        ..Package::default()
    }
}

fn corrupt_arg0(low: &mut Package, mut skip: usize) {
    for step in low.steps.clone() {
        if step.op != Op::Engine {
            continue;
        }
        for k in 0..step.count {
            let word = &mut low.payloads[(step.arg + k / 3) as usize];
            let j = (k % 3) as u32;
            if word.get(8 * j, 8) == 1 {
                if skip != 0 {
                    skip -= 1;
                    continue;
                }
                let bit = 64 * (j + 1);
                word.set(bit, 64, word.get(bit, 64) ^ (1 << 60));
                return;
            }
        }
    }
    panic!("fixture has no ARG0");
}

#[test]
fn replay_checks_repeat_values() {
    for fetch in [false, true] {
        let mut src = repeat_source(fetch);
        src.payloads[0] = Word::ZERO;
        src.payloads[0].set(60, 12, 15);
        src.payloads[1] = Word([60 << 32 | 12 << 40, 3, 0, 0]);
        let mut low = lower(&src, &facts()).expect("valid repeat");
        assert!(ka_package::replay::check(&src, &low, &facts()).is_ok());
        if fetch {
            low.payloads[src.payloads.len() + 1].0[0] ^= 1 << 60;
        } else {
            corrupt_arg0(&mut low, 1);
        }
        let error = ka_package::replay::check(&src, &low, &facts()).expect_err("corrupt word");
        assert!(error.to_string().contains("L0.DE.040"));
    }
}

#[test]
fn replay_uses_dispatch_source() {
    for fetch in [false, true] {
        let mut src = repeat_source(fetch);
        src.steps = vec![Step::new(Op::Dispatch, 0, 1, 0, 0)];
        let mut low = lower(&src, &facts()).expect("valid dispatch");
        assert!(ka_package::replay::check(&src, &low, &facts()).is_ok());
        low.payloads[0].0[0] ^= 1 << 60;
        if !fetch {
            corrupt_arg0(&mut low, 0);
        }
        let error = ka_package::replay::check(&src, &low, &facts()).expect_err("corrupt word");
        assert!(error.to_string().contains("L0.DE.040"));
    }
}

#[test]
fn replay_refuses_absent_dispatch_words() {
    let mut src = repeat_source(true);
    src.steps = vec![Step::new(Op::Dispatch, 0, 1, 0, 0)];
    let mut low = lower(&src, &facts()).expect("valid dispatch");
    let absent = low.payloads.len() as u64 + 10;
    src.steps[0].arg = absent;
    for step in low.steps.clone() {
        if step.op != Op::Engine {
            continue;
        }
        for k in 0..step.count {
            let word = &mut low.payloads[(step.arg + k / 3) as usize];
            let j = (k % 3) as u32;
            if word.get(8 * j, 8) == (4 | 64) {
                let bit = 64 * (j + 1);
                let value = word.get(bit, 64) & 0xff_ffff;
                word.set(bit, 64, value | (absent * 32) << 24);
            }
        }
    }
    let error = ka_package::replay::check(&src, &low, &facts()).expect_err("absent word");
    assert!(error.to_string().contains("L0.DE.040"));
}

#[test]
fn builder_rejects_overlapping_fields() {
    let mut builder = ka_package::build::PackageBuilder::new(&facts(), Default::default(), 16);
    let unit = builder.unit((1, 1), Some("VC"), None).expect("valid unit");
    let word = Word([250, 0, 0, 0]);
    assert!(builder
        .repeat(unit, &[word], 2, &[(0, 0, 8, 3), (0, 0, 8, 3)], None)
        .is_err());
    assert!(builder
        .repeat(unit, &[word], 2, &[(0, 0, 8, 3)], None)
        .is_ok());
}

fn facts() -> Facts {
    Facts {
        package_magic: 0,
        package_version: 0,
        header_bytes: 0,
        regs: 0,
        unbounded: 1 << 32,
        engine_units: 16,
        counter_bits: 12,
        max_pkg_units: 16,
        max_buffers: 16,
        ring_depth: 16,
        ring_unheld: true,
        wait_window: true,
        mover_span: 0,
        inst_depth: 512,
        il_slots: 0,
        boot_version: 0,
        boot_max_units: 16,
        boot_regions: 0,
        addr_bits: 40,
        word_bytes: 32,
        mover_levels: 0,
        mover_max_index: 0,
        units: Default::default(),
        fetch_port: None,
        mesh: 0,
    }
}

fn repeat(repeats: u64, count: u64, duplicate: bool) -> Package {
    let head = 8u64 << 40;
    Package {
        steps: vec![Step::new(
            Op::Repeat,
            0,
            count,
            repeats << 32 | (if duplicate { 2 } else { 1 }) << 48,
            0,
        )],
        payloads: vec![
            Word([250, 0, 0, 0]),
            Word([head, 3, if duplicate { head } else { 0 }, 3]),
        ],
        ..Package::default()
    }
}

#[test]
fn overlapping_fields() {
    assert!(repeat(2, 1, true).bind(&[]).is_err());
    assert!(repeat(2, 1, false).bind(&[]).is_ok());
    let mut disjoint = repeat(2, 1, true);
    disjoint.payloads[1].0[0] |= 8 << 32;
    assert!(disjoint.bind(&[]).is_ok());
}

#[test]
fn non_address_repeat_wrap() {
    let template = Template {
        package: repeat(2, 1, false),
        table: ParameterTable {
            sites: vec![ValuePatch {
                name: "repetitions".into(),
                payload: 0,
                step: Some(0),
                bit: 96,
                width: 16,
                shift: 0,
                constant: 3,
                coefficients: Default::default(),
                buffer: None,
            }],
            ..ParameterTable::default()
        },
    };
    assert!(template.bind(&[], &Params::new()).is_err());
    assert!(repeat(3, 1, false).bind(&[]).is_err());
}

#[test]
fn repeat_extent() {
    assert!(repeat(2, 3, false).bind(&[]).is_err());
    let mut p = repeat(2, 3, false);
    p.steps[0].arg = 2 << 32;
    assert!(p.bind(&[]).is_err());
}
