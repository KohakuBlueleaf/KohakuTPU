//! Semantic admission before command emission.
use std::collections::BTreeMap;
use std::sync::mpsc;
use std::time::Duration;

use ktpu_isa::unit::mover::{MoverIsa, MoverOp, R_DIM};
use ktpu_isa::unit::sru::config::{self, Job, Pool};
use ktpu_isa::Machine;
use ktpu_l0::program::Stmt as NodeStmt;
use ktpu_l0::{mover, Facts};
use ktpu_l1::build::programs;
use ktpu_l1::text::read;

fn bounded(f: impl FnOnce() + Send + 'static) {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        f();
        tx.send(()).expect("receiver");
    });
    rx.recv_timeout(Duration::from_secs(2))
        .expect("admission must finish without emitting an unbounded stream");
}

fn convert(entries: i64, input: i64, output: i64) -> MoverOp {
    MoverOp::Convert {
        src: 0,
        dst: 1048576,
        entries,
        xform_id: 1,
        mode: 0,
        in_words: Some(input),
        out_words: Some(output),
    }
}

fn copy(nbytes: i64) -> MoverOp {
    MoverOp::Copy {
        src: 0,
        dst: 2097152,
        nbytes,
    }
}

fn read_words(writes: &[(u32, u64)]) -> u64 {
    writes
        .iter()
        .filter(|(r, v)| *r == R_DIM && v & 1 == 0)
        .map(|(_, v)| (v >> 4) & 65535)
        .sum()
}

#[test]
fn packed_moves_require_progress() {
    bounded(|| {
        let card = Machine::named("multimesh_v9t6").expect("machine");
        let mut isa = MoverIsa::new(&card);
        let facts = Facts::of_mesh(&card, 0).expect("facts");
        for (entries, input, output) in [
            (1, 0, 4),
            (1, 16385, 4),
            (-1, 8, 4),
            (1, 8, 0),
            (1, 8, i64::MAX),
        ] {
            assert!(isa.writes(&convert(entries, input, output)).is_err());
            let result = mover::convert(
                &facts,
                0,
                1048576,
                entries.into(),
                1,
                0,
                input.into(),
                output.into(),
            );
            assert!(result.is_err());
        }
        isa.move_words = 0;
        assert!(isa.writes(&copy(32)).is_err());
        assert!(isa.writes(&convert(1, 8, 4)).is_err());
        let quant = MoverOp::Quantise {
            src: 0,
            dst: 1048576,
            entries: 1,
            b: false,
        };
        assert!(isa.writes(&quant).is_err());
        isa.move_words = 16384;
        isa.f16_entry_words = 16385;
        assert!(isa.writes(&quant).is_err());
        isa.gt4_words = 0;
        let transpose = MoverOp::Transpose4 {
            src: 0,
            dst: 1048576,
            groups: 1,
        };
        assert!(isa.writes(&transpose).is_err());
        for (input, entries) in [(16384, 1), (8, 4097)] {
            let writes = isa
                .writes(&convert(entries, input, 4))
                .expect("whole entries");
            assert_eq!(read_words(&writes), (input * entries) as u64);
        }
    });
}

#[test]
fn copies_do_not_discard_partial_words() {
    let card = Machine::named("multimesh_v9t6").expect("machine");
    let isa = MoverIsa::new(&card);
    let facts = Facts::of_mesh(&card, 0).expect("facts");
    for bytes in [-1, 33] {
        assert!(isa.writes(&copy(bytes)).is_err());
        assert!(mover::copy(&facts, 0, 2097152, bytes.into()).is_err());
        let text =
            format!("target ktpu.v9\nfn copy_test.l1()\n    copy 0 -> 2097152 bytes={bytes}\n");
        let module = read(&text, "copy").expect("syntax");
        let result = programs(&module, "copy_test", &BTreeMap::new(), &card, Some(1), None);
        assert!(result.is_err());
    }
    for bytes in [0, 32, 1048608] {
        let writes = isa.writes(&copy(bytes)).expect("whole words");
        assert_eq!(read_words(&writes) * 32, bytes as u64);
    }
}

fn row_job() -> Job {
    serde_json::from_value(serde_json::json!({
        "g": {"NW": 8, "NSR": 1, "OUT": 1},
        "passes": [{"FINLEN": 1}],
        "fin": [{"OP": 3, "DST": 0, "A": 6, "C": 7}]
    }))
    .expect("job")
}

fn text_row(card: &Machine, body: &str) -> bool {
    let text = format!(
        "target ktpu.v9\nfn row_test.l1()\n    send sr[0]\n{body}            run a=0 out=4096\n"
    );
    let module = read(&text, "row").expect("syntax");
    programs(&module, "row_test", &BTreeMap::new(), card, Some(1), None).is_ok()
}

#[test]
fn finalise_requires_its_actual_hardware() {
    let card = Machine::named("sru_v9t7").expect("machine");
    for (field, value, ops) in [
        (
            "sr.profile.F_LANES",
            0,
            vec![
                (3, "add"),
                (4, "sub"),
                (5, "mul"),
                (6, "fma"),
                (10, "rcp"),
                (11, "rsqrt"),
            ],
        ),
        ("sr.profile.FIN_RCP", 0, vec![(10, "rcp"), (11, "rsqrt")]),
    ] {
        let limited = card.with_fact(field, value.into()).expect("variant");
        for (code, name) in ops {
            let mut job = row_job();
            job.fin[0].insert("OP".into(), code);
            assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
            assert!(config::words(&job, &Pool::of(&card).expect("pool")).is_ok());
            let operation = job.fin[0].clone();
            job.fin = vec![BTreeMap::new(); 16];
            job.fin[0] = operation;
            job.passes[0].insert("FINBASE".into(), 15);
            job.passes[0].insert("FINLEN".into(), 2);
            assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
            assert!(config::words(&job, &Pool::of(&card).expect("pool")).is_ok());
            let unused = job.passes[0].clone();
            job.passes[0].insert("FINLEN".into(), 1);
            job.passes.push(unused);
            assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
            job.g.insert("BUF".into(), 1);
            job.g.insert("NPASS".into(), 1);
            job.passes.swap(1, 0);
            job.passes[0].insert("FINLEN".into(), 0);
            job.passes.push(job.passes[0].clone());
            job.passes[2].insert("FINLEN".into(), 2);
            assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
            job.g.insert("NPASS".into(), 0);
            assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
            let args = match name {
                "fma" => "K0 K1 K2",
                "rcp" | "rsqrt" => "K0",
                _ => "K0 K1",
            };
            let body = format!(
                "            job nw=8 nsr=1 out=f16\n            pass 0\n                fin {name} R0 <- {args}\n"
            );
            assert!(!text_row(&limited, &body));
            assert!(text_row(&card, &body));
        }
    }
    let limited = card
        .with_fact("sr.profile.F_LANES", 0.into())
        .expect("variant");
    for code in [0, 8, 9] {
        let mut job = row_job();
        job.fin[0].insert("OP".into(), code);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
    }
}

#[test]
fn row_features_and_buffer_geometry_are_admitted_before_encoding() {
    let card = Machine::named("sru_v9t7").expect("machine");
    for out in [2, 3] {
        let limited = card
            .with_fact("sr.profile.HAS_MX7", 0.into())
            .expect("variant");
        let mut job = row_job();
        job.g.insert("OUT".into(), out);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
        assert!(config::words(&job, &Pool::of(&card).expect("pool")).is_ok());
        let mode = if out == 2 { "mx7a" } else { "mx7b" };
        let body = format!("            job nw=8 nsr=1 out={mode}\n            pass 0 emit\n");
        assert!(!text_row(&limited, &body));
        assert!(text_row(&card, &body));
        job.g.insert("NW".into(), 9);
        assert!(config::words(&job, &Pool::of(&card).expect("pool")).is_err());
    }
    for nw in [8i128, 9223372036854775816] {
        let text = format!(
            "target ktpu.v9\nfn row_test.l1()\n    send sr[0]\n            job nw={nw} nsr=2 out=f16 buf npass=1\n            pass 1 emit\n            run a=recv from=sr[0] out=4096\n"
        );
        let module = read(&text, "row").expect("syntax");
        let lowered = programs(&module, "row_test", &BTreeMap::new(), &card, Some(1), None)
            .expect("eight-word buffered row");
        let runs: Vec<_> = lowered[0]
            .program
            .steps
            .iter()
            .flat_map(|step| match step {
                NodeStmt::Send { words, .. } => words.as_slice(),
                _ => &[],
            })
            .filter(|word| word.get(252, 4) == 3)
            .map(|word| {
                (
                    word.get(194, 16),
                    word.get(138, 16),
                    word.get(82, 16),
                    word.get(21, 24),
                )
            })
            .collect();
        assert_eq!(runs, vec![(8, 8, 8, 16)]);
    }
    let pool = Pool::of(&card).expect("pool");
    let mut job = row_job();
    job.g.insert("BUF".into(), 1);
    job.g.insert("NPASS".into(), 1);
    job.g.insert("NW".into(), i64::from(pool.buf_words / 2));
    assert!(config::words(&job, &pool).is_ok());
    *job.g.get_mut("NW").expect("NW") += 1;
    assert!(config::words(&job, &pool).is_err());
    for nw in [0, 4096] {
        job.g.insert("NW".into(), nw);
        assert!(config::words(&job, &pool).is_err());
        let body =
            format!("            job nw={nw} nsr=1 out=f16 buf npass=1\n            pass 1 emit\n");
        assert!(!text_row(&card, &body));
    }
    job.g.insert("NW".into(), 8);
    job.g.insert("USE_B".into(), 1);
    assert!(config::words(&job, &pool).is_err());
    for declaration in [
        "job nw=257 nsr=1 out=f16 buf npass=1",
        "job nw=8 nsr=1 out=f16 buf use_b npass=1",
        "job nw=9 nsr=1 out=mx7a",
    ] {
        let body = format!("            {declaration}\n            pass 0 emit\n");
        assert!(!text_row(&card, &body));
    }
    for (field, request) in [("HAS_B", "USE_B"), ("HAS_MASK", "MASK"), ("HAS_R1", "R1")] {
        let limited = card
            .with_fact(&format!("sr.profile.{field}"), 0.into())
            .expect("variant");
        let mut job = row_job();
        job.g.insert(request.into(), 1);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
        assert!(config::words(&job, &pool).is_ok());
    }
    for (field, source) in [("HAS_R1", 4), ("HAS_R2", 5)] {
        let limited = card
            .with_fact(&format!("sr.profile.{field}"), 0.into())
            .expect("variant");
        let mut job = row_job();
        job.fin[0].insert("OP".into(), 0);
        job.fin[0].insert("A".into(), source);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
        assert!(config::words(&job, &pool).is_ok());
    }
    let mut job = row_job();
    job.passes[0].insert("R2EN".into(), 1);
    let limited = card
        .with_fact("sr.profile.HAS_R2", 0.into())
        .expect("variant");
    assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
    for nw in [3, 4099] {
        job.g.insert("NW".into(), nw);
        assert!(config::words(&job, &pool).is_err());
        let body = format!(
            "            job nw={nw} nsr=1 out=f16\n            pass 0 r2\n                fin mov R0 <- ST2\n"
        );
        assert!(!text_row(&card, &body));
    }
    job.g.insert("NW".into(), 4100);
    assert!(config::words(&job, &pool).is_ok());
    job.passes[0].insert("R2EN".into(), 0);
    let mut unused = job.passes[0].clone();
    unused.insert("R2EN".into(), 1);
    job.passes.push(unused);
    assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
    job.g.insert("BUF".into(), 1);
    job.g.insert("NPASS".into(), 1);
    job.passes.swap(0, 1);
    assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
}

#[test]
fn function_programs_cannot_alias_fma_or_another_table_mode() {
    let card = Machine::named("sru_v9t7").expect("machine");
    for (field, value, extra) in [
        ("HAS_FN", serde_json::json!(0), serde_json::json!({})),
        (
            "TAB",
            serde_json::json!("rom"),
            serde_json::json!({"FIX": 1}),
        ),
        (
            "TAB",
            serde_json::json!("rom"),
            serde_json::json!({"CS": 1}),
        ),
        (
            "HAS_MANT",
            serde_json::json!(0),
            serde_json::json!({"MANT": 1}),
        ),
    ] {
        let mut job = row_job();
        let mut op = serde_json::json!({"KIND": 1, "A": "X"});
        op.as_object_mut()
            .expect("object")
            .extend(extra.as_object().expect("object").clone());
        job.ops.push(serde_json::from_value(op).expect("op"));
        job.passes[0].insert("NOPS".into(), 1);
        let limited = card
            .with_fact(&format!("sr.profile.{field}"), value)
            .expect("variant");
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
        assert!(config::words(&job, &Pool::of(&card).expect("pool")).is_ok());
        job.passes.push(job.passes[0].clone());
        job.passes[0].insert("NOPS".into(), 0);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
        job.g.insert("BUF".into(), 1);
        job.g.insert("NPASS".into(), 1);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_err());
        job.passes.swap(0, 1);
        assert!(config::words(&job, &Pool::of(&limited).expect("pool")).is_ok());
        let mode = match field {
            "TAB" if extra.get("FIX").is_some() => "fix",
            "TAB" => "cs=1",
            "HAS_MANT" => "mant",
            _ => "exp",
        };
        let body = format!(
            "            job nw=8 nsr=1 out=f16\n            pass 0 emit\n                op fn a=X k=0 {mode}\n"
        );
        assert!(!text_row(&limited, &body));
        assert!(text_row(&card, &body));
    }
}
