//! The tier-0 L1 text vectors (`l1.parse`, `l1.module`, `l1.expand`)
//! through `ktpu_l1::spec`.

mod common;

#[test]
fn l1_parse_vectors() {
    common::run_all("l1.parse");
}

#[test]
fn l1_module_vectors() {
    common::run_all("l1.module");
}

#[test]
fn l1_expand_vectors() {
    common::run_all("l1.expand");
}
