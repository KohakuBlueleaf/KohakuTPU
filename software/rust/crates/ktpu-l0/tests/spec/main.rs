//! Every tier-0 spec vector whose runner has a port in
//! `ktpu_l0::spec::ports()`, run and compared with its `expect`; a frozen
//! vector a ruling supersedes runs with its ruled input and output.

#[path = "../common/mod.rs"]
mod common;

use ktpu_l0::spec::SUPERSEDED;

#[test]
fn every_ported_vector_meets_its_expect() {
    let (mut pass, mut ruled, mut superseded) = (0, Vec::new(), Vec::new());
    let (mut failed, mut unported) = (Vec::new(), Vec::new());
    for (name, v) in common::ported_vectors() {
        let got = match common::run_vector(&v) {
            Ok(g) => g,
            Err(e) => {
                unported.push(format!("{name}: {e}"));
                continue;
            }
        };
        if got != v["expect"] {
            failed.push(format!("{name}:\n  got  {got}\n  want {}", v["expect"]));
        } else if SUPERSEDED.iter().any(|s| s.vector == name) {
            superseded.push(name);
        } else if v["status"] == "ruling" {
            ruled.push(name);
        } else {
            pass += 1;
        }
    }
    println!("frozen passed: {pass}");
    println!("rulings followed: {ruled:#?}");
    println!("superseded vectors met with their ruled input: {superseded:#?}");
    println!("not ported: {unported:#?}");
    assert!(
        failed.is_empty(),
        "{} vectors differ:\n{}",
        failed.len(),
        failed.join("\n")
    );
    assert_eq!(superseded.len(), SUPERSEDED.len());
    // A ruling vector moves to frozen once the stack follows it.
    assert!(
        pass + ruled.len() >= 79,
        "only {pass} frozen and {} ruling vectors passed",
        ruled.len()
    );
}
