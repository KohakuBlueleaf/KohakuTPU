//! The rows packaging knob (`rows.SINGLE_PACKAGE_AST`) as the frozen vectors
//! name it: API.ROW.003 at the default, with the knob off and with the
//! default reversed, and its max-block witness.
use ktpu_kernels::gen::reductions::SEPARATE_MUTANT;
use ktpu_plan::spec;
use serde_json::Value;
use std::path::Path;

fn vector(path: &str) -> Value {
    serde_json::from_str(
        &std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../tests/spec/vectors")
                .join(path),
        )
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn wide_default_and_explicit_separate() {
    for suffix in ["", "_separate"] {
        let v = vector(&format!(
            "api/row/API.ROW.003.wide_row_partials_folded{suffix}.json"
        ));
        assert_eq!(
            spec::run("tg.run", &v["input"], None, None).unwrap(),
            v["expect"]
        );
        let error = spec::run("tg.run", &v["input"], None, Some("api.rows/max_block")).unwrap_err();
        assert!(
            error.contains("out has extents [16] where the kernel says [37, 16]"),
            "{error}"
        );
    }
}

#[test]
fn reversed_default_takes_the_separate_launches() {
    let single = vector("api/row/API.ROW.003.wide_row_partials_folded.json");
    let separate = vector("api/row/API.ROW.003.wide_row_partials_folded_separate.json");
    let got = ktpu_kernels::mutants::with(SEPARATE_MUTANT, || {
        spec::run("tg.run", &single["input"], None, None)
    });
    assert_eq!(got.unwrap(), separate["expect"]);
    assert_ne!(single["expect"], separate["expect"]);
}
