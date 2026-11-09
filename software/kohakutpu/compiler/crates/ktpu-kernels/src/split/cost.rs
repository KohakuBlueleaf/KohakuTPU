//! Unfitted local-schedule timing; no measured speedup is implied.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;

use super::SplitOption;
use crate::trace::{fail, R};
use ktpu_machine::{Machine, Refusal};

/// Lowering and capacity refusals are recorded in the candidate comparison.
#[derive(Default)]
pub(crate) struct Admission {
    refused: Vec<(String, Refusal)>,
}

impl Admission {
    pub(crate) fn consider<T>(&mut self, id: &str, result: R<T>) -> R<Option<T>> {
        match result {
            Ok(value) => Ok(Some(value)),
            Err(error)
                if error.clause == "L2.COST.001"
                    || error.clause == "DSL.KERN.020"
                        && error.message.starts_with("staging capacity:") =>
            {
                self.refused.push((id.into(), error));
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    pub(crate) fn finish(
        &self,
        mut options: Vec<SplitOption>,
        force: Option<&str>,
    ) -> R<Vec<SplitOption>> {
        let forced_refusal = force.is_some_and(|id| self.refused.iter().any(|(n, _)| n == id));
        if (options.is_empty() || forced_refusal) && !self.refused.is_empty() {
            let context = if self
                .refused
                .iter()
                .all(|(_, error)| error.clause == "DSL.KERN.020")
            {
                "staging capacity: no fitting requested option"
            } else {
                "no legal requested option"
            };
            return fail(
                "DSL.KERN.020",
                format!(
                    "{context}; {}",
                    self.refused
                        .iter()
                        .map(|(id, reason)| format!("{id}: {reason}"))
                        .collect::<Vec<_>>()
                        .join("; ")
                ),
            );
        }
        for option in &mut options {
            option.legality.clauses.extend(
                self.refused
                    .iter()
                    .map(|(id, reason)| format!("refused {id}: {reason}")),
            );
        }
        Ok(options)
    }
}

/// Hand L1 options use the same arena layout and capacity rule as graph options.
pub(crate) fn l1_staging(
    module: &ktpu_l1::text::Module,
    name: &str,
    machine: &Machine,
    budget: ktpu_machine::StagingBudget,
) -> R<()> {
    ktpu_l1::layout::scratch::admit_staging(
        module,
        name,
        &std::collections::BTreeSet::new(),
        machine,
        budget,
    )
    .map_err(|e| Refusal::new(&e.clause, e.message))
}

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
pub(crate) fn graph_nodes(
    graph: &ktpu_graph::Graph,
    machine: &Machine,
    budget: ktpu_machine::StagingBudget,
) -> R<BTreeMap<i64, i64>> {
    let cycles = ktpu_graph::node_cycles(graph, machine)?;
    ktpu_graph::check_staging(graph, machine, budget)?;
    Ok(cycles)
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
    let mut admission = Admission::default();
    let mut best = i64::MAX;
    let mut priced = Vec::new();
    for (option, floor) in candidates {
        if floor.is_some_and(|floor| floor > best) {
            continue;
        }
        let id = option.id.clone();
        let Some(option) = admission.consider(&id, price(option))? else {
            continue;
        };
        best = best.min(option.cost.makespan);
        priced.push(option);
    }
    admission.finish(priced, None)
}

/// Price in the ordinary lower-bound order, admitting only the price winner.
/// A refused winner releases its provisional bound: deferred candidates remain
/// available until a fitting winner dominates them. Exact ties use `rank`.
pub fn price_bounded_admitted(
    mut candidates: Vec<(SplitOption, Option<i64>)>,
    mut price: impl FnMut(SplitOption) -> R<SplitOption>,
    mut admit: impl FnMut(&SplitOption) -> R<()>,
) -> R<Vec<SplitOption>> {
    candidates.sort_by(|(a, af), (b, bf)| (af.unwrap_or(0), &a.id).cmp(&(bf.unwrap_or(0), &b.id)));
    let mut pending = candidates.into_iter().peekable();
    let mut priced = Vec::<SplitOption>::new();
    let mut admission = Admission::default();
    loop {
        rank(&mut priced);
        let best = priced.first().map(|option| option.cost.makespan);
        if pending.peek().is_some_and(|(_, floor)| {
            best.is_none_or(|best| floor.is_none_or(|floor| floor <= best))
        }) {
            if let Some((option, _)) = pending.next() {
                let id = option.id.clone();
                if let Some(option) = admission.consider(&id, price(option))? {
                    priced.push(option);
                }
            }
            continue;
        }
        if priced.is_empty() {
            return admission.finish(Vec::new(), None);
        }
        let option = priced.remove(0);
        if admission.consider(&option.id, admit(&option))?.is_some() {
            return admission.finish(vec![option], None);
        }
    }
}

/// The exact cost/tie order is also the capacity-admission order.
pub fn select_admitted(
    mut priced: Vec<SplitOption>,
    force: Option<&str>,
    mut admit: impl FnMut(&SplitOption) -> R<()>,
) -> R<SplitOption> {
    rank(&mut priced);
    let mut admission = Admission::default();
    for option in priced {
        if force.is_some_and(|id| id != option.id) {
            continue;
        }
        if admission.consider(&option.id, admit(&option))?.is_some() {
            return select_split(admission.finish(vec![option], force)?, force);
        }
    }
    select_split(admission.finish(Vec::new(), force)?, force)
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
pub struct ChoiceCache(Mutex<HashMap<String, ChoicePrices>>);

#[derive(Default)]
struct ChoicePrices {
    comparisons: HashMap<Option<String>, String>,
    exact: HashMap<String, SplitOption>,
    admitted: HashSet<String>,
}

impl ChoiceCache {
    /// Exact prices and admission proofs have distinct cache identities. A losing
    /// price may later become a forced winner and must then undergo admission.
    pub fn choose_bounded(
        &self,
        key: String,
        force: Option<&str>,
        candidates: impl FnOnce() -> R<Vec<(SplitOption, Option<i64>)>>,
        price: impl FnMut(SplitOption) -> R<SplitOption>,
        admit: impl FnMut(&SplitOption) -> R<()>,
    ) -> R<SplitOption> {
        self.choose_bounded_with_prices(key, force, |_| candidates(), price, admit)
    }

    /// Seed invocation-local schedule equivalences from exact cached prices
    /// before pricing misses. Prices remain separate from admission proofs;
    /// the chosen identity is still admitted.
    pub fn choose_bounded_with_prices(
        &self,
        key: String,
        force: Option<&str>,
        candidates: impl FnOnce(&HashMap<String, SplitOption>) -> R<Vec<(SplitOption, Option<i64>)>>,
        mut price: impl FnMut(SplitOption) -> R<SplitOption>,
        mut admit: impl FnMut(&SplitOption) -> R<()>,
    ) -> R<SplitOption> {
        let mut cache = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let entry = cache.entry(key).or_default();
        if let Some(id) = force.filter(|id| entry.admitted.contains(*id)) {
            return Ok(entry.exact[id].clone());
        }
        let forced = force.map(str::to_owned);
        if let Some(id) = entry.comparisons.get(&forced) {
            return Ok(entry.exact[id].clone());
        }
        let ChoicePrices {
            comparisons,
            exact,
            admitted,
        } = entry;
        let choices = price_bounded_admitted(
            candidates(exact)?
                .into_iter()
                .filter(|(option, _)| force.is_none_or(|id| id == option.id))
                .collect(),
            |option| {
                if let Some(priced) = exact.get(&option.id) {
                    return Ok(priced.clone());
                }
                let priced = price(option)?;
                exact.insert(priced.id.clone(), priced.clone());
                Ok(priced)
            },
            |option| {
                if !admitted.contains(&option.id) {
                    admit(option)?;
                    admitted.insert(option.id.clone());
                }
                Ok(())
            },
        )?;
        let chosen = select_split(choices, force)?;
        exact.insert(chosen.id.clone(), chosen.clone());
        comparisons.insert(forced, chosen.id.clone());
        Ok(chosen)
    }

    pub fn choose(
        &self,
        key: String,
        force: Option<&str>,
        price: impl FnOnce() -> R<Vec<SplitOption>>,
    ) -> R<SplitOption> {
        self.choose_priced(key, force, |_| price())
    }

    /// Reuse exact candidates without treating a forced price as a comparison.
    /// The caller applies its usual bounds and tie order before requesting misses.
    pub fn choose_priced(
        &self,
        key: String,
        force: Option<&str>,
        price: impl FnOnce(&HashMap<String, SplitOption>) -> R<Vec<SplitOption>>,
    ) -> R<SplitOption> {
        let mut cache = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let entry = cache.entry(key).or_default();
        if let Some(id) = force {
            if let Some(option) = entry.exact.get(id) {
                return Ok(option.clone());
            }
        }
        let forced = force.map(str::to_owned);
        if let Some(id) = entry.comparisons.get(&forced) {
            return Ok(entry.exact[id].clone());
        }
        let mut choices = price(&entry.exact)?;
        rank(&mut choices);
        let chosen = selected(&choices, force)?;
        for option in choices {
            entry.exact.insert(option.id.clone(), option);
        }
        entry.comparisons.insert(forced, chosen.id.clone());
        Ok(chosen)
    }
}
