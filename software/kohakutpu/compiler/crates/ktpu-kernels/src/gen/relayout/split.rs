//! Physical destination ownership for table-independent relayout word walks.

use super::{Part, Plan, Words};
use crate::gen::convert;
use crate::pyrepr::{name, Py};
use crate::split::{PlannedKernel, SplitRequest};
use crate::trace::{fail, R};
use ktpu_machine::Machine;

/// A single direct word walk has no indexed destinations or ordered merge layers.
pub(crate) fn independent_words(plan: &Plan) -> Option<&Words> {
    if !plan.scratch.is_empty() {
        return None;
    }
    match plan.parts.as_slice() {
        [Part::Words(words)] if words.src == "src" && words.dst == "dst" => Some(words),
        _ => None,
    }
}

pub(crate) fn kernel(
    plan: &Plan,
    words: &Words,
    mut original: PlannedKernel,
    request: &SplitRequest,
    machine: &Machine,
) -> R<PlannedKernel> {
    let word = i64::from(machine.die.word_bytes);
    let mut distributed_request = request.clone();
    distributed_request.force = Some("destination-tiles".into());
    let candidate = convert::destination::walk(
        plan.src_words * word,
        &words.levels,
        words.at,
        plan.dst_words * word,
        &words.dlevels,
        words.dat,
        None,
        &distributed_request,
        machine,
    )?;
    original.request = request.clone();
    let Some(mut distributed) = candidate else {
        return if request.force.is_some() {
            fail("DSL.KERN.020", "no matching legal destination ownership")
        } else {
            Ok(original)
        };
    };
    // Index and mask remain public, unused inputs. Their contents do not select
    // any source or destination in this plan, so their bindings remain unrestricted.
    distributed.kernel.graph.ports = original.kernel.graph.ports.clone();
    distributed.kernel.given = original.kernel.given.clone();
    distributed.kernel.graph.name = name(
        "layout_shares",
        vec![
            Py::str(&original.kernel.graph.name),
            Py::ints(request.nodes.ids()),
            Py::str(&distributed.split.id),
        ],
    );
    distributed.request = request.clone();
    match request.force.as_deref() {
        Some("destination-tiles") => Ok(distributed),
        Some(_) => fail("DSL.KERN.020", "no matching legal destination ownership"),
        None if distributed.split.cost.makespan < original.split.cost.makespan => Ok(distributed),
        None => Ok(original),
    }
}
