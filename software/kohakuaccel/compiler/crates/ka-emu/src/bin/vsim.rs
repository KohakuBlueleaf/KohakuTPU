//! A card of nodes under the command line of the core-only cluster model
//! (`os/sim/harness/kos_cluster_main.cpp`), so `os/tools/run_core.py` drives either.

use std::cell::RefCell;
use std::fs;
use std::process::ExitCode;
use std::rc::Rc;
use std::time::Instant;

use ka_emu::node::{Memory, Silent};
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
            "--expect" => {
                i += 1;
                expect.push((next.parse().expect("--expect N"), args[i + 1].clone()));
            }
            other => panic!("no flag {other}"),
        }
        i += 2;
    }
    assert!(nodes <= NODES, "{nodes} nodes");
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
    let t0 = Instant::now();
    let mut ran = 0;
    let mut printed = vec![0; nodes];
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
    }
    let mut ok = true;
    for (i, node) in card.iter().enumerate() {
        let state = if node.soc.exit.is_some() {
            "done"
        } else {
            "NEVER FINISHED"
        };
        let code = node.soc.exit.unwrap_or(0);
        let (pc, cyc, ret) = (node.hart.pc, node.hart.cycle, node.hart.retired);
        println!("node {i}: {state} exit {code} haltpc {pc:x} cycles {cyc} retired {ret}");
        ok &= node.soc.exit == Some(0);
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
