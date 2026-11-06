//! Attention assignments retain global head/query coordinates and full keys.

use ktpu_kernels::gen::attention::motif::AttentionMotif;
use ktpu_kernels::gen::attention::{
    assignments, AttentionMask, AttentionPartition, AttentionPrecision, AttentionSpec,
};
use ktpu_kernels::split::NodeSet;

#[test]
fn attention_ownership_keeps_full_keys_and_global_query_origins() {
    let machine = super::machine();
    let spec = AttentionSpec {
        heads: 3,
        queries: 641,
        keys: 773,
        dk: 64,
        dv: 64,
        mask: AttentionMask::Causal { offset: 7 },
        precision: AttentionPrecision::Mx7,
    };
    for ids in [vec![3], vec![3, 1], vec![3, 1, 0, 2]] {
        let nodes = NodeSet::new(ids, &machine).expect("grant");
        let p = nodes.ids().len();
        let mut wanted = vec![
            AttentionPartition::Heads,
            AttentionPartition::QueryRows,
            AttentionPartition::Balanced,
        ];
        for ph in 1..=p {
            if p % ph == 0 {
                wanted.push(AttentionPartition::HeadsQuery { ph, pq: p / ph });
            }
        }
        let plans = assignments(&spec, 128, &nodes).expect("assignments");
        for partition in wanted {
            let plan = plans
                .iter()
                .find(|plan| plan.partition == partition)
                .expect("requested ownership option");
            // Encode each logical (head, query, key) contribution independently.
            // Local query coordinates or a key partition change the result.
            let mut output = vec![None; (spec.heads * spec.queries) as usize];
            let mut work = Vec::new();
            for share in &plan.shares {
                let mut tiles = 0;
                for span in share {
                    tiles += span.query_tiles.end - span.query_tiles.start;
                    for query in span.queries.clone() {
                        let mut sum = 0;
                        let mut unmasked = 0;
                        for key in span.keys.clone() {
                            unmasked += 1 + key + spec.keys * span.head;
                            if key <= query + 7 {
                                sum += 1 + key + spec.keys * span.head;
                            }
                        }
                        let index = (span.head * spec.queries + query) as usize;
                        assert!(
                            output[index].replace((sum, unmasked)).is_none(),
                            "duplicate writer"
                        );
                    }
                }
                work.push(tiles);
            }
            for head in 0..spec.heads {
                for query in 0..spec.queries {
                    let n = spec.keys.min(query + 8);
                    let expected = n * (1 + spec.keys * head) + n * (n - 1) / 2;
                    let all = spec.keys * (1 + spec.keys * head) + spec.keys * (spec.keys - 1) / 2;
                    assert_eq!(
                        output[(head * spec.queries + query) as usize],
                        Some((expected, all))
                    );
                }
            }
            if plan.partition == AttentionPartition::Balanced {
                let lo = work.iter().min().expect("participants");
                let hi = work.iter().max().expect("participants");
                assert!(hi - lo <= 1, "flattened work must be balanced");
            }
        }
    }
}

#[test]
fn attention_roles_never_alias_to_fit_a_smaller_machine() {
    for motif in [
        AttentionMotif::Flash,
        AttentionMotif::Flashd,
        AttentionMotif::Srflash,
    ] {
        assert!(motif.bind_roles(2, 2, 2).is_err());
        let roles = motif.bind_roles(4, 2, 2).expect("resident roles");
        let mut occupied = [false; 4];
        for role in roles.scores.into_iter().chain(roles.products) {
            assert!(!occupied[role], "two live roles share an MG");
            occupied[role] = true;
        }
    }
    assert!(AttentionMotif::Srflash.bind_roles(4, 2, 0).is_err());
    assert!(AttentionMotif::Srflash.bind_roles(4, 0, 2).is_ok());
    assert!(AttentionMotif::Flash.bind_roles(4, 0, 2).is_err());
    assert!(AttentionMotif::Flashd.bind_roles(4, 0, 2).is_err());
}
