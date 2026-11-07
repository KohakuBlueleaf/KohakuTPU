//! Repeated sends advance each descriptor independently of image-loop trips.

use ktpu_l1::text::{Env, Term};

use crate::ir::{const_of, value, Body, Par, Result};
use crate::lower::sync::Access;

pub(super) fn accesses(
    body: &Body,
    par: &Par,
    env: &Env,
    mut accesses: Vec<Access>,
) -> Result<Vec<Access>> {
    if let Some(count) = par.st.kw("repeats") {
        let count = const_of(body, &par.st, count, env)?;
        let advances = par.st.kw("advances").ok_or_else(|| {
            body.reader.fail(
                &par.st,
                "L2.VC.018",
                "repeated par requires descriptor advances",
            )
        })?;
        let Term::Tuple(steps) = value(body, &par.st, advances, env)? else {
            return Err(body.reader.fail(
                &par.st,
                "L2.VC.018",
                "descriptor advances are a tuple of steps",
            ));
        };
        if count < 1 {
            return Err(body.reader.fail(
                &par.st,
                "L2.VC.018",
                "repeated par requires a positive count",
            ));
        }
        for access in &mut accesses {
            let Some(descriptor) = access.descriptor else {
                continue;
            };
            let step = steps.get(descriptor).ok_or_else(|| {
                body.reader.fail(
                    &par.st,
                    "L2.VC.018",
                    format!("descriptor {descriptor} has no advance"),
                )
            })?;
            let step = const_of(body, &par.st, step, env)?;
            *access = access.repeated(count, step).ok_or_else(|| {
                body.reader
                    .fail(&par.st, "L2.ORDER.009", "repeated access overflow")
            })?;
        }
    }
    for access in &mut accesses {
        access.descriptor = None;
    }
    Ok(accesses)
}
