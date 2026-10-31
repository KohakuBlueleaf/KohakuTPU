//! A composed read (L2.READ.001): the source element behind each logical one.

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

/// Logical index to source index, -1 where no step wrote.
pub fn read(st: &Stmt, env: &Env) -> R<Vec<i64>> {
    let Some(Term::Tuple(steps)) = st.kw("read_map") else {
        return Err("read_map is a tuple of steps".into());
    };
    let mut before: Option<Vec<i64>> = None;
    for step in steps {
        let Term::Tuple(f) = step else {
            return Err(format!("a read_map step, got {}", fmt(step)));
        };
        let at = int(&f[1], env)?;
        let dat = int(&f[2], env)?;
        let extent = int(&f[3], env)? as usize;
        let (from, to) = (levels(&f[4], env)?, levels(&f[5], env)?);
        let mut points = vec![(at, dat)];
        for (&(count, s), &(_, d)) in from.iter().zip(&to) {
            points = points
                .iter()
                .flat_map(|&(a, b)| (0..count).map(move |i| (a + i * s, b + i * d)))
                .collect();
        }
        let mut out = vec![-1; extent];
        for (a, b) in points {
            out[b as usize] = before.as_ref().map_or(a, |p| p[a as usize]);
        }
        before = Some(out);
    }
    before.ok_or("an empty read_map".into())
}
