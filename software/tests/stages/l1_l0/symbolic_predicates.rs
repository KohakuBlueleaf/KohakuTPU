//! Behavioral witnesses for support factoring, using the production engine and
//! bound packages. The finite oracle below is a truth mask, not another BDD.
use std::collections::BTreeMap;

use ktpu_l0::format::{Alu, Cond};
use ktpu_l0::program::{BuildOpts, Program, Stmt};
use ktpu_verify::{Symbolic, SymbolicValue as Value};

use super::common::card;

fn bit(s: &mut Symbolic) -> Value {
    let word = s.fresh();
    s.alu(Alu::And as u32, word, Value::Known(1)).unwrap()
}

fn eq(s: &mut Symbolic, value: Value, constant: u64) -> usize {
    s.condition(Cond::Eq as u32, value, Value::Known(constant))
}

// Mask bit x+2*y is the result for that assignment. Build a sum of minterms
// with real word ALU operations, independently of predicate restriction.
fn truth(s: &mut Symbolic, x: Value, y: Value, mask: u8) -> Value {
    let nx = s.alu(Alu::Xor as u32, x, Value::Known(1)).unwrap();
    let ny = s.alu(Alu::Xor as u32, y, Value::Known(1)).unwrap();
    let mut result = Value::Known(0);
    for assignment in 0..4 {
        if mask & (1 << assignment) != 0 {
            let a = if assignment & 1 == 0 { nx } else { x };
            let b = if assignment & 2 == 0 { ny } else { y };
            let term = s.alu(Alu::And as u32, a, b).unwrap();
            result = s.alu(Alu::Or as u32, result, term).unwrap();
        }
    }
    result
}

fn assignments(path: &Symbolic, x: Value, y: Value, z: Value) -> u8 {
    let mut mask = 0;
    for assignment in 0..8 {
        let mut trial = path.clone();
        let conditions = [
            eq(&mut trial, x, assignment & 1),
            eq(&mut trial, y, (assignment >> 1) & 1),
            eq(&mut trial, z, (assignment >> 2) & 1),
        ];
        if conditions.into_iter().all(|c| trial.try_assume(c)) {
            mask |= 1 << assignment;
        }
    }
    mask
}

fn nodes(p: &Program) -> (ktpu_verify::Nodes, ktpu_verify::Target) {
    let machine = card();
    let facts = ktpu_l0::Facts::of_mesh(&machine, 0).unwrap();
    let package = p.build(&facts, BuildOpts::default()).unwrap().build(false, false);
    let target = ktpu_verify::Target::from_machine(&machine, Some(1)).unwrap();
    (BTreeMap::from([(0, package)]), target)
}

fn alu(p: &mut Program, rd: u64, op: Alu, ra: u64, b: u64, imm: bool) {
    p.steps.push(Stmt::Alu { rd, op, ra, b, imm });
}

fn assert_zero(p: &mut Program, reg: u64, code: u64) {
    p.steps.push(Stmt::AssertZero { reg, code });
}

#[test]
fn support_factoring_matches_exhaustive_truth_functions() {
    let mut s = Symbolic::default();
    let x = bit(&mut s);
    let y = bit(&mut s);
    let z = bit(&mut s);
    let z_one = eq(&mut s, z, 1);
    for a in 0..16u8 {
        let av = truth(&mut s, x, y, a);
        let ac = eq(&mut s, av, 1);
        for b in 0..16u8 {
            let bv = truth(&mut s, x, y, b);
            // Both branch polarities, including false incoming predicates.
            for wanted in [0, 1] {
                let bc = eq(&mut s, bv, wanted);
                let mut path = s.clone();
                path.assume(z_one); // disjoint factor must survive every merge
                path.assume(ac);
                path.assume(bc);
                let expected = a & if wanted == 1 { b } else { !b & 15 };
                assert_eq!(assignments(&path, x, y, z), expected << 4,
                    "a={a:04b}, b={b:04b}, polarity={wanted}");
            }
        }
    }
    // One condition touches TWO previously independent factors. Keeping just
    // one overlapping owner would accept this contradiction.
    let x_one = eq(&mut s, x, 1);
    let y_one = eq(&mut s, y, 1);
    let both = truth(&mut s, x, y, 0b1000);
    let both_one = eq(&mut s, both, 1);
    let different = truth(&mut s, x, y, 0b0110);
    let different_one = eq(&mut s, different, 1);
    let mut separate = s.clone();
    separate.assume(x_one);
    separate.assume(y_one);
    let mut grouped = s.clone();
    grouped.assume(both_one);
    assert_eq!(assignments(&separate, x, y, z), 0b10001000);
    assert_eq!(separate, grouped, "state equality cannot compare factor keys");
    let mut classes = vec![separate.clone()];
    if !classes.contains(&grouped) { classes.push(grouped); }
    let mut weaker = s.clone();
    weaker.assume(x_one);
    if !classes.contains(&weaker) { classes.push(weaker); }
    assert_eq!(classes.len(), 2, "dedup equivalent paths, not weaker ones");
    assert!(!separate.try_assume(different_one));
}

#[test]
fn failed_assertions_preserve_distinct_incoming_path_identities() {
    let mut s = Symbolic::default();
    let x = bit(&mut s);
    let y = bit(&mut s);
    let z = bit(&mut s);
    let either = truth(&mut s, x, y, 0b1110);
    let either_one = eq(&mut s, either, 1);
    let x_zero = eq(&mut s, x, 0);
    let y_zero = eq(&mut s, y, 0);
    let mut reduced = s.clone();
    reduced.assume(either_one);
    reduced.assume(x_zero); // the overlap forces x=0, y=1
    let incoming = reduced.clone();
    assert!(!reduced.try_assume(y_zero));
    assert_eq!(reduced, incoming);
    assert_eq!(assignments(&reduced, x, y, z), 0b01000100);

    let mut other = s.clone();
    other.assume(y_zero);
    let other_incoming = other.clone();
    assert!(other.try_assume(either_one));
    assert_eq!(assignments(&other, x, y, z), 0b00100010);
    assert_ne!(other, reduced);
    // A known-false assertion must not collapse distinct terminal identities.
    other = other_incoming.clone();
    assert!(!other.try_assume(0));
    assert_eq!(other, other_incoming);
    assert_ne!(other, reduced);
}

#[test]
fn independent_package_assertions_do_not_build_a_global_path_diagram() {
    let mut p = Program::new();
    let mut oracle = Symbolic::default();
    for i in 0..40 {
        p.load(0, 4096 + 8 * i, None, false);
        assert_zero(&mut p, 0, i);
        let input = oracle.fresh();
        let condition = eq(&mut oracle, input, 0);
        let before = oracle.storage();
        assert!(oracle.try_assume(condition));
        assert_eq!(oracle.storage(), before,
            "an independent assertion must allocate neither prefix nor complement");
    }
    let (nodes, target) = nodes(&p);
    let findings = ktpu_verify::check(std::slice::from_ref(&nodes), &target);
    assert!(findings.is_empty(), "{findings:?}");
    let scheduled = ktpu_verify::schedule(&nodes, &BTreeMap::new(), &target).unwrap();
    let paths: Vec<_> = scheduled.symbolic_paths().collect();
    assert_eq!(paths.len(), 1, "ASSERT_ZERO must not fork a failing successor");
    assert_eq!(paths[0].storage(), oracle.storage(),
        "node ASSERT_ZERO must use the same one-sided restriction");
    assert_eq!(ktpu_verify::check_scheduled(&scheduled, &[], &target), findings);
}

// Copies r2/r3 deliberately retain the unknown values: ASSERT_ZERO overwrites
// its operand register with known zero, which must not make the later proof
// accidentally concrete. r4 names a condition spanning both input supports.
fn overlapping_package(grouped: bool, contradiction: bool) -> Program {
    let mut p = Program::new();
    p.load(0, 4096, None, false);
    p.load(1, 4104, None, false);
    alu(&mut p, 2, Alu::Mov, 0, 0, false);
    alu(&mut p, 3, Alu::Mov, 0, 1, false);
    alu(&mut p, 4, Alu::Or, 0, 1, false);
    if grouped {
        assert_zero(&mut p, 4, 10);
    } else {
        assert_zero(&mut p, 0, 10);
        assert_zero(&mut p, 1, 11);
    }
    alu(&mut p, 4, Alu::Or, 2, 3, false);
    if contradiction {
        alu(&mut p, 4, Alu::Xor, 4, 1, true);
        assert_zero(&mut p, 4, 99);
    } else {
        p.jump("end", Cond::Eq, 4, 0, true);
    }
    p.wait_bell(0, 1); // unreachable both after a failed contract and on success
    p.label("end").unwrap();
    p
}

#[test]
fn grouped_package_contracts_have_the_same_findings_and_feasible_paths() {
    let mut observed = Vec::new();
    for grouped in [false, true] {
        let (nodes, target) = nodes(&overlapping_package(grouped, false));
        let findings = ktpu_verify::check(std::slice::from_ref(&nodes), &target);
        assert!(findings.is_empty(), "{findings:?}");
        let scheduled = ktpu_verify::schedule(&nodes, &BTreeMap::new(), &target).unwrap();
        assert_eq!(scheduled.symbolic_paths().count(), 1,
            "the exhaustive reading has only the taken successor");
        assert_eq!(ktpu_verify::check_scheduled(&scheduled, &[], &target), findings);
        let (bad, target) = self::nodes(&overlapping_package(grouped, true));
        let failed = ktpu_verify::check(&[bad], &target);
        assert_eq!(failed.len(), 1, "no deadlock past a terminal assertion: {failed:?}");
        assert_eq!(failed[0].kind, ktpu_verify::Kind::Model);
        assert_eq!(failed[0].text, "ASSERT_ZERO contract 99 cannot succeed");
        observed.push(failed);
    }
    assert_eq!(observed[0], observed[1], "grouping cannot alter findings");
}

#[test]
fn computed_cache_is_bounded_without_losing_symbolic_identity() {
    let mut s = Symbolic::default();
    let first = s.fresh();
    let first_zero = eq(&mut s, first, 0);
    let mut retained = s.clone();
    assert!(retained.try_assume(first_zero));
    // Full-width equality introduces 64 complements + (1+...+63)
    // conjunction keys = 2,080 keys/input before eviction. Including first,
    // 35 inputs cross 65,536 and evict the oldest equality. No assertion
    // combines these inputs into a large correlated diagram.
    let mut saturated = false;
    for _ in 0..34 {
        let input = s.fresh();
        let condition = eq(&mut s, input, 0);
        let count = s.storage().2;
        assert!(count <= 65_536, "computed entries are bounded, got {count}");
        saturated |= count == 65_536;
        let mut path = s.clone();
        assert!(path.try_assume(condition));
    }
    assert!(saturated, "fixture must actually cross the cache capacity");
    let before = s.storage();
    assert_eq!(eq(&mut s, first, 0), first_zero,
        "evict computed results, never immutable node/word identities");
    assert_eq!(s.storage().0, before.0, "recomputation reuses unique nodes");
    assert_eq!(s.storage().3, before.3);
    let one = eq(&mut s, first, 1);
    assert!(!retained.try_assume(one), "retained predicates survive eviction");
    assert!(retained.try_assume(first_zero));
}
