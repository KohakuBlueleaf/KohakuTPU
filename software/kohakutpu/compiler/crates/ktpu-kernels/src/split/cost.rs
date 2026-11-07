//! Unfitted local-schedule timing; no measured speedup is implied.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;

use super::SplitOption;
use crate::trace::{fail, R};
use ktpu_machine::{Machine, Refusal};

#[derive(Clone, Debug)]
pub struct CostEstimate {
    pub makespan: i64,
    pub local_cycles: Vec<i64>,
    pub dispatch_intercept: i64,
    pub revision: String,
    pub provenance: String,
    pub uncertainty: String,
    pub measured: bool,
    pub delivery: Option<DeliveryCost>,
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
            delivery: None,
        }
    }
}

/// Emitted raw delivery demand. Bytes are transport bytes, not logical f16
/// operand sizes; every remote copy and each traversed link is counted.
#[derive(Clone, Debug, Default)]
pub struct DeliveryCost {
    pub sender_fills: u64,
    pub sender_bytes: u64,
    pub local_bytes: u64,
    pub remote_bytes: u64,
    pub route_bytes: u64,
    pub remote_payloads: u64,
    pub relay_words: u64,
    pub bank_events: u64,
    pub compute_cycles: u64,
    pub private_fill_bytes: u64,
}

/// Phase-exclusive delivery is priced from its actual images and node streams,
/// not from the independent M shards. Serializing the node budgets is an
/// unfitted upper estimate of reduced overlap; transport and bank-event costs
/// are added because the node-local timing model does not join remote clocks.
pub fn price_delivery(
    local_cycles: Vec<i64>,
    delivery: DeliveryCost,
    machine: &Machine,
) -> R<CostEstimate> {
    let word = u64::from(machine.die.word_bytes);
    let transport = delivery.route_bytes.div_ceil(word)
        + delivery.remote_payloads
        + delivery.bank_events
            * (2 * u64::from(machine.vc.word_cycles) + u64::from(machine.vc.wait_cycles))
        + delivery.relay_words
        + delivery.compute_cycles
        + delivery.private_fill_bytes.div_ceil(word);
    let transport = i64::try_from(transport)
        .map_err(|_| Refusal::new("L2.COST.001", "delivery cost exceeds cycle range"))?;
    let makespan = local_cycles
        .iter()
        .try_fold(
            machine.vc.launch_front as i64 + transport,
            |total, cycles| total.checked_add(*cycles),
        )
        .ok_or_else(|| Refusal::new("L2.COST.001", "delivery cost exceeds cycle range"))?;
    Ok(CostEstimate {
        makespan,
        local_cycles,
        dispatch_intercept: machine.vc.launch_front as i64,
        revision: "raw-b-delivery-v1".into(),
        provenance:
            "emitted VC fills/sends, serialized node budgets, mesh-hop bytes and bank events".into(),
        uncertainty:
            "unfitted serial upper estimate; link slope one word/cycle; no measured crossover"
                .into(),
        measured: false,
        delivery: Some(delivery),
    })
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

/// Exact L1 prices within one planning invocation and its fixed mutant scope.
/// Each miss starts a fresh timing clock; only complete successful prices persist.
pub(crate) struct LocalPrices {
    target: ktpu_l2::target::Target,
    l1: HashMap<(String, String), i64>,
}

impl LocalPrices {
    pub(crate) fn new(machine: &Machine) -> Self {
        Self {
            target: ktpu_l2::target::Target::of(machine),
            l1: HashMap::new(),
        }
    }

    pub(crate) fn l1(&mut self, text: String, name: &str) -> R<i64> {
        let key = (text, name.to_string());
        if let Some(&cycles) = self.l1.get(&key) {
            return Ok(cycles);
        }
        let module =
            ktpu_l1::text::read(&key.0, name).map_err(|e| Refusal::new(&e.clause, e.message))?;
        let cycles = ktpu_l2::cost::cycles(&module, name, &self.target)
            .map_err(|e| Refusal::new(&e.clause, e.message))?;
        self.l1.insert(key, cycles);
        Ok(cycles)
    }
}

/// Complete emitted node budgets, excluding the launch intercept. Compilation
/// is performed once; every local price retains the complete module context.
pub(crate) fn graph_nodes(graph: &ktpu_graph::Graph, machine: &Machine) -> R<BTreeMap<i64, i64>> {
    ktpu_graph::node_cycles(graph, machine)
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

/// Compile candidates in lower-bound order. A strict bound comparison preserves
/// all equal-price tie breakers. Missing bounds always receive a complete price.
pub fn price_bounded(
    mut candidates: Vec<(SplitOption, Option<i64>)>,
    mut price: impl FnMut(SplitOption) -> R<SplitOption>,
) -> R<Vec<SplitOption>> {
    candidates.sort_by(|(a, af), (b, bf)| (af.unwrap_or(0), &a.id).cmp(&(bf.unwrap_or(0), &b.id)));
    let mut best = i64::MAX;
    let mut priced = Vec::new();
    for (option, floor) in candidates {
        if floor.is_some_and(|floor| floor > best) {
            continue;
        }
        let option = price(option)?;
        best = best.min(option.cost.makespan);
        priced.push(option);
    }
    Ok(priced)
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
    select_split(priced, force)
}

/// Select from already priced options without lowering their shares again.
pub fn select_split(mut priced: Vec<SplitOption>, force: Option<&str>) -> R<SplitOption> {
    rank(&mut priced);
    selected(&priced, force)
}

fn rank(priced: &mut [SplitOption]) {
    priced.sort_by(|a, b| {
        (a.cost.makespan, a.conversions, a.events.len(), &a.id).cmp(&(
            b.cost.makespan,
            b.conversions,
            b.events.len(),
            &b.id,
        ))
    });
}

fn selected(priced: &[SplitOption], force: Option<&str>) -> R<SplitOption> {
    if let Some(option) = priced.iter().find(|o| force.is_none_or(|id| id == o.id)) {
        Ok(option.clone())
    } else {
        fail("DSL.KERN.020", "no matching legal split option")
    }
}

/// Process-lifetime prices. Automatic entries serve forced selections whose
/// exact price they contain. A bound-pruned option is priced when forced; a
/// forced entry never stands in for an automatic candidate comparison.
#[derive(Default)]
pub struct ChoiceCache(Mutex<HashMap<(String, Option<String>), Vec<SplitOption>>>);

impl ChoiceCache {
    pub fn choose(
        &self,
        key: String,
        force: Option<&str>,
        price: impl FnOnce() -> R<Vec<SplitOption>>,
    ) -> R<SplitOption> {
        let mut cache = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(id) = force {
            if let Some(option) = cache
                .get(&(key.clone(), None))
                .and_then(|choices| choices.iter().find(|option| option.id == id))
            {
                return Ok(option.clone());
            }
        }
        let choices = match cache.entry((key, force.map(str::to_owned))) {
            std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
            std::collections::hash_map::Entry::Vacant(entry) => {
                let mut choices = price()?;
                rank(&mut choices);
                entry.insert(choices)
            }
        };
        selected(choices, force)
    }
}
