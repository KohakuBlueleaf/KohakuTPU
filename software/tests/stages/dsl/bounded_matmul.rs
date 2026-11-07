//! Geometry bounds preserve exhaustive selection while omitting dominated work.
use super::*;
use ktpu_kernels::gen::matmul::{self, split as matrix};
use ktpu_kernels::split::cost::{price_bounded, select_split};

#[test]
fn matrix_bounds_skip_losing_compilations_without_changing_the_winner() {
    ktpu_graph::with_cycle_cache(|| {
        let m = machine();
        let knobs = matmul::unrecorded();
        let mm = matmul::plan(
            2,
            32,
            64,
            128,
            true,
            4,
            ktpu_machine::Pairing::Unpaired,
            &m,
            &knobs,
        )
        .expect("batched product");
        let e = ktpu_kernels::expr::load(0);
        let out = ktpu_kernels::Operand::full(mm.n, mm.bt * mm.mp);
        let made = BTreeMap::new();
        let shape = matrix::Shape {
            mm: &mm,
            class: None,
            e: &e,
            extras: &[],
            out: &out,
            knobs: &knobs,
            made: &made,
            demands: &[],
        };
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![0], &m).expect("unscoped physical owner");
        let options = matrix::split_options(&shape, &req, &m).expect("candidate geometries");
        let mut exact = BTreeMap::new();
        let mut candidates = Vec::new();
        for option in options {
            let floor =
                matrix::resource_floor(&shape, &req, &option, &m).expect("ordinary matrix bound");
            let mut forced = req.clone();
            forced.force = Some(option.id.clone());
            let priced = matrix::kernel(&shape, &forced, &m).expect("complete forced price");
            assert!(
                floor <= priced.split.cost.makespan,
                "{}: bound {floor} exceeds {:?}",
                option.id,
                priced.split.cost,
            );
            exact.insert(option.id.clone(), priced.split);
            candidates.push((option, Some(floor)));
        }
        let exhaustive = select_split(exact.values().cloned().collect(), None)
            .expect("exhaustive winner including ID ties");
        let mut compiled = Vec::new();
        let bounded = price_bounded(candidates, |option| {
            compiled.push(option.id.clone());
            Ok(exact[&option.id].clone())
        })
        .expect("bounded comparison");
        assert!(
            compiled.len() < exact.len(),
            "dominated graphs are not compiled"
        );
        let bounded = select_split(bounded, None).expect("bounded winner");
        assert_eq!(bounded.id, exhaustive.id);
        assert_eq!(bounded.cost.makespan, exhaustive.cost.makespan);
        let automatic = matrix::kernel(&shape, &req, &m).expect("automatic bounded planner");
        assert_eq!(automatic.split.id, exhaustive.id);
        assert_eq!(automatic.split.cost.makespan, exhaustive.cost.makespan);
    });
}
