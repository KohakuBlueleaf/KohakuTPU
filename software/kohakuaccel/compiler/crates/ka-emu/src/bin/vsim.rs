//! A card of nodes under the command line of the core-only cluster model
//! (`os/sim/harness/kos_cluster_main.cpp`), so `os/tools/run_core.py` drives either.

use std::cell::RefCell;
use std::fs;
use std::process::ExitCode;
use std::rc::Rc;
use std::time::Instant;

use ka_emu::bus::Bus;
use ka_emu::node::{Memory, Silent, CONTROL, CONTROL_BYTES};
use ka_emu::{elf, Node};

const NODES: usize = 4;

struct Blob {
    node: usize,
    at: u64,
    file: String,
}

/// `node:addr:rest`.
fn triple(s: &str) -> Option<(usize, u64, String)> {
    let (node, rest) = s.split_once(':')?;
    let (at, rest) = rest.split_once(':')?;
    let at = u64::from_str_radix(at.strip_prefix("0x")?, 16).ok()?;
    Some((node.parse().ok()?, at, rest.to_string()))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mut image, mut blobs, mut dumps) = (None, Vec::new(), Vec::new());
    let mut expect: Vec<(usize, String)> = Vec::new();
    let mut until: Vec<(usize, u64, u64)> = Vec::new();
    let mut control: Vec<(usize, u64, u64)> = Vec::new();
    let (mut nodes, mut cycles) = (0, 50_000_000u64);
    let mut flat = false;
    let mut i = 0;
    while i < args.len() {
        let next = args.get(i + 1).cloned().unwrap_or_default();
        match args[i].as_str() {
            "--flat" => {
                flat = true;
                i += 1;
                continue;
            }
            "--elf" => image = Some(next),
            "--max-cycles" => cycles = next.parse().expect("--max-cycles"),
            "--boots" => assert_eq!(next, "1", "one boot"),
            "--blob" => {
                let (node, at, file) = triple(&next).expect("--blob N:0xADDR:FILE");
                nodes = nodes.max(node + 1);
                blobs.push(Blob { node, at, file });
            }
            "--blob-at" => {
                i += 1;
                let (node, at, file) = triple(&args[i + 1]).expect("--blob-at B N:0xADDR:FILE");
                nodes = nodes.max(node + 1);
                blobs.push(Blob { node, at, file });
            }
            "--dump" => {
                let (node, at, rest) = triple(&next).expect("--dump N:0xADDR:LEN:FILE");
                let (len, file) = rest.split_once(':').expect("--dump LEN:FILE");
                dumps.push((
                    node,
                    at,
                    len.parse::<usize>().expect("a length"),
                    file.to_string(),
                ));
            }
            "--until-word" | "--control-word" => {
                let parsed = triple(&next).and_then(|(node, at, value)| {
                    let value = u64::from_str_radix(value.strip_prefix("0x")?, 16).ok()?;
                    Some((node, at, value))
                });
                let Some(word) = parsed else {
                    eprintln!("{} N:0xADDR:0xVALUE", args[i]);
                    return ExitCode::FAILURE;
                };
                if args[i] == "--until-word" {
                    until.push(word);
                } else {
                    control.push(word);
                }
            }
            "--expect" => {
                i += 1;
                expect.push((next.parse().expect("--expect N"), args[i + 1].clone()));
            }
            other => panic!("no flag {other}"),
        }
        i += 2;
    }
    assert!(nodes <= NODES, "{nodes} nodes");
    if until.iter().any(|(node, _, _)| *node >= nodes) {
        eprintln!("--until-word must name a loaded node");
        return ExitCode::FAILURE;
    }
    if control
        .iter()
        .any(|&(node, offset, _)| node >= nodes || offset >= CONTROL_BYTES || offset & 7 != 0)
    {
        eprintln!("--control-word requires a loaded node and an aligned control offset");
        return ExitCode::FAILURE;
    }
    let bytes = fs::read(image.expect("--elf")).expect("the image");
    let image = elf::read(&bytes).expect("an ELF");
    let shared = (
        Rc::<RefCell<Memory>>::default(),
        Rc::<RefCell<_>>::default(),
    );
    shared.0.borrow_mut().flat = flat;
    let mut card: Vec<Node> = (0..nodes)
        .map(|n| Node::on(n, shared.clone(), Box::new(Silent)))
        .collect();
    for node in &mut card {
        node.load(&image).expect("the image's addresses");
    }
    for b in &blobs {
        let bytes = fs::read(&b.file).expect("a blob");
        card[b.node].place(b.at, &bytes).expect("a blob's address");
    }
    for (node, offset, value) in control {
        if card[node].soc.store(CONTROL + offset, 8, value).is_err() {
            eprintln!("cannot initialize control word {node}:{offset:#x}");
            return ExitCode::FAILURE;
        }
    }
    let t0 = Instant::now();
    let mut ran = 0;
    let mut printed = vec![0; nodes];
    let mut reached = false;
    while ran < cycles && card.iter().any(|n| n.soc.exit.is_none()) {
        for (i, node) in card.iter_mut().enumerate() {
            if node.soc.exit.is_none() {
                node.step();
            }
            let console = &node.soc.console;
            while let Some(end) = console[printed[i]..].iter().position(|b| *b == b'\n') {
                let line = &console[printed[i]..printed[i] + end];
                println!("[{i}] {}", String::from_utf8_lossy(line));
                printed[i] += end + 1;
            }
        }
        ran += 1;
        if !until.is_empty() && ran & 1023 == 0 {
            reached = until
                .iter()
                .all(|&(node, at, value)| card[node].soc.card_u64(at) == value);
            if reached {
                break;
            }
        }
    }
    if !reached && !until.is_empty() {
        reached = until
            .iter()
            .all(|&(node, at, value)| card[node].soc.card_u64(at) == value);
    }
    let mut ok = until.is_empty() || reached;
    for (i, node) in card.iter().enumerate() {
        let state = if node.soc.exit.is_some() {
            "done"
        } else if reached {
            "observed"
        } else {
            "NEVER FINISHED"
        };
        let code = node.soc.exit.unwrap_or(0);
        let (pc, cyc, ret) = (node.hart.pc, node.hart.cycle, node.hart.retired);
        println!("node {i}: {state} exit {code} haltpc {pc:x} cycles {cyc} retired {ret}");
        ok &= if until.is_empty() {
            node.soc.exit == Some(0)
        } else {
            node.soc.exit.is_none() || node.soc.exit == Some(0)
        };
    }
    for (n, want) in &expect {
        if !String::from_utf8_lossy(&card[*n].soc.console).contains(want) {
            println!("node {n}: expected \"{want}\" on the console");
            ok = false;
        }
    }
    for (n, at, len, file) in dumps {
        fs::write(file, card[n].soc.card_read(at, len)).expect("a dump");
    }
    let wall = t0.elapsed().as_secs_f64();
    println!(
        "total {ran} cycles, {wall:.3} s wall, {:.1} kcyc/s",
        ran as f64 / wall / 1000.0
    );
    println!("{}", if ok { "PASS" } else { "FAIL" });
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
