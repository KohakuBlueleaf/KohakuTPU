//! Direct score-ring specialization. Inner images and credit streams belong to
//! the hand motif; only ownership coordinates and pipeline bounds are substituted.

use super::flash::Share;
use crate::trace::R;

pub const TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../language/kohakutpu/language/examples/l1/fused/flashd.ktpu"
));

pub fn text(share: &Share) -> R<String> {
    share.validate()?;
    let nq = share.query_tiles.end - share.query_tiles.start;
    let nh = share.heads.end - share.heads.start;
    let per_head = 4 * nq;
    let subrows = 32 * nq;
    let nt = nh * per_head;
    let mut text = TEMPLATE.to_string();
    for (from, to) in [
        ("g/256", format!("g/{subrows}")),
        ("(8*nt - 1)/256", format!("(8*nt - 1)/{subrows}")),
        ("g % 256", format!("g % {subrows}")),
        (
            "q + (g/32 + 1)*8K",
            format!("q + ((g/32 + 1)/{nq})*64K + ((g/32 + 1) % {nq})*8K"),
        ),
        ("fill A0 <- q n=64", "fill A0 <- q + 0 n=64".into()),
        ("t % 32", format!("t % {per_head}")),
        ("t/32", format!("t/{per_head}")),
        (
            "y + (t - 5)*4K",
            format!("y + ((t - 5)/{per_head})*128K + ((t - 5) % {per_head})*4K"),
        ),
        (
            "y + t*4K",
            format!("y + (t/{per_head})*128K + (t % {per_head})*4K"),
        ),
        ("for T in 0..4", format!("for T in 0..{}", nt.min(4))),
        ("scores(T, 320, 10)", format!("scores(T, {nt}, {nh})")),
        ("for t in 0..321", format!("for t in 0..{}", nt + 1)),
        ("when t < 320", format!("when t < {nt}")),
        ("after(t, 320)", format!("after(t, {nt})")),
        ("when t < 316", format!("when t < {}", nt - 4)),
        (
            "scores(t + 4, 320, 10)",
            format!("scores(t + 4, {nt}, {nh})"),
        ),
        (
            "for t in 315..320",
            format!("for t in {}..{nt}", (nt - 5).max(0)),
        ),
    ] {
        text = text.replace(from, &to);
    }
    Ok(share.rebase(&text))
}
