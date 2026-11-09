//! Finite 64-bit values and path predicates as reduced ordered decision diagrams.

use std::sync::{Arc, Mutex};

use super::fx::{FxHashMap as HashMap, FxHashSet as HashSet};

mod cache;
mod predicate;
pub(crate) use predicate::Predicate;

/// Concrete operands do not consult the symbolic arena.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[doc(hidden)]
pub enum Value {
    Known(u64),
    Symbolic(usize),
}

type Bit = usize;
type Word = [Bit; 64];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Branch {
    var: (usize, usize),
    low: Bit,
    high: Bit,
}

/// Branches retain their own predicate and share immutable node/word identities.
#[derive(Clone, Debug)]
#[doc(hidden)]
pub struct Symbolic {
    arena: Arc<Mutex<Arena>>,
    pub(crate) predicate: Predicate,
}

impl PartialEq for Symbolic {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.arena, &other.arena)
            && self.predicate.equivalent(
                &other.predicate,
                &mut self.arena.lock().expect("symbolic arena"),
            )
    }
}

impl Eq for Symbolic {}

#[derive(Debug)]
struct Arena {
    nodes: Vec<Branch>,
    unique: HashMap<Branch, Bit>,
    cache: cache::Computed,
    inputs: usize,
    words: Vec<Word>,
    word_ids: HashMap<Word, usize>,
}

impl Default for Arena {
    fn default() -> Self {
        Self {
            nodes: vec![
                Branch {
                    var: (usize::MAX, usize::MAX),
                    low: 0,
                    high: 0,
                },
                Branch {
                    var: (usize::MAX, usize::MAX),
                    low: 1,
                    high: 1,
                },
            ],
            unique: HashMap::default(),
            cache: cache::Computed::default(),
            inputs: 0,
            words: Vec::new(),
            word_ids: HashMap::default(),
        }
    }
}

impl Arena {
    /// Fresh-input support of a diagram, walking each shared node once.
    fn support(&self, root: Bit) -> Vec<usize> {
        let mut inputs = std::collections::BTreeSet::new();
        let mut seen = HashSet::default();
        let mut pending = vec![root];
        while let Some(bit) = pending.pop() {
            if bit <= 1 || !seen.insert(bit) {
                continue;
            }
            let branch = self.nodes[bit];
            inputs.insert(branch.var.1);
            pending.push(branch.low);
            pending.push(branch.high);
        }
        inputs.into_iter().collect()
    }

    pub(crate) fn storage(&self) -> (usize, usize, usize, usize) {
        (
            self.nodes.len(),
            self.unique.len(),
            self.cache.len(),
            self.words.len(),
        )
    }

    fn node(&mut self, var: (usize, usize), low: Bit, high: Bit) -> Bit {
        if low == high {
            return low;
        }
        let branch = Branch { var, low, high };
        if let Some(&id) = self.unique.get(&branch) {
            return id;
        }
        let id = self.nodes.len();
        self.nodes.push(branch);
        self.unique.insert(branch, id);
        if self.nodes.len().is_power_of_two() && std::env::var_os("KTPU_VERIFY_STATS").is_some() {
            eprintln!("verify symbolic storage: {:?}", self.storage());
        }
        id
    }

    /// `c ? yes : no`, as one diagram. The expansion runs on an explicit stack
    /// in the recursive order (low branch, high branch, node), so its depth is
    /// the variable count without a native frame per variable.
    fn select(&mut self, c: Bit, yes: Bit, no: Bit) -> Bit {
        enum Task {
            Expand(Bit, Bit, Bit),
            Join((Bit, Bit, Bit), (usize, usize)),
        }
        let mut tasks = vec![Task::Expand(c, yes, no)];
        let mut done: Vec<Bit> = Vec::new();
        while let Some(task) = tasks.pop() {
            match task {
                Task::Expand(c, yes, no) => {
                    if c == 1 || yes == no {
                        done.push(yes);
                        continue;
                    }
                    if c == 0 {
                        done.push(no);
                        continue;
                    }
                    if yes == 1 && no == 0 {
                        done.push(c);
                        continue;
                    }
                    let key = (c, yes, no);
                    if let Some(&id) = self.cache.get(&key) {
                        done.push(id);
                        continue;
                    }
                    let var = [c, yes, no]
                        .map(|id| self.nodes[id].var)
                        .into_iter()
                        .min()
                        .expect("three decision operands");
                    let split = |id: Bit| {
                        let n = self.nodes[id];
                        if n.var == var {
                            (n.low, n.high)
                        } else {
                            (id, id)
                        }
                    };
                    let (cl, ch) = split(c);
                    let (yl, yh) = split(yes);
                    let (nl, nh) = split(no);
                    tasks.push(Task::Join(key, var));
                    tasks.push(Task::Expand(ch, yh, nh));
                    tasks.push(Task::Expand(cl, yl, nl));
                }
                Task::Join(key, var) => {
                    let high = done.pop().expect("high branch");
                    let low = done.pop().expect("low branch");
                    let id = self.node(var, low, high);
                    self.cache.insert(key, id);
                    done.push(id);
                }
            }
        }
        done.pop().expect("one diagram")
    }

    fn not(&mut self, a: Bit) -> Bit {
        self.select(a, 0, 1)
    }

    fn and(&mut self, a: Bit, b: Bit) -> Bit {
        self.select(a, b, 0)
    }

    fn xor(&mut self, a: Bit, b: Bit) -> Bit {
        let nb = self.not(b);
        self.select(a, nb, b)
    }

    fn bits(&self, value: Value) -> Word {
        match value {
            Value::Known(v) => std::array::from_fn(|i| ((v >> i) & 1) as Bit),
            Value::Symbolic(id) => self.words[id],
        }
    }

    fn value(&mut self, bits: Word) -> Value {
        if bits.iter().all(|&b| b <= 1) {
            return Value::Known(
                bits.iter()
                    .enumerate()
                    .fold(0, |v, (i, &b)| v | (b as u64) << i),
            );
        }
        if let Some(&id) = self.word_ids.get(&bits) {
            return Value::Symbolic(id);
        }
        let id = self.words.len();
        self.words.push(bits);
        self.word_ids.insert(bits, id);
        Value::Symbolic(id)
    }

    /// Bit-major ordering interleaves independent inputs for word comparisons.
    pub fn fresh(&mut self) -> Value {
        let input = self.inputs;
        self.inputs += 1;
        let bits = std::array::from_fn(|bit| self.node((bit, input), 0, 1));
        self.value(bits)
    }

    fn mux(&mut self, c: Bit, a: Word, b: Word) -> Word {
        std::array::from_fn(|i| self.select(c, a[i], b[i]))
    }

    fn add(&mut self, a: Word, b: Word, mut carry: Bit) -> Word {
        std::array::from_fn(|i| {
            let different = self.xor(a[i], b[i]);
            let bit = self.xor(different, carry);
            carry = self.select(different, carry, a[i]);
            bit
        })
    }

    fn less(&mut self, a: Word, b: Word) -> Bit {
        let mut less = 0;
        for i in 0..64 {
            let different = self.xor(a[i], b[i]);
            less = self.select(different, b[i], less);
        }
        less
    }

    pub fn condition(&mut self, c: u32, a: Value, b: Value) -> Bit {
        let mut a = self.bits(a);
        let mut b = self.bits(b);
        match c {
            1 | 2 => {
                let mut equal = 1;
                for i in 0..64 {
                    let different = self.xor(a[i], b[i]);
                    let same = self.not(different);
                    equal = self.and(equal, same);
                }
                if c == 1 {
                    equal
                } else {
                    self.not(equal)
                }
            }
            3..=6 => {
                if c >= 5 {
                    a[63] = self.not(a[63]);
                    b[63] = self.not(b[63]);
                }
                let less = self.less(a, b);
                if c == 3 || c == 5 {
                    less
                } else {
                    self.not(less)
                }
            }
            _ => 0,
        }
    }

    pub fn alu(&mut self, op: u32, a: Value, b: Value) -> Option<Value> {
        if op == 0 {
            return Some(b);
        }
        let a = self.bits(a);
        let b = self.bits(b);
        let bits = match op {
            1 => self.add(a, b, 0),
            2 => {
                let b = b.map(|bit| self.not(bit));
                self.add(a, b, 1)
            }
            3 => {
                let mut product = [0; 64];
                for shift in 0..64 {
                    let row = std::array::from_fn(|i| {
                        if i < shift {
                            0
                        } else {
                            self.and(a[i - shift], b[shift])
                        }
                    });
                    product = self.add(product, row, 0);
                }
                product
            }
            4 => std::array::from_fn(|i| self.and(a[i], b[i])),
            5 => std::array::from_fn(|i| self.select(a[i], 1, b[i])),
            6 => std::array::from_fn(|i| self.xor(a[i], b[i])),
            7 | 8 => {
                let mut shifted = a;
                for (k, &bit) in b[..6].iter().enumerate() {
                    let distance = 1 << k;
                    let next = std::array::from_fn(|i: usize| {
                        let source = if op == 7 {
                            i.checked_sub(distance)
                        } else {
                            i.checked_add(distance)
                        };
                        source.filter(|&j| j < 64).map_or(0, |j| shifted[j])
                    });
                    shifted = self.mux(bit, next, shifted);
                }
                shifted
            }
            9 | 10 => {
                let less = self.less(a, b);
                if op == 9 {
                    self.mux(less, a, b)
                } else {
                    self.mux(less, b, a)
                }
            }
            11 | 12 => {
                let mut remainder = [0; 64];
                let mut quotient = [0; 64];
                let negative = b.map(|bit| self.not(bit));
                for i in (0..64).rev() {
                    let overflow = remainder[63];
                    remainder =
                        std::array::from_fn(|j| if j == 0 { a[i] } else { remainder[j - 1] });
                    let less = self.less(remainder, b);
                    let ge = self.not(less);
                    let subtract = self.select(overflow, 1, ge);
                    let difference = self.add(remainder, negative, 1);
                    remainder = self.mux(subtract, difference, remainder);
                    quotient[i] = subtract;
                }
                if op == 11 {
                    quotient
                } else {
                    remainder
                }
            }
            _ => return None,
        };
        Some(self.value(bits))
    }
}

impl Default for Symbolic {
    fn default() -> Self {
        Self {
            arena: Arc::new(Mutex::new(Arena::default())),
            predicate: Predicate::default(),
        }
    }
}

impl Symbolic {
    /// Immutable nodes, unique nodes, computed entries, and interned words.
    /// This snapshot does not allocate or change the path predicate.
    #[doc(hidden)]
    pub fn storage(&self) -> (usize, usize, usize, usize) {
        self.arena.lock().expect("symbolic arena").storage()
    }

    pub fn fresh(&mut self) -> Value {
        self.arena.lock().expect("symbolic arena").fresh()
    }

    pub fn condition(&mut self, c: u32, a: Value, b: Value) -> Bit {
        self.arena
            .lock()
            .expect("symbolic arena")
            .condition(c, a, b)
    }

    /// Restrict only the continuing path; assertions and summarized polls do
    /// not consume a failing/fallthrough successor.
    pub fn assume(&mut self, condition: Bit) {
        self.predicate.restrict(
            &mut self.arena.lock().expect("symbolic arena"),
            condition,
        );
    }

    /// A failing assertion terminates with its incoming path identity intact.
    pub fn try_assume(&mut self, condition: Bit) -> bool {
        self.predicate.try_restrict(
            &mut self.arena.lock().expect("symbolic arena"),
            condition,
        )
    }

    /// Both predicates include the entire incoming path condition. Cloning is
    /// needed only at an actual branch, not at each successful assertion.
    pub(crate) fn successors(&mut self, condition: Bit) -> (Predicate, Predicate) {
        let mut arena = self.arena.lock().expect("symbolic arena");
        let mut taken = self.predicate.clone();
        taken.restrict(&mut arena, condition);
        let not = arena.not(condition);
        let mut fallthrough = self.predicate.clone();
        fallthrough.restrict(&mut arena, not);
        (taken, fallthrough)
    }

    pub fn alu(&mut self, op: u32, a: Value, b: Value) -> Option<Value> {
        self.arena.lock().expect("symbolic arena").alu(op, a, b)
    }
}
