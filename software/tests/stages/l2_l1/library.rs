//! L2 to L1 over a template library (`KTPU_SIM_LIBRARY`): each kernel's L2 on
//! the L2 simulator and its L1 on the L1 simulator end in the same parameters.

use std::path::Path;

#[path = "../common.rs"]
mod common;

use common::{judge, Level, Pair};

#[test]
fn every_kernel_ends_as_its_l2() {
    let Ok(root) = std::env::var("KTPU_SIM_LIBRARY") else {
        return;
    };
    let mut pairs = Vec::new();
    for f in std::fs::read_dir(Path::new(&root)).unwrap() {
        let path = f.unwrap().path();
        let file = path.file_name().unwrap().to_string_lossy().to_string();
        if file.len() < 60 {
            continue;
        }
        let doc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let compiled = &doc["compiled"];
        let (l1, l2) = (
            compiled["l1"].as_str().unwrap(),
            compiled["l2"].as_str().unwrap(),
        );
        let name = compiled["name"].as_str().unwrap().to_string();
        let pair = Pair {
            name,
            before: (Level::L2, l2.into()),
            after: (Level::L1, l1.into()),
        };
        pairs.push((file[..12].to_string(), Ok(pair)));
    }
    judge(&pairs);
}
