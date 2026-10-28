//! The tier-0 lowering vectors (`l1.steps`, `l1.build`, `l1.package`)
//! through `ktpu_l1::spec`.

mod common;

#[test]
fn l1_steps_vectors() {
    common::run_all("l1.steps");
}

#[test]
fn l1_build_vectors() {
    common::run_all("l1.build");
}

#[test]
fn l1_package_vectors() {
    assert!(common::run_all("l1.package") > 0);
}
