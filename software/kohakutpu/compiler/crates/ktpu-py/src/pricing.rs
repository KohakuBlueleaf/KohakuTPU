//! Compiled prices shared by generated graph and machine, not call arguments.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use ktpu::stack::CardStack;
use ktpu::{Cache, Input};
use ktpu_kernels::call::Call;
use ktpu_machine::Machine;

use super::LruMap;

type Price = Arc<OnceLock<Option<i64>>>;
type Key = ([u8; 32], String);

fn prices() -> &'static Mutex<LruMap<Key, Price>> {
    static PRICES: OnceLock<Mutex<LruMap<Key, Price>>> = OnceLock::new();
    PRICES.get_or_init(|| Mutex::new(LruMap::new(4096)))
}

/// One admission retains each graph's price even if the process LRU evicts it.
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

    pub(super) fn cycles(&self, call: &Call) -> Option<i64> {
        let kernel = ktpu_kernels::call::call(&call.gen, &call.args, self.machine).ok()?;
        let graph = serde_json::to_string(&kernel.graph).ok()?;
        let price = {
            let mut local = self.local.borrow_mut();
            local
                .entry(graph.clone())
                .or_insert_with(|| {
                    if !self.shared {
                        return Arc::new(OnceLock::new());
                    }
                    let key = (self.digest, graph.clone());
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
        *price.get_or_init(|| {
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
}
