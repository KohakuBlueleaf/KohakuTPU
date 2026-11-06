//! Resource contracts of the hand attention streams.
//!
//! Role binding checks capacity; it is not a numerical or scheduling certificate.

use crate::trace::{fail, R};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttentionMotif {
    Flash,
    Flashd,
    Srflash,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SoftmaxState {
    /// Two independent key halves; f16 O, sum and integral offset per half.
    HalfPartials,
    /// SR row state keeps the maximum and reciprocal row sum.
    SrRows,
}

#[derive(Clone, Copy, Debug)]
pub struct PackGeometry {
    pub q_gm: usize,
    pub qk_nk: usize,
    pub k_gn: usize,
    pub v_gn: usize,
    pub v_nk: usize,
}

/// Simultaneously resident roles, in the hand stream's local-unit order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoleBinding {
    pub scores: [usize; 2],
    pub products: [usize; 2],
    pub vectors: Vec<usize>,
    pub sr: Vec<usize>,
}

impl AttentionMotif {
    pub fn name(self) -> &'static str {
        match self {
            Self::Flash => "flash",
            Self::Flashd => "flashd",
            Self::Srflash => "srflash",
        }
    }

    pub fn template(self) -> &'static str {
        match self {
            Self::Flash => super::flash::TEMPLATE,
            Self::Flashd => super::flashd::TEMPLATE,
            Self::Srflash => super::srflash::TEMPLATE,
        }
    }

    pub fn text(self, share: &super::flash::Share, heads: i64) -> R<String> {
        match self {
            Self::Flash => share.text(),
            Self::Flashd => super::flashd::text(share),
            Self::Srflash => super::srflash::text(share, heads),
        }
    }

    pub fn scratch(self) -> &'static [(&'static str, i64)] {
        match self {
            Self::Flash => &[("s", 1048576), ("o", 65536), ("li", 16384)],
            Self::Flashd => &[("o", 65536), ("li", 16384)],
            Self::Srflash => &[("o", 262144)],
        }
    }

    /// Complete score/PV/statistic ownership unit in query rows.
    pub fn query_rows(self) -> i64 {
        match self {
            Self::Flash | Self::Flashd => 128,
            Self::Srflash => 256,
        }
    }

    pub(crate) fn admit(self, heads: i64, queries: i64, contract: Option<&str>) -> R<()> {
        if !(1..=10).contains(&heads)
            || !(self.query_rows()..=1024).contains(&queries)
            || queries % self.query_rows() != 0
        {
            return fail(
                "API.OP.001",
                format!(
                    "{} expects 1..=10 heads and query groups of {} up to 1024 rows",
                    self.name(),
                    self.query_rows(),
                ),
            );
        }
        if contract.is_some_and(|c| c != "unit-scores") {
            return fail("API.OP.001", "unknown attention numerical contract");
        }
        if self == Self::Flashd && contract != Some("unit-scores") {
            return fail("API.OP.001", "flashd requires the unit-scores contract");
        }
        Ok(())
    }

    /// Bind the original schedule without serializing or aliasing live roles.
    pub fn bind_roles(self, mg: usize, vc: usize, sr: usize) -> R<RoleBinding> {
        if mg < 4 {
            return fail(
                "DSL.KERN.020",
                "attention motif needs four distinct MG roles; no two-MG schedule is available",
            );
        }
        let uses_sr = self == Self::Srflash;
        if uses_sr && sr < 2 {
            return fail("DSL.KERN.020", "srflash needs two distinct SR roles");
        }
        if !uses_sr && vc < 2 {
            return fail(
                "DSL.KERN.020",
                "flash motif needs two distinct vector roles",
            );
        }
        Ok(RoleBinding {
            scores: [0, 2],
            products: [1, 3],
            vectors: if uses_sr { vec![] } else { vec![0, 1] },
            sr: if uses_sr { vec![0, 1] } else { vec![] },
        })
    }

    pub fn pack(self) -> PackGeometry {
        match self {
            Self::Flash => PackGeometry {
                q_gm: 32,
                qk_nk: 1,
                k_gn: 128,
                v_gn: 16,
                v_nk: 16,
            },
            Self::Flashd => PackGeometry {
                q_gm: 32,
                qk_nk: 2,
                k_gn: 128,
                v_gn: 16,
                v_nk: 16,
            },
            Self::Srflash => PackGeometry {
                q_gm: 32,
                qk_nk: 2,
                k_gn: 128,
                v_gn: 8,
                v_nk: 32,
            },
        }
    }

    pub fn state(self) -> SoftmaxState {
        match self {
            Self::Flash | Self::Flashd => SoftmaxState::HalfPartials,
            Self::Srflash => SoftmaxState::SrRows,
        }
    }
}
