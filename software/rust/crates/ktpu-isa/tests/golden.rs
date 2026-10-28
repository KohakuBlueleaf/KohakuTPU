//! Byte goldens from the Python encoders (`software/rust/golden/isa_dump.py`):
//! every case's payloads, decodes, address fields and refusal clauses.

use ktpu_isa::unit::sru::{self, Job, Pool, PoolOp};
use ktpu_isa::unit::vector::{dim, e8m15};
use ktpu_isa::{ClusterInst, Isa, Machine, MoverOp, Refusal, SruInst, VWord, VecCu, VecIsa, W256};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;

fn golden(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/isa")
        .join(name);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path:?}: {e}"));
    serde_json::from_str(&text).unwrap()
}

fn isa_for(doc: &Value) -> Isa {
    Isa::new(&Machine::named(doc["machine"].as_str().unwrap()).unwrap()).unwrap()
}

fn hex(v: &Value) -> W256 {
    W256::from_hex(v.as_str().unwrap()).unwrap()
}

fn decoded(name: &str, fields: &[(&str, i64)]) -> Value {
    json!([
        name,
        fields
            .iter()
            .map(|(n, v)| json!([n, v]))
            .collect::<Vec<_>>()
    ])
}

fn addrs(fields: &[ktpu_isa::AddressField]) -> Value {
    Value::Array(
        fields
            .iter()
            .map(|f| {
                let segs: Vec<Value> = f
                    .segments
                    .iter()
                    .map(|s| json!([s.bit, s.width, s.shift]))
                    .collect();
                json!([segs, f.value])
            })
            .collect(),
    )
}

/// `Ok` matches the case's outputs (checked by `check`), `Err` its clause.
fn expect<T: std::fmt::Debug>(case: &Value, got: Result<T, Refusal>, check: impl FnOnce(T)) {
    match (case.get("refuse").and_then(Value::as_str), got) {
        (Some(clause), Err(r)) => assert_eq!(r.clause, clause, "{case}: {r}"),
        (Some(clause), Ok(v)) => panic!("{case}: accepted, want refusal {clause}: {v:?}"),
        (None, Err(r)) => panic!("{case}: refused {r}"),
        (None, Ok(v)) => check(v),
    }
}

#[test]
fn cluster_goldens() {
    let doc = golden("cluster.json");
    let isa = isa_for(&doc);
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() > 150);
    for case in cases {
        let inst: ClusterInst = serde_json::from_value(case["inst"].clone()).unwrap();
        expect(case, inst.encode(&isa.cluster), |w| {
            assert_eq!(w, hex(&case["words"][0]), "{case}");
            let (name, fields) = isa.cluster.set.decode(w).unwrap();
            assert_eq!(decoded(name, &fields), case["decode"], "{case}");
            assert_eq!(
                addrs(&isa.cluster.addresses(w)),
                case["addresses"],
                "{case}"
            );
        });
    }
    for (name, segs) in doc["segments"].as_object().unwrap() {
        let got: Vec<Value> = isa
            .cluster
            .segments(name)
            .iter()
            .map(|s| json!([s.bit, s.width, s.shift]))
            .collect();
        assert_eq!(Value::Array(got), *segs, "{name}");
    }
}

fn with_regs(isa: &VecIsa, regs: u32) -> VecIsa {
    let mut cfg = isa.cfg.clone();
    cfg.regs = regs;
    VecIsa::of(cfg)
}

#[test]
fn vector_goldens() {
    let doc = golden("vector.json");
    let isa = isa_for(&doc);
    let by_regs: BTreeMap<u32, VecIsa> = [32, 128].map(|r| (r, with_regs(&isa.vector, r))).into();
    let words = doc["words"].as_array().unwrap();
    assert!(words.len() > 150);
    for case in words {
        let word: VWord = serde_json::from_value(case["word"].clone()).unwrap();
        let v = &by_regs[&(case["regs"].as_u64().unwrap() as u32)];
        expect(case, word.encode(v), |w| {
            assert_eq!(W256::from_u64(w), hex(&case["value"]), "{case}");
            let (name, fields) = v.core.set.decode(W256::from_u64(w & 0xFFFF_FFFF)).unwrap();
            assert_eq!(decoded(name, &fields), case["decode"], "{case}");
        });
    }
    for case in doc["cu"].as_array().unwrap() {
        let cu: VecCu = serde_json::from_value(case["cu"].clone()).unwrap();
        expect(case, cu.encode(&isa.vector), |p| {
            assert_eq!(p, hex(&case["payload"]), "{case}");
            let (name, fields) = isa.vector.set.decode(p).unwrap();
            assert_eq!(decoded(name, &fields), case["decode"], "{case}");
            assert_eq!(addrs(&isa.vector.addresses(p)), case["addresses"], "{case}");
            assert_eq!(isa.vector.decode_cu(p).as_ref(), Some(&cu), "{case}");
            if name == "IMEM" {
                assert_eq!(
                    W256::from_u64(isa.vector.imem_word(p)),
                    hex(&case["imem_word"])
                );
                let VecCu::Imem { addr, .. } = cu else {
                    unreachable!()
                };
                assert_eq!(isa.vector.imem_addr(p), addr);
            }
        });
    }
    for case in doc["dims"].as_array().unwrap() {
        let got = dim(
            case["stride"].as_i64().unwrap(),
            case["bound"].as_u64().unwrap() as u32,
        );
        assert_eq!(got.unwrap(), case["value"].as_u64().unwrap(), "{case}");
    }
    for case in doc["dim_refuse"].as_array().unwrap() {
        let got = dim(
            case["stride"].as_i64().unwrap(),
            case["bound"].as_u64().unwrap() as u32,
        );
        expect(case, got, |_| ());
    }
    for case in doc["e8m15"].as_array().unwrap() {
        let x = f64::from_bits(case["bits"].as_u64().unwrap());
        assert_eq!(e8m15(x) as u64, case["value"].as_u64().unwrap(), "{case}");
    }
    let p = &doc["program"];
    let words: Vec<u64> = p["words"]
        .as_array()
        .unwrap()
        .iter()
        .map(|w| hex(w).low_u64())
        .collect();
    let got = isa
        .vector
        .program(&words, p["pc"].as_u64().unwrap() as u32)
        .unwrap();
    let want: Vec<W256> = p["payloads"].as_array().unwrap().iter().map(hex).collect();
    assert_eq!(got, want);
}

fn pool(machine: &Machine, case: &Value) -> Pool {
    let mut pool = Pool::of(machine).unwrap();
    pool.f_lanes = case["f_lanes"].as_u64().unwrap() as u32;
    pool.h_taps = case["h_taps"].as_u64().unwrap() as u32;
    pool
}

#[test]
fn sru_goldens() {
    let doc = golden("sru.json");
    let machine = Machine::named(doc["machine"].as_str().unwrap()).unwrap();
    let isa = Isa::new(&machine).unwrap();
    for case in doc["insts"].as_array().unwrap() {
        let inst: SruInst = serde_json::from_value(case["inst"].clone()).unwrap();
        expect(case, inst.encode(&isa.sru), |words| {
            let want: Vec<W256> = case["words"].as_array().unwrap().iter().map(hex).collect();
            assert_eq!(words, want, "{case}");
            for (i, w) in words.iter().enumerate() {
                let (name, fields) = isa.sru.set.decode(*w).unwrap();
                assert_eq!(decoded(name, &fields), case["decode"][i], "{case}");
                assert_eq!(
                    addrs(&isa.sru.addresses(*w)),
                    case["addresses"][i],
                    "{case}"
                );
            }
        });
    }
    for case in doc["jobs"].as_array().unwrap() {
        let job: Job = serde_json::from_value(case["job"].clone()).unwrap();
        expect(case, sru::words(&job, &pool(&machine, case)), |words| {
            let got: Vec<Value> = words.iter().map(|(a, v)| json!([a, v])).collect();
            assert_eq!(Value::Array(got), case["words"], "{case}");
        });
    }
    let schedules = doc["schedules"].as_array().unwrap();
    assert!(schedules.iter().any(|c| c.get("refuse").is_some()));
    for case in schedules {
        let prog: Vec<PoolOp> = serde_json::from_value(case["prog"].clone()).unwrap();
        expect(case, sru::schedule(&prog, &pool(&machine, case)), |s| {
            assert_eq!(
                serde_json::to_value(&s.fields).unwrap(),
                case["fields"],
                "{case}"
            );
            assert_eq!(json!(s.ii), case["ii"], "{case}");
            assert_eq!(json!(s.sigma), case["sigma"], "{case}");
        });
    }
    for case in doc["e8"].as_array().unwrap() {
        let x = f64::from_bits(case["bits"].as_u64().unwrap());
        assert_eq!(sru::e8(x) as u64, case["value"].as_u64().unwrap(), "{x:e}");
    }
}

#[test]
fn mover_goldens() {
    let doc = golden("mover.json");
    let isa = isa_for(&doc);
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() > 80);
    for case in cases {
        let op: MoverOp = serde_json::from_value(case["op"].clone()).unwrap();
        expect(case, isa.mover.writes(&op), |writes| {
            let got: Vec<Value> = writes.iter().map(|(r, v)| json!([r, v])).collect();
            assert_eq!(Value::Array(got), case["writes"], "{case}");
            let fields: Vec<Value> = isa
                .mover
                .fields(&writes)
                .into_iter()
                .map(|((r, n), (i, b, w))| json!([r, n, i, b, w]))
                .collect();
            assert_eq!(Value::Array(fields), case["fields"], "{case}");
            let payloads = case["payloads"].as_array().unwrap();
            assert_eq!(payloads.len(), writes.len().div_ceil(2));
            for (i, p) in payloads.iter().enumerate() {
                let p = hex(p);
                let (reg, value) = writes[2 * i];
                assert_eq!((p.get(0, 64), p.get(64, 64)), (reg as u64, value), "{case}");
                assert_eq!(
                    addrs(&isa.mover.addresses(p)),
                    case["addresses"][i],
                    "{case}"
                );
            }
        });
    }
    for case in doc["flags"].as_array().unwrap() {
        let got = isa
            .mover
            .flag_byte(
                case["wcoal"].as_bool().unwrap(),
                case["burst"].as_i64().unwrap(),
            )
            .unwrap();
        assert_eq!(got as u64, case["value"].as_u64().unwrap());
    }
}
