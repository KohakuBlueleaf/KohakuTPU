//! Forced prices are not complete automatic-selection results.
use super::*;
use ktpu_kernels::split::cost::ChoiceCache;

#[test]
fn a_forced_price_cannot_hide_a_cheaper_automatic_option() {
    let m = machine();
    let template = call_split(
        "convert.copy_kernel",
        &json!({"nbytes": 128}),
        &request(&m),
        &m,
    )
    .expect("copy ownership")
    .split;
    let candidate = |id: &str, cycles| {
        let mut option = template.clone();
        option.id = id.into();
        option.cost.makespan = cycles;
        option
    };
    let slow = candidate("slow", 90);
    let fast = candidate("fast", 20);
    let cache = ChoiceCache::default();
    let forced = cache
        .choose("schedule".into(), Some("slow"), || Ok(vec![slow.clone()]))
        .expect("forced price");
    let automatic = cache
        .choose("schedule".into(), None, || {
            Ok(vec![slow.clone(), fast.clone()])
        })
        .expect("complete comparison");
    assert_eq!(automatic.id, fast.id);
    assert!(automatic.cost.makespan < forced.cost.makespan);
    let cached = cache
        .choose("schedule".into(), Some("slow"), || {
            panic!("a complete cached comparison already contains the forced price")
        })
        .expect("cached forced price");
    assert_eq!(cached.cost.makespan, forced.cost.makespan);

    let pruned = ChoiceCache::default();
    pruned
        .choose("bounded".into(), None, || Ok(vec![fast.clone()]))
        .expect("automatic comparison with a dominated option omitted");
    let forced = pruned
        .choose("bounded".into(), Some("slow"), || Ok(vec![slow.clone()]))
        .expect("a bound-pruned legal option is still forceable");
    assert_eq!(forced.cost.makespan, slow.cost.makespan);
    let automatic = pruned
        .choose("bounded".into(), None, || {
            panic!("the exact winning price is cached")
        })
        .expect("automatic cached choice");
    assert_eq!(automatic.cost.makespan, fast.cost.makespan);
}
