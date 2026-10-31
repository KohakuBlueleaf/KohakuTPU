//! The host decoder of the debug firmware's records (docs/spec/stack/debug/
//! firmware.md, RUL.080): the debug region's bytes, read after a completion,
//! as typed per-package results. Every name comes from the machine schema,
//! which the generator reads from the firmware headers and the RTL
//! (`fw.status`, `fw.waits`, `fw.signals`, `vc.fault_names`, `mover.faults`).
//! A record the decoder cannot read is an `Err` (DBG.API.002), never skipped.

pub mod layout;

use ktpu_machine::{refuse, Machine, Refusal};
use serde::Serialize;
use std::collections::BTreeMap;

/// The region's header.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Header {
    pub version: u64,
    /// Completion words kept per package unit.
    pub last: u64,
    pub units: u64,
    pub rec: u64,
    pub cap: u64,
    /// Record bytes produced, dropped, and consumed by the host so far.
    pub written: u64,
    pub lost: u64,
    pub read: u64,
}

/// A package unit's counters (the firmware's own, RUL.074).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Counters {
    pub sent: u32,
    pub received: u32,
    pub expected: u32,
    pub inflight: u32,
}

/// A unit as the package's unit table names it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct UnitRef {
    pub x: u32,
    pub y: u32,
    pub mesh: u32,
    /// The type code's two characters (`MG`, `VC`, `SR`).
    pub kind: String,
}

/// One step (or dispatch group `first..last`) of a traced run (DBG.TRACE.001).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Step {
    pub first: u32,
    pub last: u32,
    pub op: String,
    /// The step's package unit index, for an op that names one
    /// (`layout::UNIT_OPS`).
    pub unit: Option<u32>,
    /// The step word's `[31:16]` as written.
    pub field: u32,
    /// From bind, the trace's own cost subtracted.
    pub cycle: u64,
    /// "engine": when the step was queued to the dispatch engine;
    /// "firmware": when the firmware finished it (DBG.TRACE.004).
    pub clock: String,
    /// The unit's firmware counters after the step: firmware clock and a
    /// unit op only (the engine keeps its own).
    pub counters: Option<Counters>,
}

/// A package unit's state at a failure.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct UnitState {
    pub index: u32,
    pub unit: UnitRef,
    pub counters: Counters,
    pub acks: u32,
    pub credit: u32,
    /// Completions the firmware saw from it in this run.
    pub completions: u64,
}

/// A completion word decoded (`KA_CQ_*`, ka/hal/node.h).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Completion {
    pub word: u64,
    pub x: u32,
    pub y: u32,
    pub signal: String,
    pub arg: u32,
    /// The unit kind's fault name, for a vector core's SIG_FAULT.
    pub fault: Option<String>,
}

/// The mover status word (`KA_R_MVSTAT`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct MoverStat {
    pub room: bool,
    pub busy: bool,
    pub fault: u32,
    pub fault_name: String,
    pub done: u32,
}

/// The dispatch engine's `STAT` word (docs/spec/dispatch-engine.md), the
/// value of a TIMEOUT waiting on ENGINE (RUL.032): decoded here, never on
/// the node.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct EngineStat {
    pub magic: u32,
    pub depth: u32,
    pub overflow: bool,
    pub clearing: bool,
    /// The head entry is held (a write waiting on its unit's credit, a
    /// WAIT short of its count, or a move in progress).
    pub head_blocked: bool,
    pub enabled: bool,
    /// Entries issued since the last clear: the head entry's index in the
    /// package's queued entries, whose code is that entry's.
    pub issued: u32,
    pub queued: u32,
}

impl EngineStat {
    pub fn of(stat: u64) -> EngineStat {
        EngineStat {
            magic: (stat >> 48) as u32,
            depth: 1 << ((stat >> 40) & 0xff),
            overflow: stat >> 35 & 1 == 1,
            clearing: stat >> 34 & 1 == 1,
            head_blocked: stat >> 33 & 1 == 1,
            enabled: stat >> 32 & 1 == 1,
            issued: ((stat >> 16) & 0xffff) as u32,
            queued: (stat & 0xffff) as u32,
        }
    }
}

/// The failing step's report (DBG.FAIL.001).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Fail {
    pub step: u32,
    pub op: String,
    /// The step's package unit index, for an op that names one.
    pub unit: Option<u32>,
    /// The step word's `[31:16]` as written.
    pub field: u32,
    pub status: u32,
    pub status_name: String,
    pub detail: u32,
    /// The detail's name: the wait of a TIMEOUT, the mover fault of a
    /// MOVER_FAULT, "unit <i>" of a UNIT_FAULT.
    pub detail_name: String,
    pub value: u64,
    /// The value decoded as the engine's `STAT`, for a TIMEOUT waiting on
    /// ENGINE (RUL.032).
    pub engine_stat: Option<EngineStat>,
    /// The failing completion, for a UNIT_FAULT.
    pub completion: Option<Completion>,
    pub mover: MoverStat,
    pub stray: u32,
    pub outstanding: u32,
    /// The quiesce drained (DBG.DUMP.002): bytes read after this run are stable.
    pub drained: bool,
    /// The run was on the dispatch engine (DBG.FAIL.005).
    pub engine: bool,
    /// On the engine: its issue count when the failure was recorded, beside
    /// `step`, the step the firmware was queuing (DBG.FAIL.005).
    pub issued: Option<u32>,
    pub units: u64,
}

/// Everything one submission left in the region (DBG.SESS.002).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Package {
    pub tag: u64,
    pub steps: Vec<Step>,
    pub units: Vec<UnitState>,
    pub fail: Option<Fail>,
}

/// A package unit's last completions, oldest first (RUL.077).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Last {
    pub index: u32,
    pub count: u64,
    pub words: Vec<Completion>,
}

/// The region decoded.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Report {
    pub header: Header,
    /// By submission, in the order their records were written.
    pub packages: Vec<Package>,
    pub lasts: Vec<Last>,
    /// The read position the host writes back (`H_RD`) once it has these.
    pub consumed: u64,
}

fn word(b: &[u8], at: usize) -> Result<u64, Refusal> {
    b.get(at..at + 8)
        .map(|s| u64::from_le_bytes(s.try_into().unwrap()))
        .ok_or_else(|| Refusal::new("DBG.API.002", format!("region ends before byte {at}")))
}

/// `name -> code` tables of the schema, inverted, the prefix dropped.
struct Names {
    status: BTreeMap<u32, String>,
    waits: BTreeMap<u32, String>,
    signals: BTreeMap<u32, String>,
    vc: BTreeMap<u32, String>,
    mover: BTreeMap<u32, String>,
}

fn invert(t: &BTreeMap<String, u32>, prefix: &str) -> BTreeMap<u32, String> {
    t.iter()
        .map(|(k, v)| (*v, k.strip_prefix(prefix).unwrap_or(k).to_string()))
        .collect()
}

impl Names {
    fn of(m: &Machine) -> Names {
        Names {
            status: invert(&m.fw.status, "KA_ST_"),
            waits: invert(m.fw.waits.runtime(), "KA_WAIT_"),
            signals: invert(m.fw.signals.runtime(), "KA_SIG_"),
            vc: invert(m.vc.fault_names.runtime(), ""),
            mover: invert(m.mover.faults.runtime(), ""),
        }
    }

    fn name(t: &BTreeMap<u32, String>, code: u32) -> String {
        t.get(&code)
            .cloned()
            .unwrap_or_else(|| format!("{code:#x}"))
    }

    fn completion(&self, w: u64, kind: &str) -> Completion {
        let code = ((w >> 40) & 0xff) as u32;
        let arg = ((w >> 8) & 0xffff_ffff) as u32;
        let signal = Names::name(&self.signals, code);
        let fault = (signal == "FAULT" && kind == "VC").then(|| Names::name(&self.vc, arg & 0xff));
        Completion {
            word: w,
            x: ((w >> 48) & 0xf) as u32,
            y: ((w >> 52) & 0xf) as u32,
            signal,
            arg,
            fault,
        }
    }
}

fn op_name(code: u64) -> String {
    layout::OPS
        .get(code as usize)
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("{code:#x}"))
}

/// The package unit a step names: word 0's `[47:32]` (the step word's
/// `[31:16]`) for an op of `layout::UNIT_OPS`, 0xffff (any) excepted.
fn unit_of(op: &str, field: u32) -> Option<u32> {
    (layout::UNIT_OPS.contains(&op) && field != 0xffff).then_some(field)
}

fn kind(type_code: u64) -> String {
    let hi = ((type_code >> 8) & 0xff) as u8;
    let lo = (type_code & 0xff) as u8;
    if hi.is_ascii_uppercase() && lo.is_ascii_uppercase() {
        format!("{}{}", hi as char, lo as char)
    } else {
        format!("{type_code:#x}")
    }
}

fn unit_ref(w: u64) -> UnitRef {
    UnitRef {
        x: (w & 0xff) as u32,
        y: ((w >> 8) & 0xff) as u32,
        mesh: ((w >> 16) & 0xff) as u32,
        kind: kind(w >> 32),
    }
}

fn pair(w: u64) -> (u32, u32) {
    (w as u32, (w >> 32) as u32)
}

/// Decode `region` (the debug region's bytes as read after a completion)
/// against `m`'s names. Records run from the host's read position to the
/// firmware's write position.
pub fn decode(region: &[u8], m: &Machine) -> Result<Report, Refusal> {
    if word(region, layout::H_MAGIC)? != layout::MAGIC {
        return refuse("DBG.API.002", "the region holds no debug header (KADBG1)");
    }
    let header = Header {
        version: word(region, layout::H_VERSION)?,
        last: word(region, layout::H_LAST)?,
        units: word(region, layout::H_UNITS)?,
        rec: word(region, layout::H_REC)?,
        cap: word(region, layout::H_RECCAP)?,
        written: word(region, layout::H_WR)?,
        lost: word(region, layout::H_LOST)?,
        read: word(region, layout::H_RD)?,
    };
    if header.version != layout::VERSION {
        return refuse(
            "DBG.API.002",
            format!(
                "debug region version {}, this decoder reads {}",
                header.version,
                layout::VERSION
            ),
        );
    }
    let rb = layout::REC_BYTES as u64;
    if header.cap == 0 || !header.cap.is_multiple_of(rb) || header.written < header.read {
        return refuse(
            "DBG.API.002",
            format!("a debug header that cannot be: {header:?}"),
        );
    }
    if header.written - header.read > header.cap {
        return refuse(
            "DBG.TRACE.002",
            format!(
                "{} record bytes past the host's read, the ring holds {}",
                header.written - header.read,
                header.cap
            ),
        );
    }
    let names = Names::of(m);
    let mut packages: Vec<Package> = Vec::new();
    let mut kinds: BTreeMap<(u64, u32), String> = BTreeMap::new();
    let mut at = header.read;
    while at < header.written {
        let base = (header.rec + at % header.cap) as usize;
        let w: Vec<u64> = (0..8)
            .map(|k| word(region, base + 8 * k))
            .collect::<Result<_, _>>()?;
        let tag = w[1];
        if packages.last().is_none_or(|p| p.tag != tag) {
            packages.push(Package {
                tag,
                ..Package::default()
            });
        }
        let pkg = packages.last_mut().unwrap();
        let flags = (w[0] >> 8) & 0xff;
        match w[0] & 0xff {
            layout::STEP => {
                let (first, last) = pair(w[2]);
                let (sent, received) = pair(w[4]);
                let (expected, inflight) = pair(w[5]);
                let op = op_name((w[0] >> 16) & 0xff);
                let field = ((w[0] >> 32) & 0xffff) as u32;
                let unit = unit_of(&op, field);
                let engine = flags & layout::STEP_ENGINE != 0;
                pkg.steps.push(Step {
                    first,
                    last,
                    op,
                    unit,
                    field,
                    cycle: w[3],
                    clock: if engine { "engine" } else { "firmware" }.to_string(),
                    counters: (unit.is_some() && !engine).then_some(Counters {
                        sent,
                        received,
                        expected,
                        inflight,
                    }),
                });
            }
            layout::UNIT => {
                let (sent, received) = pair(w[3]);
                let (expected, inflight) = pair(w[4]);
                let (acks, credit) = pair(w[5]);
                let index = (w[0] >> 32) as u32;
                let unit = unit_ref(w[2]);
                kinds.insert((tag, index), unit.kind.clone());
                pkg.units.push(UnitState {
                    index,
                    unit,
                    counters: Counters {
                        sent,
                        received,
                        expected,
                        inflight,
                    },
                    acks,
                    credit,
                    completions: w[6],
                });
            }
            layout::FAIL => {
                let status = (w[2] & 0xffff) as u32;
                let detail = ((w[2] >> 16) & 0xffff) as u32;
                let status_name = Names::name(&names.status, status);
                let mv = w[4];
                let mfault = ((mv >> 28) & 0xf) as u32;
                let detail_name = match status_name.as_str() {
                    "TIMEOUT" => Names::name(&names.waits, detail),
                    "MOVER_FAULT" => Names::name(&names.mover, detail),
                    "UNIT_FAULT" if detail == 0xffff => "a unit not in the package".to_string(),
                    "UNIT_FAULT" => format!("unit {detail}"),
                    _ => format!("{detail:#x}"),
                };
                let completion = (status_name == "UNIT_FAULT").then(|| {
                    let k = kinds.get(&(tag, detail)).cloned().unwrap_or_default();
                    names.completion(w[3], &k)
                });
                let engine_stat = (status_name == "TIMEOUT" && detail_name == "ENGINE")
                    .then(|| EngineStat::of(w[3]));
                let (stray, outstanding) = pair(w[5]);
                let op = op_name((w[0] >> 16) & 0xff);
                let field = ((w[0] >> 32) & 0xffff) as u32;
                let engine = flags & layout::FAIL_ENGINE != 0;
                pkg.fail = Some(Fail {
                    step: (w[2] >> 32) as u32,
                    unit: unit_of(&op, field),
                    op,
                    field,
                    status,
                    status_name,
                    detail,
                    detail_name,
                    value: w[3],
                    engine_stat,
                    completion,
                    mover: MoverStat {
                        room: mv >> 33 & 1 == 1,
                        busy: mv >> 32 & 1 == 1,
                        fault: mfault,
                        fault_name: Names::name(&names.mover, mfault),
                        done: (mv & 0x0fff_ffff) as u32,
                    },
                    stray,
                    outstanding,
                    drained: flags & layout::FAIL_DRAINED != 0,
                    engine,
                    issued: engine.then_some(w[7] as u32),
                    units: w[6],
                });
            }
            t => {
                return refuse(
                    "DBG.API.002",
                    format!("record of type {t} at byte {at} of tag {tag:#x}"),
                )
            }
        }
        at += rb;
    }
    let n = header.last as usize;
    let mut lasts = Vec::new();
    let last_kind = |i: u32| {
        packages
            .last()
            .and_then(|p| kinds.get(&(p.tag, i)).cloned())
            .unwrap_or_default()
    };
    for i in 0..header.units as usize {
        let block = layout::LASTS + i * (1 + n) * 8;
        let count = word(region, block)?;
        if count == 0 || n == 0 {
            continue;
        }
        let k = last_kind(i as u32);
        let first = count.saturating_sub(n as u64);
        let words = (first..count)
            .map(|c| {
                word(region, block + 8 + (c as usize % n) * 8).map(|w| names.completion(w, &k))
            })
            .collect::<Result<_, _>>()?;
        lasts.push(Last {
            index: i as u32,
            count,
            words,
        });
    }
    Ok(Report {
        consumed: header.written,
        header,
        packages,
        lasts,
    })
}

/// [`decode`] as JSON, for the Python host (`_rs`).
pub fn decode_json(region: &[u8], m: &Machine) -> Result<serde_json::Value, Refusal> {
    let r = decode(region, m)?;
    serde_json::to_value(r).map_err(|e| Refusal::new("DBG.API.002", e.to_string()))
}
