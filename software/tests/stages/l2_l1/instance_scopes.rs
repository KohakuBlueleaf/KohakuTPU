use ktpu_l2::ir::read::{instance_scopes, instances, last_item_emissions};
use ktpu_l2::ir::{Body, Stage, StageX};
use ktpu_l2::lower::vector;
use ktpu_l2::{Program, Search, Target};

use super::common::card;

fn costs(body: &Body, stages: &[Stage], target: &Target) -> Vec<i64> {
    let search = Search::default();
    stages
        .iter()
        .filter_map(|stage| match &stage.x {
            StageX::Par(parts) => Some(
                parts
                    .iter()
                    .map(|part| {
                        vector::total(body, part.par(), &part.env, target, &search)
                            .expect("instance price")
                    })
                    .max()
                    .unwrap_or(0),
            ),
            _ => None,
        })
        .collect()
}

#[test]
fn pricing_scopes_keep_stage_costs_without_materializing_dynamic_item_environments() {
    let target = Target::of(&card());
    let mut previous = None;
    for trips in [8, 128] {
        let rows = trips * 8;
        let source = format!(
            r#"target ktpu.v9
fn scopes.l2(x: f16[{rows}, 32] @dram, y: f16[{rows}, 32] @dram)
    par c in 0..2 on vc[c]
        for t in c*{trips}..(c+1)*{trips}
            h = load x[t*4:+4, 0:32] : f16[4, 32] @l1(gt4)
            v = unpack h : f32[4, 32] @reg(gt4)
            z = mul v, 0.5 : f32[4, 32] @reg(gt4)
            q = pack z : f16[4, 32] @l1(gt4)
            store y[t*4:+4, 0:32] <- q
"#
        );
        let p = Program::parse(&source, "scopes", Some("scopes")).expect("row stream");
        let body = p.body().expect("body");
        let full = instances(&body).expect("expanded instances");
        let expanded = last_item_emissions();
        let scopes = instance_scopes(&body).expect("structured instances");
        let compact = last_item_emissions();
        assert!(
            compact < expanded,
            "pricing does not emit dynamic environments"
        );
        if let Some(old) = previous {
            assert_eq!(compact, old, "scope work does not grow with the stream");
        }
        previous = Some(compact);
        assert_eq!(costs(&body, &scopes, &target), costs(&body, &full, &target));
    }
}
