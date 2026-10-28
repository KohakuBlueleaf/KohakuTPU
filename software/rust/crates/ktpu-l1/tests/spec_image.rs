//! The tier-0 image vectors (`l1.image`) through `ktpu_l1::spec`. The
//! scheduler vectors are tests/sched/vectors.rs.

mod common;

#[test]
fn l1_image_vectors() {
    common::run_all("l1.image");
}
