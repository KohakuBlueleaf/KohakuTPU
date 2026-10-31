//! One node's unit-to-unit transfers (L2.LOW.005-.007): each lands where
//! `send` allocates it (a ring at the top of a core's L1; a cluster bank,
//! alternating) and is taken by the receiver's `recv`s in order. A pair whose
//! receiver takes inside a loop is a stream: its transfers follow one another
//! around a ring.

use indexmap::IndexMap;
use ktpu_l1::text::Stmt;

use crate::ir::{Body, Result};
use crate::target::Target;

#[derive(Clone, Debug, PartialEq)]
pub struct Transfer {
    pub src: String,
    pub dst: String,
    /// "l1" words of a core, "A" / "B" entries of a cluster's banks
    pub side: String,
    pub at: i64,
    pub size: i64,
    pub shape: Vec<i64>,
    pub flow: bool,
    /// a stream sent a trip at a time: each trip's landing by parity
    pub slots: Vec<i64>,
    pub per: i64,
}

impl Transfer {
    fn new(
        src: &str,
        dst: &str,
        side: &str,
        at: i64,
        size: i64,
        shape: &[i64],
        flow: bool,
    ) -> Self {
        Transfer {
            src: src.to_string(),
            dst: dst.to_string(),
            side: side.to_string(),
            at,
            size,
            shape: shape.to_vec(),
            flow,
            slots: Vec::new(),
            per: 0,
        }
    }
}

/// A stream's ring: transfers land one after another in `[lo, lo + size)`.
#[derive(Clone, Debug)]
pub struct Ring {
    pub src: String,
    pub dst: String,
    pub side: String,
    pub lo: i64,
    pub size: i64,
    pub sent: i64,
    pub taken: i64,
    /// Words an MXFP7 entry of a cluster side takes.
    pub entry_words: i64,
}

impl Ring {
    /// The ring's room in credits (32-byte granules).
    pub fn granules(&self) -> i64 {
        if self.side == "l1" {
            self.size
        } else {
            self.entry_words * self.size
        }
    }
}

pub type Key = (String, String, String);

/// The transfers of one node, or (`alone`) the stand-in for costing one
/// vector instance by itself: a `recv` lands at the top of the L1, a `send`
/// at a bank's first entry.
#[derive(Clone, Debug, Default)]
pub struct Links {
    pub alone: bool,
    pub queues: IndexMap<Key, Vec<Transfer>>,
    pub ring: IndexMap<String, i64>,
    pub banks: IndexMap<(String, String), i64>,
    pub rings: IndexMap<Key, Ring>,
    l1_words: i64,
    bank: i64,
    pool: i64,
}

fn key(src: &str, dst: &str, side: &str) -> Key {
    (src.to_string(), dst.to_string(), side.to_string())
}

impl Links {
    pub fn new(streams: &IndexMap<Key, i64>, t: &Target) -> Links {
        let mut rings = IndexMap::new();
        for ((src, dst, side), &size) in streams {
            let lo = if side == "l1" { t.l1_words - size } else { 0 };
            rings.insert(
                key(src, dst, side),
                Ring {
                    src: src.clone(),
                    dst: dst.clone(),
                    side: side.clone(),
                    lo,
                    size,
                    sent: 0,
                    taken: 0,
                    entry_words: t.entry_words(),
                },
            );
        }
        Links {
            alone: false,
            rings,
            l1_words: t.l1_words,
            bank: t.bank,
            pool: t.pool(),
            ..Default::default()
        }
    }

    pub fn alone(t: &Target) -> Links {
        Links {
            alone: true,
            l1_words: t.l1_words,
            bank: t.bank,
            pool: t.pool(),
            ..Default::default()
        }
    }

    pub fn stream(&self, src: &str, dst: &str, side: &str) -> Option<&Ring> {
        if self.alone {
            return None;
        }
        self.rings.get(&key(src, dst, side))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn send(
        &mut self,
        body: &Body,
        st: &Stmt,
        src: &str,
        dst: &str,
        side: &str,
        size: i64,
        shape: &[i64],
    ) -> Result<Transfer> {
        if self.alone {
            return Ok(Transfer::new(src, dst, side, 0, size, shape, false));
        }
        let k = key(src, dst, side);
        if let Some(r) = self.rings.get_mut(&k) {
            if r.size % size != 0 {
                return Err(body.reader.fail(
                    st,
                    "L2.LOW.006",
                    format!("{size} into {dst}'s ring of {}: whole transfers", r.size),
                ));
            }
            let at = r.lo + r.sent % r.size;
            r.sent += size;
            let x = Transfer::new(src, dst, side, at, size, shape, true);
            self.queues.entry(k).or_default().push(x.clone());
            return Ok(x);
        }
        let at = if side == "l1" {
            let pool = self.pool;
            if size > pool {
                return Err(body.reader.fail(
                    st,
                    "L2.LOW.013",
                    format!("{size} words into {dst}: a core takes {pool} at a time"),
                ));
            }
            let mut a = self.ring.get(dst).copied().unwrap_or(self.l1_words - pool);
            if a + size > self.l1_words {
                a = self.l1_words - pool;
            }
            self.ring.insert(dst.to_string(), a + size);
            a
        } else {
            if size > self.bank {
                return Err(body.reader.fail(
                    st,
                    "L2.LOW.013",
                    format!("{size} entries into {dst}: a bank holds {}", self.bank),
                ));
            }
            let bk = (dst.to_string(), side.to_string());
            let first = crate::mutant::on("l2.lower/landing-bank1") as i64;
            let bank = self.banks.get(&bk).copied().unwrap_or(first);
            self.banks.insert(bk, 1 - bank);
            bank * self.bank
        };
        let x = Transfer::new(src, dst, side, at, size, shape, false);
        self.queues.entry(k).or_default().push(x.clone());
        Ok(x)
    }

    /// The oldest transfer from `src` into `dst`'s side not yet taken (None
    /// when costing alone).
    pub fn take(
        &mut self,
        body: &Body,
        st: &Stmt,
        src: &str,
        dst: &str,
        side: &str,
    ) -> Result<Option<Transfer>> {
        if self.alone {
            return Ok(None);
        }
        match self.queues.get_mut(&key(src, dst, side)) {
            Some(q) if q.is_empty() && crate::mutant::on("l2.lower/recv-unsent") => Ok(None),
            None if crate::mutant::on("l2.lower/recv-unsent") => Ok(None),
            Some(q) if !q.is_empty() => Ok(Some(q.remove(0))),
            _ => Err(body.reader.fail(
                st,
                "L2.LOW.013",
                format!("recv: {src} sent nothing into {dst} ({side}) before this"),
            )),
        }
    }

    /// `size` units of the stream, from the oldest transfers not yet taken.
    pub fn take_stream(
        &mut self,
        body: &Body,
        st: &Stmt,
        src: &str,
        dst: &str,
        side: &str,
        size: i64,
    ) -> Result<Transfer> {
        let k = key(src, dst, side);
        let (lo, rsize, taken) = {
            let r = &self.rings[&k];
            (r.lo, r.size, r.taken)
        };
        let mut first: Option<Transfer> = None;
        let mut got = 0;
        while got < size {
            let q = self.queues.entry(k.clone()).or_default();
            if q.is_empty() {
                return Err(body.reader.fail(
                    st,
                    "L2.LOW.006",
                    format!("recv: {src} sent {got} of {size} into {dst} ({side})"),
                ));
            }
            let x = q.remove(0);
            if x.at != lo + (taken + got) % rsize {
                return Err(body.reader.fail(
                    st,
                    "L2.LOW.006",
                    format!("recv: {src}'s transfers into {dst} are out of order"),
                ));
            }
            got += x.size;
            if first.is_none() {
                first = Some(x);
            }
        }
        if got != size {
            return Err(body.reader.fail(
                st,
                "L2.LOW.006",
                format!("recv of {size} splits {src}'s transfer into {dst}"),
            ));
        }
        self.rings.get_mut(&k).unwrap().taken += size;
        let f = first.unwrap();
        Ok(Transfer::new(src, dst, side, f.at, size, &f.shape, true))
    }

    pub fn check(&self, body: &Body, st: Option<&Stmt>) -> Result<()> {
        for ((src, dst, side), q) in &self.queues {
            if !q.is_empty() && !crate::mutant::on("l2.lower/unreceived-ignored") {
                let msg = format!("{src} sends into {dst} ({side}) that nothing receives");
                return Err(match st {
                    Some(st) => body.reader.fail(st, "L2.LOW.013", msg),
                    None => ktpu_l1::Error::new("L2.LOW.013", msg),
                });
            }
        }
        Ok(())
    }
}
