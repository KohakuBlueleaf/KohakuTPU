//! Compiled prices shared by generated graph or split request and machine.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use ktpu::stack::CardStack;
use ktpu::{Cache, Input};
use ktpu_kernels::call::Call;
use ktpu_kernels::split::{PlannedKernel, SplitRequest};
use ktpu_machine::{Machine, Refusal};

use super::LruMap;

type Price = Arc<OnceLock<Option<i64>>>;
type Key = ([u8; 32], String);

fn prices() -> &'static Mutex<LruMap<Key, Price>> {
    static PRICES: OnceLock<Mutex<LruMap<Key, Price>>> = OnceLock::new();
    PRICES.get_or_init(|| Mutex::new(LruMap::new(4096)))
}

type Plan = Arc<OnceLock<Result<PlannedKernel, Refusal>>>;
type PlanKey = ([u8; 32], String, bool);

static PLANNED: AtomicU64 = AtomicU64::new(0);
static REUSED: AtomicU64 = AtomicU64::new(0);

fn plans() -> &'static Mutex<LruMap<PlanKey, Plan>> {
    static PLANS: OnceLock<Mutex<LruMap<PlanKey, Plan>>> = OnceLock::new();
    PLANS.get_or_init(|| Mutex::new(LruMap::new(128)))
}

pub(super) fn plan_counts() -> (u64, u64) {
    (
        PLANNED.load(Ordering::Relaxed),
        REUSED.load(Ordering::Relaxed),
    )
}

/// A successful timed plan also supplies emission; an untimed result cannot
/// supply admission. Planning occurs outside the cache lock and once per entry.
pub(super) fn planned(
    generator: &str,
    args: &serde_json::Value,
    request: &SplitRequest,
    machine: &Machine,
    timed: bool,
) -> Result<PlannedKernel, Refusal> {
    let compute = || {
        PLANNED.fetch_add(1, Ordering::Relaxed);
        ktpu_kernels::call::call_split_with_timing(generator, args, request, machine, timed)
    };
    if ktpu::mutant::active().is_some() {
        return compute();
    }
    let identity = serde_json::to_string(&(generator, args, format!("{request:?}")))
        .map_err(|e| Refusal::new("API.OP.001", e.to_string()))?;
    let digest = machine.digest_bytes();
    let plan = {
        let mut cache = plans().lock().unwrap_or_else(|e| e.into_inner());
        if !timed {
            if let Some(plan) = cache.get(&(digest, identity.clone(), true)) {
                if let Some(Ok(planned)) = plan.get() {
                    REUSED.fetch_add(1, Ordering::Relaxed);
                    return Ok(planned.clone());
                }
            }
        }
        let key = (digest, identity, timed);
        if let Some(plan) = cache.get(&key) {
            REUSED.fetch_add(1, Ordering::Relaxed);
            plan
        } else {
            let plan = Arc::new(OnceLock::new());
            cache.insert(key, Arc::clone(&plan));
            plan
        }
    };
    plan.get_or_init(compute).clone()
}

/// One admission retains each price even if the process LRU evicts it.
/// Mutant runs use only admission-local entries.
pub(super) struct Prices<'a> {
    machine: &'a Machine,
    root: &'a Cache,
    digest: [u8; 32],
    shared: bool,
    local: RefCell<HashMap<String, Price>>,
}

impl<'a> Prices<'a> {
    pub(super) fn new(machine: &'a Machine, root: &'a Cache) -> Self {
        Self {
            machine,
            root,
            digest: machine.digest_bytes(),
            shared: ktpu::mutant::active().is_none(),
            local: RefCell::new(HashMap::new()),
        }
    }

    fn price(&self, identity: String, compute: impl FnOnce() -> Option<i64>) -> Option<i64> {
        let price = {
            let mut local = self.local.borrow_mut();
            local
                .entry(identity.clone())
                .or_insert_with(|| {
                    if !self.shared {
                        return Arc::new(OnceLock::new());
                    }
                    let key = (self.digest, identity);
                    let mut held = prices().lock().unwrap_or_else(|e| e.into_inner());
                    if let Some(price) = held.get(&key) {
                        return price;
                    }
                    let price = Arc::new(OnceLock::new());
                    held.insert(key, Arc::clone(&price));
                    price
                })
                .clone()
        };
        *price.get_or_init(compute)
    }

    pub(super) fn cycles(&self, call: &Call) -> Option<i64> {
        let kernel = ktpu_kernels::call::call(&call.gen, &call.args, self.machine).ok()?;
        let graph = serde_json::to_string(&kernel.graph).ok()?;
        self.price(graph.clone(), || {
            let source = Input::detect(&graph);
            let name = kernel.graph.name.as_str();
            let l1 = ktpu::compile_l1(
                &CardStack::default(),
                &source,
                Some(name),
                self.machine,
                self.root,
            )
            .ok()?;
            let module = ktpu_l1::text::read(&l1, "<l1>").ok()?;
            ktpu_l2::cost::cycles(&module, name, &ktpu_l2::target::Target::of(self.machine)).ok()
        })
    }

    /// The complete planner runs once per effective call and request, including
    /// failed admissions. Sequence membership does not change a launch's price.
    pub(super) fn split_cycles(&self, call: &Call, request: &SplitRequest) -> Option<i64> {
        let identity = serde_json::to_string(&(call, format!("{request:?}"))).ok()?;
        self.price(identity, || {
            planned(&call.gen, &call.args, request, self.machine, true)
                .ok()
                .map(|planned| planned.split.cost.makespan)
        })
    }
}
