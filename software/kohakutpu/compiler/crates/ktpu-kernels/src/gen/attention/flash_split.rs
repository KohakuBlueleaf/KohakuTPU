//! Ownership and executable L1 for the hand attention motifs.

use std::collections::BTreeMap;

use ktpu_graph as g;
use ktpu_l1::text::read;
use ktpu_machine::{Machine, Refusal, UnitKind};

use super::flash::{Origins, Share};
use super::motif::AttentionMotif;
use super::{assignments, AttentionMask, AttentionPartition, AttentionPrecision, AttentionSpec};
use crate::kernel::{layout, Kernel, Port};
use crate::split::{
    choose_split, validate_ownership, CostEstimate, CostFeatures, LegalityWitness, LocalTiming,
    OutputLayout, PlannedKernel, Region, ShardRole, ShardTiles, SplitAxis, SplitOption,
    SplitRequest,
};
use crate::trace::{Facts, R};

fn region(allocation: &str, start: i64, end: i64) -> Region {
    Region {
        allocation: allocation.into(),
        bytes: start as u64..end as u64,
    }
}

fn signature(heads: i64, motif: AttentionMotif, m: &Machine) -> R<Kernel> {
    let pack = motif.pack();
    let mut y = Port::output("y", &[heads * 1024, 64], g::Dtype::F16);
    let row_tile = if motif == AttentionMotif::Srflash {
        1
    } else {
        8
    };
    y.layout = vec![("tile".into(), g::LayoutValue::Ints(vec![row_tile, 16]))];
    let mut ports = vec![
        Port::input("q", &[heads * 1024, 64], g::Dtype::Mx7a)
            .with_layout(layout(&[("gm", 32), ("nk", pack.qk_nk as i64)])),
        Port::input("k", &[heads * 1024, 64], g::Dtype::Mx7b)
            .with_layout(layout(&[("gn", 128), ("nk", pack.qk_nk as i64)])),
        Port::input("v", &[64, heads * 1024], g::Dtype::Mx7b).with_layout(layout(&[
            ("gn", pack.v_gn as i64),
            ("nk", pack.v_nk as i64),
            ("t", 1),
        ])),
        y,
    ];
    if motif == AttentionMotif::Srflash {
        ports.push(Port::input("tab", &[256, 8], g::Dtype::F32));
    }
    let given: Vec<(String, g::Spec)> = ports
        .iter()
        .map(|p| {
            (
                p.name.clone(),
                g::Spec {
                    dtype: p.dtype,
                    shape: p.dims.clone(),
                    layout: p.layout.clone(),
                    space: g::BufSpace::Dram,
                },
            )
        })
        .collect();
    let facts = Facts::of(m);
    Ok(Kernel {
        graph: g::Graph {
            format: g::FORMAT.into(),
            version: g::FORMAT_VERSION,
            name: motif.name().into(),
            target: m.die.target.clone(),
            machine: g::DslMachine {
                mg: facts.mg as u32,
                vc: facts.vc as u32,
                nodes: facts.nodes as u32,
            },
            consts: serde_json::json!({}),
            ports: ports
                .iter()
                .zip(&given)
                .map(|(p, (_, spec))| g::Port {
                    name: p.name.clone(),
                    role: p.role,
                    spec: spec.clone(),
                })
                .collect(),
            scratch: vec![],
            body: vec![],
        },
        given,
    })
}

/// Rename lexical identifiers, including macro references and mark families.
fn namespace(text: &str, tag: usize) -> String {
    let mut names = BTreeMap::new();
    for line in text.lines() {
        if line.starts_with("image ") || line.starts_with("macro ") {
            if let Some(name) = line
                .split_whitespace()
                .nth(1)
                .and_then(|s| s.split('(').next())
            {
                names.insert(name.to_string(), format!("{name}_{tag}"));
            }
        }
    }
    for name in ["e", "pa", "pb", "fa", "fb", "va", "vb"] {
        names.insert(name.into(), format!("{name}_{tag}"));
    }
    let mut result = String::new();
    let mut word = String::new();
    for c in text.chars().chain(std::iter::once('\0')) {
        if c.is_ascii_alphanumeric() || c == '_' {
            word.push(c);
        } else {
            result.push_str(names.get(&word).unwrap_or(&word));
            word.clear();
            if c != '\0' {
                result.push(c);
            }
        }
    }
    result
}

fn emit(
    shares: &[ShardTiles],
    request: &SplitRequest,
    heads: i64,
    motif: AttentionMotif,
) -> R<String> {
    let mut definitions = String::new();
    let mut body = String::new();
    let mut tag = 0;
    for shard in shares {
        let node = request.nodes.ids()[shard.logical_node];
        body.push_str(&format!("    node {node}\n"));
        if shard.role == ShardRole::Empty {
            body.push_str("        barrier\n");
        }
        for ranges in shard.tile_ranges.chunks_exact(2) {
            let rank = shard.logical_node as i64;
            let share = Share {
                heads: ranges[0].clone(),
                query_tiles: ranges[1].clone(),
                origins: Origins {
                    s: rank * 1048576,
                    o: rank
                        * if motif == AttentionMotif::Srflash {
                            262144
                        } else {
                            65536
                        },
                    li: rank * 16384,
                    ..Origins::default()
                },
            };
            let text = namespace(&motif.text(&share, heads)?, tag);
            tag += 1;
            let (defs, function) = text
                .split_once(&format!("fn {}.l1(", motif.name()))
                .expect("template function");
            for line in defs.lines() {
                if !line.starts_with("target ") {
                    definitions.push_str(line);
                    definitions.push('\n');
                }
            }
            let (_, statements) = function.split_once('\n').expect("template body");
            for line in statements.lines() {
                body.push_str("    ");
                body.push_str(line);
                body.push('\n');
            }
        }
    }
    let p = request.nodes.ids().len();
    let declaration = motif
        .template()
        .lines()
        .find(|s| s.starts_with("fn "))
        .expect("template signature")
        .replace("fn flash.l1(", &format!("fn {}.l1(", motif.name()))
        .replace("10240", &(heads * 1024).to_string())
        .replace("2048, 64", &format!("{}, 64", heads * 1024))
        .replace("64, 2048", &format!("64, {}", heads * 1024))
        .replace(
            "o: f16[4096, 32] @dram",
            &format!("o: f16[{}, 32] @stage", 4096 * p),
        )
        .replace("s: f16[512, 1024]", &format!("s: f16[{}, 1024]", 512 * p))
        .replace("o: f16[512, 64]", &format!("o: f16[{}, 64]", 512 * p))
        .replace("li: f16[128, 64]", &format!("li: f16[{}, 64]", 128 * p));
    Ok(format!(
        "target ktpu.v9\n{definitions}\n{declaration}\n{body}"
    ))
}

/// Logical head/query prefixes use the reference template's physical port contract.
/// Q is already scaled by log2(e)/sqrt(64); each head retains 1024 keys.
pub fn plan(heads: i64, queries: i64, request: &SplitRequest, m: &Machine) -> R<PlannedKernel> {
    plan_motif(heads, queries, AttentionMotif::Flash, None, request, m)
}

pub(crate) fn plan_motif(
    heads: i64,
    queries: i64,
    motif: AttentionMotif,
    contract: Option<&str>,
    request: &SplitRequest,
    m: &Machine,
) -> R<PlannedKernel> {
    motif.admit(heads, queries, contract)?;
    let facts = Facts::of(m);
    motif.bind_roles(
        facts.mg as usize,
        facts.vc as usize,
        m.units(UnitKind::Sr).len(),
    )?;
    let spec = AttentionSpec {
        heads,
        queries,
        keys: 1024,
        dk: 64,
        dv: 64,
        mask: AttentionMask::None,
        precision: AttentionPrecision::Mx7,
    };
    let kernel = signature(heads, motif, m)?;
    let mut options = Vec::new();
    let tq = motif.query_rows();
    let mut numerics = request.numerics.clone();
    numerics.precision = match motif {
        AttentionMotif::Flash => "mx7/sticky-overflow",
        AttentionMotif::Flashd => "mx7/unit-scores",
        AttentionMotif::Srflash => "mx7/sr-row-max",
    }
    .into();
    for assignment in assignments(&spec, tq, &request.nodes)? {
        let (id, axis) = match assignment.partition {
            AttentionPartition::Heads => ("heads".into(), SplitAxis::Heads),
            AttentionPartition::QueryRows => ("query-rows".into(), SplitAxis::QueryRows),
            AttentionPartition::HeadsQuery { ph, pq } => (
                format!("heads-query-{ph}x{pq}"),
                SplitAxis::HeadsQuery { ph, pq },
            ),
            AttentionPartition::Balanced => ("balanced".into(), SplitAxis::DestinationTiles),
        };
        let mut output = OutputLayout {
            form: "producer".into(),
            tensors: kernel
                .graph
                .ports
                .iter()
                .filter(|p| p.role == g::Role::Out)
                .map(|p| (p.name.clone(), p.spec.clone()))
                .collect(),
            valid_region: (0..heads)
                .map(|h| region("y", h * 131072, h * 131072 + queries * 128))
                .collect(),
            ..OutputLayout::default()
        };
        let mut shards = Vec::new();
        for (rank, spans) in assignment.shares.iter().enumerate() {
            let mut shard = ShardTiles {
                logical_node: rank,
                tile_ranges: vec![],
                logical_origin: vec![],
                valid_extent: vec![],
                padded_extent: vec![],
                read_regions: vec![],
                write_regions: vec![],
                scratch_regions: vec![],
                local_schedule: format!("attention.{}", motif.name()),
                role: if spans.is_empty() {
                    ShardRole::Empty
                } else {
                    ShardRole::Compute
                },
            };
            for span in spans {
                let h = span.head;
                let q = &(span.query_tiles.start * (tq / 128)..span.query_tiles.end * (tq / 128));
                shard.read_regions.extend([
                    region("q", h * 65536 + q.start * 8192, h * 65536 + q.end * 8192),
                    region("k", h * 65536, (h + 1) * 65536),
                ]);
                if motif == AttentionMotif::Srflash {
                    for half in 0..2 {
                        let start = (half * heads + h) * 32768;
                        shard.read_regions.push(region("v", start, start + 32768));
                    }
                    shard.read_regions.push(region("tab", 0, 8192));
                } else {
                    shard
                        .read_regions
                        .push(region("v", h * 65536, (h + 1) * 65536));
                }
                let write = region(
                    "y",
                    h * 131072 + q.start * 16384,
                    h * 131072 + q.end * 16384,
                );
                output.ownership.push((rank, write.clone()));
                shard.write_regions.push(write);
                let n = shard.tile_ranges.len();
                if n >= 2
                    && shard.tile_ranges[n - 2].end == h
                    && shard.tile_ranges[n - 1] == *q
                    && (motif != AttentionMotif::Srflash || h - shard.tile_ranges[n - 2].start < 2)
                {
                    shard.tile_ranges[n - 2].end = h + 1;
                } else {
                    shard.tile_ranges.extend([h..h + 1, q.clone()]);
                }
            }
            if !spans.is_empty() {
                for &(name, bytes) in motif.scratch() {
                    shard.scratch_regions.push(region(
                        name,
                        rank as i64 * bytes,
                        (rank as i64 + 1) * bytes,
                    ));
                }
            }
            shards.push(shard);
        }
        validate_ownership(&output)?;
        let mut cycles = Vec::new();
        for shard in &shards {
            if shard.role != ShardRole::Empty {
                let text = emit(std::slice::from_ref(shard), request, heads, motif)?;
                let module =
                    read(&text, motif.name()).map_err(|e| Refusal::new(&e.clause, e.message))?;
                let share =
                    ktpu_l2::cost::cycles(&module, motif.name(), &ktpu_l2::target::Target::of(m))
                        .map_err(|e| Refusal::new(&e.clause, e.message))?;
                cycles.push(share - m.vc.launch_front as i64);
            }
        }
        let features = CostFeatures {
            share_cycles: cycles,
            dispatch_intercept: m.vc.launch_front as i64,
        };
        let option = SplitOption {
            id,
            axis,
            p: request.nodes.ids().len(),
            active_nodes: features.share_cycles.len(),
            shards,
            output,
            events: vec![],
            conversions: 0,
            cost: CostEstimate::default(),
            legality: LegalityWitness {
                numerics: numerics.clone(),
                clauses: vec!["complete keys per head/query tile; disjoint output lines".into()],
            },
        };
        options.push((option, features));
    }
    let split = choose_split(options, &LocalTiming, request.force.as_deref())?;
    let l1 = Some(emit(&split.shards, request, heads, motif)?);
    Ok(PlannedKernel {
        kernel,
        split,
        request: request.clone(),
        l1,
    })
}
