//! Finite 64-bit values and path predicates as reduced ordered decision diagrams.

use std::sync::{Arc, Mutex};

use super::fx::FxHashMap as HashMap;

/// Concrete operands do not consult the symbolic arena.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Value {
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
pub(crate) struct Symbolic {
    arena: Arc<Mutex<Arena>>,
    pub predicate: Bit,
}

impl PartialEq for Symbolic {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.arena, &other.arena) && self.predicate == other.predicate
    }
}

impl Eq for Symbolic {}

#[derive(Debug)]
struct Arena {
    nodes: Vec<Branch>,
    unique: HashMap<Branch, Bit>,
    cache: HashMap<(Bit, Bit, Bit), Bit>,
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
            cache: HashMap::default(),
            inputs: 0,
            words: Vec::new(),
            word_ids: HashMap::default(),
        }
    }
}

impl Arena {
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

    fn select(&mut self, c: Bit, yes: Bit, no: Bit) -> Bit {
        if c == 1 || yes == no {
            return yes;
        }
        if c == 0 {
            return no;
        }
        if yes == 1 && no == 0 {
            return c;
        }
        let key = (c, yes, no);
        if let Some(&id) = self.cache.get(&key) {
            return id;
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
        let low = self.select(cl, yl, nl);
        let high = self.select(ch, yh, nh);
        let id = self.node(var, low, high);
        self.cache.insert(key, id);
        id
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
            predicate: 1,
        }
    }
}

impl Symbolic {
    pub(crate) fn storage(&self) -> (usize, usize, usize, usize) {
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

    /// Both roots include the entire incoming path condition.
    pub fn successors(&mut self, condition: Bit) -> (Bit, Bit) {
        let mut arena = self.arena.lock().expect("symbolic arena");
        let taken = arena.and(self.predicate, condition);
        let not = arena.not(condition);
        let fallthrough = arena.and(self.predicate, not);
        (taken, fallthrough)
    }

    pub fn alu(&mut self, op: u32, a: Value, b: Value) -> Option<Value> {
        self.arena.lock().expect("symbolic arena").alu(op, a, b)
    }
}
