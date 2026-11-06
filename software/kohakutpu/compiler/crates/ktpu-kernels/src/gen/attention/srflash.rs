//! MG pair -> SR -> MG head streams. A share owns complete 256-row
//! normalization groups; each SR retains its own row-state banks.

use super::flash::Share;
use crate::trace::{fail, R};

pub const TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../language/kohakutpu/language/examples/l1/sru/srflash.ktpu"
));

pub fn text(share: &Share, heads: i64) -> R<String> {
    share.validate()?;
    let nq = share.query_tiles.end - share.query_tiles.start;
    let nh = share.heads.end - share.heads.start;
    if nh > 2 || share.query_tiles.start % 2 != 0 || nq % 2 != 0 {
        return fail(
            "DSL.KERN.020",
            "srflash shares own up to two heads and whole 256-row groups",
        );
    }
    let nt = 4 * nq;
    let groups = nt / 8;
    let h0 = share.heads.start;
    let q0 = share.query_tiles.start;
    let o = &share.origins;
    let mut text = TEMPLATE.to_string();
    for (from, to) in [
        ("fn flash.l1(", "fn srflash.l1(".into()),
        ("for h in 0..2", format!("for h in 0..{nh}")),
        ("g/32 + 1 < 8", format!("g/32 + 1 < {nq}")),
        ("when t < 30", format!("when t < {}", nt - 2)),
        ("for t in 0..32", format!("for t in 0..{nt}")),
        (
            "    expand normg(2)\n    expand normg(3)",
            format!(
                "    for G in {}..{groups}\n        expand normg(G)",
                (groups - 2).max(0),
            ),
        ),
        (
            "q + h*64K",
            format!("q + {} + h*64K", o.q + h0 * 65536 + q0 * 8192),
        ),
        ("k + h*64K", format!("k + {} + h*64K", o.k + h0 * 65536)),
        ("v + h*32K", format!("v + {} + h*32K", o.v + h0 * 32768)),
        (
            "v + (2 + h)*32K",
            format!("v + {} + ({heads} + h)*32K", o.v + h0 * 32768),
        ),
        (
            "y + h*128K",
            format!("y + {} + h*128K", o.y + h0 * 131072 + q0 * 16384),
        ),
        ("o + ", format!("o + {} + ", o.o)),
    ] {
        text = text.replace(from, &to);
    }
    Ok(text)
}
