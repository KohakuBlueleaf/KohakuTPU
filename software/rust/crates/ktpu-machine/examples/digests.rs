//! Regenerate the canonical machine digest witnesses.
use ktpu_machine::{machines_dir, names, Machine};
use std::{collections::BTreeMap, error::Error, path::PathBuf};

fn main() -> Result<(), Box<dyn Error>> {
    let digests = names(&machines_dir())
        .into_iter()
        .map(|name| Machine::named(&name).map(|machine| (name, machine.digest())))
        .collect::<Result<BTreeMap<_, _>, _>>()?;
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/machine/digests.json");
    std::fs::write(
        path,
        format!("{}\n", serde_json::to_string_pretty(&digests)?),
    )?;
    Ok(())
}
