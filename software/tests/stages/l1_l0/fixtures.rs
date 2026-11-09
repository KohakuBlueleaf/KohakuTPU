//! L1 to L0: scheduled single-node fixtures and encoded peer-transfer witnesses.
//! The peer witnesses execute concurrent pre-engine packages.

mod admission;
mod attention_flash;
mod attention_flashd;
mod attention_srflash;
mod b_pass;
mod branch_certification;
mod branch_ordering;
#[path = "../common.rs"]
mod common;
mod descriptor_initialization;
mod drain_bursts;
mod f014;
mod feedback;
mod global_dram;
mod long_remote;
mod mark_frontiers;
mod package;
mod periodic_work;
mod probe_memory;
mod probe_placement;
mod symbolic_predicates;
mod tiled_readback;

use std::collections::BTreeMap;
use std::fmt::Write;

use common::{binds_of, fixtures, known, stage_suite, Fixture, Level, Pair};
use ktpu::{Backend, CardStack};
use ktpu_isa::Machine as Card;
use ktpu_l0::Facts;
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::read;

fn encoded(f: &Fixture, card: &Card) -> Result<Pair, String> {
    let s = CardStack::default();
    let l2 = s.parse_l2(&f.source).map_err(|e| e.to_string())?;
    let placed = s.passes(&l2, &f.name, card).map_err(|e| e.to_string())?;
    let l1 = s.lower(&placed, &f.name, card).map_err(|e| e.to_string())?;
    let done = s.schedule(&l1, card).map_err(|e| e.to_string())?;
    let text = s.print_l1(&done);
    let module = read(&text, "scheduled").map_err(|e| e.to_string())?;
    let binds: BTreeMap<String, i128> = binds_of(&module, &f.name)?
        .into_iter()
        .map(|(k, v)| (k, v as i128))
        .collect();
    let nps = programs(&module, &f.name, &binds, card, Some(1), None).map_err(|e| e.to_string())?;
    let builder = Builder::new(card).map_err(|e| e.to_string())?;
    let mut cores = Cores::new(card).map_err(|e| e.to_string())?;
    let mut hex = String::new();
    for np in &nps {
        let facts = Facts::of_mesh(card, np.mesh).map_err(|e| e.to_string())?;
        let fetch = Some((card.die.fetch_port.x(), card.die.fetch_port.y()));
        let opts = Opts {
            fetch,
            cores: Some(&mut cores),
            relocate: false,
        };
        let (pkg, _) = builder.package(np, opts).map_err(|e| e.to_string())?;
        write!(hex, "{} ", np.mesh).expect("a string");
        for b in pkg.to_bytes(&facts).map_err(|e| e.to_string())? {
            write!(hex, "{b:02x}").expect("a string");
        }
        hex.push('\n');
    }
    Ok(Pair {
        name: f.name.clone(),
        before: (Level::L1, text),
        after: (Level::L0, hex),
    })
}

fn one_node(f: &Fixture) -> bool {
    !f.source.contains("on node[")
}

#[test]
fn every_package_keeps_its_fixture() {
    let corpus: Vec<Fixture> = fixtures(
        &[
            "kohakutpu/language/kohakutpu/language/examples/l2",
            "tests/fixtures/l2",
        ],
        Level::L2,
    )
    .into_iter()
    .filter(one_node)
    .collect();
    let stages = [
        "l1.lower",
        "l1.img",
        "l1.node",
        "l1.mg",
        "l1.mov",
        "l1.layout",
        "l0.",
    ];
    stage_suite(&corpus, encoded, &stages, &known(include_str!("known.txt")));
}
