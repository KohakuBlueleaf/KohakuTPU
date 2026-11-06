//! Raw packed-B delivery between sysnodes. The adapter retains the lowered
//! operand-A fills, GEMM words, product ring and epilogue images.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use ktpu_l1::text::{fmt, head, parse, Stmt, Term};
use ktpu_l2::lower::events::{EventId, EventKind, EventSpec};
use ktpu_l2::lower::sync::{Access, Stage};
use ktpu_l2::target::Target;

use crate::kernel::Kernel;
use crate::split::{Delivery, SplitAxis, SplitOption, SplitRequest};
use crate::trace::R;
use ktpu_machine::{Machine, Refusal};

#[derive(Clone, Debug, Default)]
pub(super) struct Traffic {
    pub fills: u64,
    pub read_bytes: u64,
    pub local_bytes: u64,
    pub remote_bytes: u64,
    pub route_bytes: u64,
    pub relay_words: u64,
    pub handshakes: u64,
    pub issues: u64,
    pub remote_payloads: u64,
    pub private_fill_bytes: u64,
}

pub(super) struct Emitted {
    pub text: String,
    pub traffic: Traffic,
    pub events: Vec<EventSpec>,
    reads: BTreeMap<i64, Vec<crate::split::Region>>,
}

impl Emitted {
    pub fn manifest(&self, option: &mut SplitOption, request: &SplitRequest) {
        option.events = self.events.clone();
        for shard in &mut option.shards {
            shard.read_regions.retain(|r| r.allocation != "b");
        }
        for (&node, reads) in &self.reads {
            if let Some(shard) = option.shards.iter_mut().find(|s| {
                request.nodes.ids()[s.logical_node] == node
                    && s.role != crate::split::ShardRole::Empty
            }) {
                shard.read_regions.extend(reads.iter().cloned());
            }
        }
    }

    pub fn price(
        &self,
        name: &str,
        request: &SplitRequest,
        machine: &Machine,
    ) -> R<crate::split::CostEstimate> {
        let module = ktpu_l1::text::read(&self.text, "B delivery")
            .map_err(|e| Refusal::new(&e.clause, e.message))?;
        let body = module
            .body(name, "l1")
            .map_err(|e| Refusal::new(&e.clause, e.message))?;
        let mut cycles = Vec::new();
        for node in request.nodes.ids().iter().filter_map(|id| {
            body.stmts
                .iter()
                .find(|s| s.positional().first().and_then(|n| n.as_int()) == Some(i128::from(*id)))
        }) {
            let mut local = module.clone();
            if let Some(body) = local.fns.get_mut(&(name.to_string(), "l1".into())) {
                body.stmts = vec![node.clone()];
            }
            cycles.push(
                ktpu_l2::cost::cycles(&local, name, &Target::of(machine))
                    .map_err(|e| Refusal::new(&e.clause, e.message))?
                    - machine.vc.launch_front as i64,
            );
        }
        let t = &self.traffic;
        crate::split::cost::price_delivery(
            cycles,
            crate::split::cost::DeliveryCost {
                sender_fills: t.fills,
                sender_bytes: t.read_bytes,
                local_bytes: t.local_bytes,
                remote_bytes: t.remote_bytes,
                route_bytes: t.route_bytes,
                remote_payloads: t.remote_payloads,
                relay_words: t.relay_words,
                bank_events: t.handshakes,
                compute_cycles: t.issues,
                private_fill_bytes: t.private_fill_bytes,
            },
            machine,
        )
    }
}

#[derive(Clone)]
struct Fill {
    node: i64,
    unit: String,
    bank: i64,
    at: i64,
    base: String,
    offset: i64,
    entries: i64,
}

fn error(message: impl Into<String>) -> ktpu_l1::Error {
    ktpu_l1::Error::new("L2.MG.006", message)
}

fn address(t: &Term) -> ktpu_l1::Result<(String, i64)> {
    match t {
        Term::Name(n) => Ok((n.clone(), 0)),
        Term::Offset(base, off) => {
            let (base, start) = address(base)?;
            let offset = off
                .as_int()
                .ok_or_else(|| error("packed fill offset is constant"))?;
            Ok((base, start + offset as i64))
        }
        _ => Err(error("packed fill names a buffer and byte offset")),
    }
}

fn integer(st: &Stmt, key: &str, default: i64) -> ktpu_l1::Result<i64> {
    st.kw(key).map_or(Ok(default), |t| {
        t.as_int()
            .map(|v| v as i64)
            .ok_or_else(|| error(format!("{key} is constant in a packed delivery")))
    })
}

fn send(lines: &mut Vec<String>, unit: &str, words: &[Stmt]) {
    if !words.is_empty() {
        lines.push(format!("send {unit}"));
        lines.extend(words.iter().map(|s| format!("    {}", head(s))));
    }
}

struct Adapter<'a> {
    request: &'a SplitRequest,
    target: &'a Target,
    relay: bool,
    images: Vec<String>,
    events: BTreeMap<String, EventSpec>,
    traffic: Traffic,
    generation: usize,
    reads: BTreeMap<i64, Vec<crate::split::Region>>,
}

impl Adapter<'_> {
    fn event(&mut self, from: i64, to: i64) -> String {
        // Ready and retirement are alternating tokens on one leased FIFO.
        let kind = "bready";
        let id = format!("@{kind}:{from}:{to}@");
        self.events.entry(id.clone()).or_insert_with(|| EventSpec {
            id: EventId(id.clone()),
            producers: vec![from],
            consumers: vec![to],
            released_regions: vec![],
            acquire_regions: vec![],
            kind: EventKind::BankCredit,
            count: 1,
            lifetime: 0..1,
        });
        self.traffic.handshakes += 1;
        id
    }

    fn image(&mut self, body: String, args: &str, call: &str, lines: &mut Vec<String>) {
        let name = format!("__bpass_{}", self.images.len());
        self.images
            .push(format!("image {name}({args})\n{body}    halt\n"));
        lines.extend([
            "send vc[0]".into(),
            format!("    run {name}({call})"),
            "wait vc[0]".into(),
        ]);
    }

    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        from: i64,
        to: i64,
        unit: &str,
        buf: i64,
        at: i64,
        off: i64,
        words: i64,
        lines: &mut Vec<String>,
    ) -> ktpu_l1::Result<()> {
        let remote = from != to;
        let (kind, index) = if let Some(i) = unit.strip_prefix("mg[") {
            (ktpu_machine::UnitKind::Mg, i.trim_end_matches(']'))
        } else {
            (ktpu_machine::UnitKind::Vc, "0")
        };
        let index = index.parse::<usize>().map_err(|_| error("unit index"))?;
        let coord = self
            .target
            .machine
            .unit(kind, index)
            .ok_or_else(|| error("delivery unit"))?;
        let (x, y) = (coord.x(), coord.y());
        let port = self.target.machine.die.fetch_port;
        let destination = if remote {
            format!(", mesh={to}, fin=({x}, {y})")
        } else {
            String::new()
        };
        let (dx, dy) = if remote { (port.x(), port.y()) } else { (x, y) };
        self.image(
            format!(
                "    desc a0 = peer({at}, count=1{destination}) walk=(1, {words})\n    vdrain a0 <- l1[{off}] to=({dx}, {dy}) buf={buf}\n"
            ),
            "",
            "",
            lines,
        );
        let bytes = (words * self.target.word_bytes) as u64;
        if remote {
            self.traffic.remote_bytes += bytes;
            let split = self.target.machine.flaws.f002.present
                && words % i64::from(self.target.machine.il.packet_beats) == 0;
            let payloads = 1 + u64::from(split);
            self.traffic.remote_payloads += payloads;
            self.traffic.route_bytes +=
                (bytes + payloads * self.target.word_bytes as u64) * from.abs_diff(to);
        } else {
            self.traffic.local_bytes += bytes;
        }
        Ok(())
    }

    fn delivery(
        &mut self,
        fills: &[Fill],
        base: &str,
        lo: i64,
        hi: i64,
        out: &mut BTreeMap<i64, Vec<String>>,
    ) -> ktpu_l1::Result<()> {
        let ew = self.target.entry_words();
        let eb = ew * self.target.word_bytes;
        let nodes: Vec<_> = self
            .request
            .nodes
            .ids()
            .iter()
            .copied()
            .filter(|n| fills.iter().any(|f| f.node == *n))
            .collect();
        let source = nodes[0];
        let half = self.target.l1_words / 2 / ew * ew;
        let mut start = lo;
        while start < hi {
            // The vector AGU's run accumulator is eight bits (ISA.VC.131).
            // Complete-entry segments avoid a one-word trailing memory request.
            let capacity = half.min(255 / ew * ew);
            let words = ((hi - start) / self.target.word_bytes).min(capacity);
            let off = (self.generation as i64 % 2) * half;
            self.generation += 1;
            for &node in &nodes {
                if node != source {
                    let ready = self.event(node, source);
                    out.get_mut(&node)
                        .expect("participant")
                        .push(format!("ring mesh={source} slot={ready}"));
                    out.get_mut(&source)
                        .expect("source")
                        .push(format!("wait slot {ready} n=1 consume"));
                }
            }
            self.image(
                format!(
                    "    desc a0 = b walk=({}, {words})\n    vfill a0 -> l1[{off}]\n",
                    self.target.word_bytes
                ),
                "b",
                &format!("{base} + {start}"),
                out.get_mut(&source).expect("source"),
            );
            self.traffic.fills += 1;
            self.traffic.read_bytes += (words * self.target.word_bytes) as u64;
            self.reads
                .entry(source)
                .or_default()
                .push(crate::split::Region {
                    allocation: base.into(),
                    bytes: start as u64..(start + words * self.target.word_bytes) as u64,
                });
            for (rank, &node) in nodes.iter().enumerate() {
                if self.relay && rank > 0 {
                    self.image(
                        format!("    vwait fe rx n={words}\n"),
                        "",
                        "",
                        out.get_mut(&node).expect("participant"),
                    );
                    self.traffic.relay_words += words as u64;
                }
                for f in fills.iter().filter(|f| f.node == node) {
                    let at = (f.bank * self.target.bank + f.at + (start - f.offset) / eb) * ew;
                    let from = if self.relay { node } else { source };
                    self.push(
                        from,
                        node,
                        &f.unit,
                        1,
                        at,
                        off,
                        words,
                        out.get_mut(&from).expect("sender"),
                    )?;
                    let lines = out.get_mut(&node).expect("participant");
                    lines.extend([
                        format!("send {}", f.unit),
                        format!("    fill B{} recv n={}", f.bank, words / ew),
                        format!("wait {}", f.unit),
                    ]);
                }
                if self.relay && rank + 1 < nodes.len() {
                    self.push(
                        node,
                        nodes[rank + 1],
                        "vc[0]",
                        0,
                        off,
                        off,
                        words,
                        out.get_mut(&node).expect("relay"),
                    )?;
                }
            }
            for &node in nodes.iter().skip(1) {
                let credit = self.event(node, source);
                out.get_mut(&node)
                    .expect("participant")
                    .push(format!("ring mesh={source} slot={credit}"));
                out.get_mut(&source)
                    .expect("source")
                    .push(format!("wait slot {credit} n=1 consume"));
            }
            start += words * self.target.word_bytes;
        }
        Ok(())
    }

    fn stage(&mut self, stages: &mut [(i64, &mut Stage)]) -> ktpu_l1::Result<()> {
        let mut streams: BTreeMap<(i64, String), VecDeque<Stmt>> = BTreeMap::new();
        let mut out: BTreeMap<_, Vec<String>> = stages
            .iter()
            .map(|(n, _)| (*n, vec!["wait vc[0]".into()]))
            .collect();
        for (node, stage) in stages.iter() {
            for st in parse(&stage.lines.join("\n"), "B-pass cluster stage")? {
                if st.op == "send" {
                    let unit = fmt(st.positional()[0]);
                    streams.entry((*node, unit)).or_default().extend(st.body);
                } else if st.op != "mark" && st.op != "wait" {
                    return Err(error(
                        "a delivery stage contains cluster sends and shared-fill rendezvous",
                    ));
                }
            }
        }
        while streams.values().any(|s| !s.is_empty()) {
            let mut fills = Vec::new();
            let mut sweeps = Vec::new();
            let mut prefixes = BTreeMap::new();
            for ((node, unit), stream) in &mut streams {
                let mut prefix = Vec::new();
                while let Some(st) = stream.pop_front() {
                    if st.op == "gemm" {
                        sweeps.push((*node, unit.clone(), st));
                        break;
                    }
                    let pos = st.positional();
                    if st.op == "fill"
                        && matches!(pos.first().and_then(|p| p.as_name()), Some("B0" | "B1"))
                    {
                        let (base, offset) = address(pos[2])?;
                        fills.push(Fill {
                            node: *node,
                            unit: unit.clone(),
                            bank: i64::from(pos[0].as_name() == Some("B1")),
                            at: integer(&st, "at", 0)?,
                            entries: integer(&st, "n", 1)?,
                            base,
                            offset,
                        });
                    } else {
                        if st.op == "fill" {
                            // Shared A contributes one source read per peer group.
                            let mut leader = unit.clone();
                            if let Some(Term::Tuple(peers)) = st.kw("peers") {
                                for peer in peers {
                                    leader = leader.min(fmt(peer));
                                }
                            }
                            if leader == *unit {
                                self.traffic.private_fill_bytes += (integer(&st, "n", 1)?
                                    * self.target.entry_words()
                                    * self.target.word_bytes)
                                    as u64;
                            }
                        }
                        prefix.push(st);
                    }
                }
                prefixes.insert((*node, unit.clone()), VecDeque::from(prefix));
            }
            while prefixes.values().any(|p| !p.is_empty()) {
                for ((node, unit), prefix) in &mut prefixes {
                    let mut private = Vec::new();
                    while prefix.front().is_some_and(|s| s.kw("peers").is_none()) {
                        if let Some(st) = prefix.pop_front() {
                            private.push(st);
                        }
                    }
                    send(out.get_mut(node).expect("node"), unit, &private);
                }
                // Every private prerequisite retires before any shared-A leader.
                for (node, unit) in prefixes.keys() {
                    out.get_mut(node)
                        .expect("node")
                        .push(format!("wait {unit}"));
                }
                for ((node, unit), prefix) in &mut prefixes {
                    if let Some(shared) = prefix.pop_front() {
                        send(out.get_mut(node).expect("node"), unit, &[shared]);
                    }
                }
                for (node, unit) in prefixes.keys() {
                    out.get_mut(node)
                        .expect("node")
                        .push(format!("wait {unit}"));
                }
            }
            for (node, unit) in streams.keys() {
                out.get_mut(node)
                    .expect("node")
                    .push(format!("wait {unit}"));
            }
            let eb = self.target.entry_words() * self.target.word_bytes;
            let mut cuts: BTreeMap<String, BTreeSet<i64>> = BTreeMap::new();
            for f in &fills {
                cuts.entry(f.base.clone())
                    .or_default()
                    .extend([f.offset, f.offset + f.entries * eb]);
            }
            for (base, cuts) in cuts {
                let cuts: Vec<_> = cuts.into_iter().collect();
                for pair in cuts.windows(2) {
                    let (lo, hi) = (pair[0], pair[1]);
                    let receivers: Vec<_> = fills
                        .iter()
                        .filter(|f| {
                            f.base == base && f.offset <= lo && hi <= f.offset + f.entries * eb
                        })
                        .cloned()
                        .collect();
                    if !receivers.is_empty() {
                        self.delivery(&receivers, &base, lo, hi, &mut out)?;
                    }
                }
            }
            let mut critical = 0;
            for (node, unit, sweep) in sweeps {
                if let Some(Term::Dims(d)) = sweep.positional().first() {
                    let subtiles = d[0] * d[1];
                    let idle = if d[2] <= 1 {
                        0
                    } else {
                        match subtiles {
                            1 => 4,
                            2 => 2,
                            3 | 4 => 1,
                            _ => 0,
                        }
                    };
                    critical = critical.max((subtiles * ((d[2] + 1) / 2) * (1 + idle)) as u64);
                }
                send(out.get_mut(&node).expect("node"), &unit, &[sweep]);
            }
            self.traffic.issues += critical;
        }
        for (node, stage) in stages.iter_mut() {
            stage.lines = out.remove(node).expect("stage");
            stage.segs.clear();
            stage.fence = true;
            stage.units.push("vc[0]".into());
            stage.accesses.push(Access::new(
                "vc[0]",
                true,
                ("vc[0]", "l1"),
                0,
                self.target.l1_words,
            ));
        }
        Ok(())
    }
}

pub(super) fn emit(
    kernel: &Kernel,
    option: &SplitOption,
    request: &SplitRequest,
    machine: &Machine,
) -> R<Emitted> {
    let target = Target::of(machine);
    let mut adapter = Adapter {
        request,
        target: &target,
        relay: matches!(
            option.axis,
            SplitAxis::BPass {
                delivery: Delivery::Relay,
                ..
            }
        ),
        images: Vec::new(),
        events: BTreeMap::new(),
        traffic: Traffic::default(),
        generation: 0,
        reads: BTreeMap::new(),
    };
    let program = ktpu_graph::program(&kernel.graph, machine)?;
    let lowered = ktpu_l2::passes::run(
        &program,
        &ktpu_l2::PASSES,
        &target,
        &ktpu_l2::Search::default(),
    )
    .map_err(|e| Refusal::new(&e.clause, e.message))?;
    let text = ktpu_l2::lower::lower_adapted(
        &lowered,
        &target,
        &ktpu_l2::Search::default(),
        &ktpu_l2::lower::events::BackendSyncFacts::common(target.il_slots),
        |nodes, images| {
            let mut groups: BTreeMap<usize, Vec<(i64, &mut Stage)>> = BTreeMap::new();
            for (node, stages) in nodes {
                let mut index = 0;
                for stage in stages {
                    if !stage.units.is_empty() && stage.units.iter().all(|u| u.starts_with("mg[")) {
                        groups.entry(index).or_default().push((*node, stage));
                        index += 1;
                    }
                }
            }
            for stages in groups.values_mut() {
                adapter.stage(stages)?;
            }
            images.append(&mut adapter.images);
            Ok(())
        },
    )
    .map_err(|e| Refusal::new(&e.clause, e.message))?;
    let module =
        ktpu_l1::text::read(&text, "B delivery").map_err(|e| Refusal::new(&e.clause, e.message))?;
    let module = ktpu_l1::sched::schedule_module(&module, machine)
        .map_err(|e| Refusal::new(&e.clause, e.message))?;
    Ok(Emitted {
        text: module.source,
        traffic: adapter.traffic,
        events: adapter.events.into_values().collect(),
        reads: adapter.reads,
    })
}
