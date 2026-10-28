//! The UOp graph against tinygrad's own: each golden ast (written in
//! tinygrad's `toposort` order) reads into distinct hash-consed nodes, and
//! `toposort` from the root gives them back in that order.

use std::path::{Path, PathBuf};

use ktpu_plan::uop::Graph;
use ktpu_plan::Case;

fn goldens() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

#[test]
fn toposort_is_tinygrads_order() {
    let mut asts = 0;
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        for p in &case.plans {
            let (g, root) = Graph::read(&p.uops);
            let order = g.toposort(root);
            assert_eq!(
                order,
                (0..p.uops.nodes.len()).collect::<Vec<_>>(),
                "{}: a node read twice or out of order",
                f.display()
            );
            asts += 1;
        }
    }
    assert!(asts >= 100, "{asts} asts");
}
