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
