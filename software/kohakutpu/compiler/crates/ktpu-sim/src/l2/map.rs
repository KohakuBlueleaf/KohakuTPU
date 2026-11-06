//! A composed read (L2.READ.001): the source element behind each logical one.

use std::collections::HashMap;
use std::rc::Rc;

use ktpu_l1::text::{fmt, Stmt, Term};

use crate::l1::term::{int, Env};
use crate::R;

fn levels(t: &Term, env: &Env) -> R<Vec<(i64, i64)>> {
    let Term::Tuple(items) = t else {
        return Err(format!("a read_map walk, got {}", fmt(t)));
    };
    items
        .iter()
        .map(|x| crate::l1::term::pair(x, env))
        .collect()
}

#[derive(Hash, PartialEq, Eq)]
struct Step {
    at: i64,
    dat: i64,
    extent: usize,
    from: Vec<(i64, i64)>,
    to: Vec<(i64, i64)>,
}

fn steps(st: &Stmt, env: &Env) -> R<Vec<Step>> {
    let Some(Term::Tuple(steps)) = st.kw("read_map") else {
        return Err("read_map is a tuple of steps".into());
    };
    steps
        .iter()
        .map(|step| {
            let Term::Tuple(f) = step else {
                return Err(format!("a read_map step, got {}", fmt(step)));
            };
            Ok(Step {
                at: int(&f[1], env)?,
                dat: int(&f[2], env)?,
                extent: int(&f[3], env)? as usize,
                from: levels(&f[4], env)?,
                to: levels(&f[5], env)?,
            })
        })
        .collect()
}

fn expand(steps: &[Step]) -> R<Vec<i64>> {
    let mut before: Option<Vec<i64>> = None;
    for step in steps {
        let mut points = vec![(step.at, step.dat)];
        for (&(count, s), &(_, d)) in step.from.iter().zip(&step.to) {
            points = points
                .iter()
                .flat_map(|&(a, b)| (0..count).map(move |i| (a + i * s, b + i * d)))
                .collect();
        }
        let mut out = vec![-1; step.extent];
        for (a, b) in points {
            out[b as usize] = before.as_ref().map_or(a, |p| p[a as usize]);
        }
        before = Some(out);
    }
    before.ok_or("an empty read_map".into())
}

/// Logical index to source index, -1 where no step wrote.
pub fn read(st: &Stmt, env: &Env) -> R<Vec<i64>> {
    expand(&steps(st, env)?)
}

/// Evaluated maps shared by loads and quantisers within one simulation.
/// Retain at most 64 maps and 16 MiB of indices, or one map larger than that.
#[derive(Default)]
pub struct Cache {
    maps: HashMap<Vec<Step>, Rc<Vec<i64>>>,
    elements: usize,
}

impl Cache {
    pub fn read(&mut self, st: &Stmt, env: &Env) -> R<Rc<Vec<i64>>> {
        let key = steps(st, env)?;
        if let Some(map) = self.maps.get(&key) {
            return Ok(Rc::clone(map));
        }
        let extent = key.last().ok_or("an empty read_map")?.extent;
        if self.maps.len() >= 64 || self.elements.saturating_add(extent) > 2 * 1024 * 1024 {
            self.maps.clear();
            self.elements = 0;
        }
        let map = Rc::new(expand(&key)?);
        self.elements += map.len();
        self.maps.insert(key, Rc::clone(&map));
        Ok(map)
    }
}
