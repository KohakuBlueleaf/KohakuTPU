//! Unfitted local-schedule timing; no measured speedup is implied.

use super::SplitOption;
use crate::trace::{fail, R};
use ktpu_machine::Machine;

#[derive(Clone, Debug)]
pub struct CostEstimate {
    pub makespan: i64,
    pub local_cycles: Vec<i64>,
    pub dispatch_intercept: i64,
    pub revision: String,
    pub provenance: String,
    pub uncertainty: String,
    pub measured: bool,
}

impl Default for CostEstimate {
    fn default() -> Self {
        Self {
            makespan: 0,
            local_cycles: vec![],
            dispatch_intercept: 0,
            revision: "local-timing-v1".into(),
            provenance: "ktpu-l2 cost / L1 timed".into(),
            uncertainty: "unfitted".into(),
            measured: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct CostFeatures {
    /// Complete local schedules, including their conversions and event waits,
    /// without the launch intercept already included by `graph::cycles`.
    pub share_cycles: Vec<i64>,
    pub dispatch_intercept: i64,
}

impl CostFeatures {
    pub fn of_shares(kernels: &[crate::kernel::Kernel], machine: &Machine) -> R<Self> {
        let front = machine.vc.launch_front as i64;
        let share_cycles = kernels
            .iter()
            .map(|k| ktpu_graph::cycles(&k.graph, machine).map(|c| c - front))
            .collect::<R<Vec<_>>>()?;
        Ok(Self {
            share_cycles,
            dispatch_intercept: front,
        })
    }
}

pub trait CostModel {
    fn estimate(&self, option: &SplitOption, features: &CostFeatures) -> CostEstimate;
}

pub struct LocalTiming;

impl CostModel for LocalTiming {
    fn estimate(&self, _option: &SplitOption, features: &CostFeatures) -> CostEstimate {
        CostEstimate {
            makespan: features.share_cycles.iter().copied().max().unwrap_or(0)
                + features.dispatch_intercept,
            local_cycles: features.share_cycles.clone(),
            dispatch_intercept: features.dispatch_intercept,
            ..CostEstimate::default()
        }
    }
}

pub fn price_option(
    mut option: SplitOption,
    features: &CostFeatures,
    model: &dyn CostModel,
) -> R<SplitOption> {
    if features.share_cycles.len() != option.active_nodes
        || features.dispatch_intercept < 0
        || features.share_cycles.iter().any(|&c| c < 0)
        || features
            .share_cycles
            .iter()
            .any(|&c| c.checked_add(features.dispatch_intercept).is_none())
    {
        return fail("L2.COST.001", "invalid per-share timing features");
    }
    option.cost = model.estimate(&option, features);
    Ok(option)
}

/// Forced options are still priced; ties never depend on enumeration order.
pub fn choose_split(
    options: Vec<(SplitOption, CostFeatures)>,
    model: &dyn CostModel,
    force: Option<&str>,
) -> R<SplitOption> {
    let mut priced = Vec::with_capacity(options.len());
    let mut ids = std::collections::BTreeSet::new();
    for (option, features) in options {
        if !ids.insert(option.id.clone()) {
            return fail("DSL.KERN.020", "duplicate split option id");
        }
        priced.push(price_option(option, &features, model)?);
    }
    priced.sort_by(|a, b| {
        (a.cost.makespan, a.conversions, a.events.len(), &a.id).cmp(&(
            b.cost.makespan,
            b.conversions,
            b.events.len(),
            &b.id,
        ))
    });
    if let Some(option) = priced
        .into_iter()
        .find(|o| force.is_none_or(|id| id == o.id))
    {
        Ok(option)
    } else {
        fail("DSL.KERN.020", "no matching legal split option")
    }
}
