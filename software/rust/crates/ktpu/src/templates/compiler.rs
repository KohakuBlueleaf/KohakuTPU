//! Representative compilation and conservative replay of remembered choices.

use std::sync::Arc;

use ktpu_l2::Search;
use ktpu_machine::Machine;

use crate::{Cache, CardStack, Compiled, Input, Refused};

/// One machine and representative; no stage cache can bypass choice recording.
pub struct PinnedCompiler {
    machine: Machine,
    backend: CardStack,
    pub representative: Compiled,
    pub search: Arc<Search>,
}

impl PinnedCompiler {
    pub fn new(source: &str, machine: &Machine) -> Result<Self, Refused> {
        Self::with_rotation_parameters(source, machine, false)
    }

    pub fn with_rotation_parameters(
        source: &str,
        machine: &Machine,
        parameter_rotations: bool,
    ) -> Result<Self, Refused> {
        let search = Arc::new(Search::recording());
        let backend = CardStack {
            template_search: Some(search.clone()),
            parameter_rotations,
            ..CardStack::default()
        };
        let representative = crate::compile(
            &backend,
            &Input::detect(source),
            None,
            machine,
            &Cache::off(),
        )?;
        search.freeze();
        Ok(Self {
            machine: machine.clone(),
            backend,
            representative,
            search,
        })
    }

    /// A changed search probe refuses instead of selecting a new image.
    pub fn compile(&self, source: &str) -> Result<Compiled, Refused> {
        crate::compile(
            &self.backend,
            &Input::detect(source),
            None,
            &self.machine,
            &Cache::off(),
        )
    }
}
