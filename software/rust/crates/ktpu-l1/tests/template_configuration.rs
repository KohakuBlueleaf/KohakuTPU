//! Parameter requests cannot participate in constant configuration elision.
use ktpu_isa::unit::vector::VecCu;
use ktpu_l0::Resident as _;
use ktpu_l1::lower::image::{image, UNKNOWN};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::read;
use ktpu_machine::Machine;

fn encode(second: i64, parameter: bool) -> Vec<u64> {
    let k = |id: &str, n| {
        if parameter {
            format!("parameter(\"{id}\", {n})")
        } else {
            format!("{n}")
        }
    };
    let text = format!("target ktpu.v9\nimage lanes()\n    vmul v1, k1, rot(v0, {})\n    vmul v2, k1, rot(v1, {})\n    vmul v3, k1, rot(v2, 1)\n    halt\n", k("a", 1), k("b", second));
    let module = read(&text, "<template>").unwrap();
    let machine = Machine::named("multimesh_v9t6").unwrap();
    image(&module, &machine, "lanes", &[], UNKNOWN).unwrap().0
}

#[test]
fn equal_samples_do_not_equate_distinct_parameters() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    for (rhs, accepted) in [
        ("parameter(\"a\", 1)", true),
        ("parameter(\"b\", 1)", false),
        ("1", false),
    ] {
        let text = format!("target ktpu.v9\nimage shared()\n    vfma v2, v0, rot(v1, parameter(\"a\", 1)), rot(v1, {rhs})\n    halt\n");
        let module = read(&text, "<template>").unwrap();
        let result = image(&module, &machine, "shared", &[], UNKNOWN);
        assert_eq!(result.is_ok(), accepted, "{rhs}: {result:?}");
    }
}

#[test]
fn parameter_bindings_keep_structure_constants_keep_elision() {
    let equal = encode(1, true);
    let unequal = encode(2, true);
    assert_eq!(equal.len(), unequal.len());
    assert_ne!(equal, unequal);
    assert!(encode(1, false).len() < encode(2, false).len());
}

fn resident_lengths(parameter: bool) -> (usize, usize) {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let mut cores = Cores::new(&machine).unwrap();
    cores.parameter_images = parameter;
    let mut words = Vec::new();
    for (addr, word) in encode(1, parameter).into_iter().enumerate() {
        words.push(
            VecCu::Imem {
                addr: addr as u32,
                word,
            }
            .encode(&cores.isa)
            .unwrap(),
        );
    }
    words.push(VecCu::Run { pc: 0 }.encode(&cores.isa).unwrap());
    let coord = (2, 1);
    let a = cores.lower("VC", coord, words.clone()).unwrap();
    let b = cores.lower("VC", coord, words).unwrap();
    (a.len(), b.len())
}

#[test]
fn sampled_images_do_not_establish_resident_constants() {
    let (a, b) = resident_lengths(true);
    assert_eq!(a, b);
    let (a, b) = resident_lengths(false);
    assert!(a > b);
}

#[cfg(feature = "mutants")]
#[test]
fn value_dependent_parameter_elision_is_killed() {
    ktpu_l1::mutant::with("l1.img/parameter-elision", || {
        assert_ne!(encode(1, true).len(), encode(2, true).len());
        let (a, b) = resident_lengths(true);
        assert!(a > b);
    });
}
