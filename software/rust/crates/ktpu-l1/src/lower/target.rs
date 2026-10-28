//! The machine an L1 program is read against, seen from one mesh: a card of
//! `nodes` meshes of the machine's die (meshes 0..nodes), or the one mesh
//! `mesh` (L1.UNIT.001-.004, L1.BODY.003).

use ktpu_machine::{Machine, UnitKind};

use super::image::Units;
use crate::error::{Error, Result};
use crate::mutant::on;
use crate::text::{fmt, Reader, Stmt, Term};

pub type Xy = (u32, u32);

#[derive(Clone, Debug)]
pub struct Target<'m> {
    pub machine: &'m Machine,
    /// The mesh indices the card has.
    pub meshes: Vec<u32>,
    /// The mesh this program runs on.
    pub mesh: u32,
}

impl<'m> Target<'m> {
    /// `machine` seen from mesh `mesh`; with `nodes`, a card of that many
    /// meshes.
    pub fn new(machine: &'m Machine, nodes: Option<u32>, mesh: u32) -> Target<'m> {
        let meshes = match nodes {
            Some(n) if n > 0 => (0..n).collect(),
            _ => vec![mesh],
        };
        Target {
            machine,
            meshes,
            mesh,
        }
    }

    /// This card seen from mesh `index`.
    pub fn for_mesh(&self, index: u32) -> Result<Target<'m>> {
        if on("l1.body/one-mesh") {
            return Ok(self.clone());
        }
        self.check(index)?;
        Ok(Target {
            mesh: index,
            ..self.clone()
        })
    }

    fn check(&self, index: u32) -> Result<()> {
        if self.meshes.contains(&index) {
            Ok(())
        } else {
            Err(Error::new(
                "L1.BODY.003",
                format!(
                    "machine {:?} has no mesh {index}; it has {:?}",
                    self.machine.name, self.meshes
                ),
            ))
        }
    }

    /// The sorted coordinates of `kind` on mesh `mesh`.
    pub fn coords(&self, kind: UnitKind, mesh: u32) -> Result<Vec<Xy>> {
        self.check(mesh)?;
        let mut out: Vec<Xy> = self
            .machine
            .units(kind)
            .iter()
            .map(|c| (c.0, c.1))
            .collect();
        if out.is_empty() {
            return Err(Error::new(
                "L1.UNIT.003",
                format!(
                    "machine {:?} mesh_{mesh} has no {:?} units",
                    self.machine.name,
                    kind.tag()
                ),
            ));
        }
        if on("l1.unit/unsorted") {
            out.reverse();
        } else {
            out.sort();
        }
        Ok(out)
    }

    /// The units of `kind` on this program's mesh, sorted; none is empty.
    pub fn units(&self, kind: UnitKind) -> Vec<Xy> {
        let mut out: Vec<Xy> = self
            .machine
            .units(kind)
            .iter()
            .map(|c| (c.0, c.1))
            .collect();
        out.sort();
        out
    }

    /// Mesh `mesh`'s memory ports.
    pub fn mem_ports(&self, mesh: u32) -> Result<Vec<Xy>> {
        self.check(mesh)?;
        Ok(self
            .machine
            .die
            .sys_ports
            .iter()
            .map(|c| (c.0, c.1))
            .collect())
    }

    /// Where mesh `mesh`'s orchestrator answers: its first memory port.
    pub fn agent(&self, mesh: u32) -> Result<Option<Xy>> {
        Ok(self.mem_ports(mesh)?.first().copied())
    }

    /// `(mesh, coord)` of a unit: `vc[i]`, `mg[i]`, `sr[i]` or `meshM.mg[i]`.
    pub fn resolve(&self, r: &Reader, st: &Stmt, t: &Term) -> Result<(u32, Xy)> {
        if let Term::View(name, slices) = t {
            if slices.len() == 1 {
                let (prefix, kind) = match name.rfind('.') {
                    Some(i) => (&name[..i], &name[i + 1..]),
                    None => ("", name.as_str()),
                };
                let mut mesh = self.mesh;
                if !prefix.is_empty() {
                    let digits = prefix
                        .strip_prefix("mesh")
                        .filter(|d| !d.is_empty() && d.chars().all(|c| c.is_ascii_digit()));
                    let Some(d) = digits else {
                        return Err(r.fail(
                            st,
                            "L1.UNIT.002",
                            format!("a unit in another mesh is meshM.{kind}[i], got {name}"),
                        ));
                    };
                    mesh = d.parse().unwrap_or(u32::MAX);
                }
                let kind = match kind {
                    "vc" => Some(UnitKind::Vc),
                    "mg" => Some(UnitKind::Mg),
                    "sr" => Some(UnitKind::Sr),
                    _ => None,
                };
                if let Some(kind) = kind {
                    let i = match &slices[0] {
                        Term::Int(v, _) => *v,
                        s => {
                            return Err(r.fail(
                                st,
                                "L1.UNIT.001",
                                format!("wanted a constant (parameters are bound), got {}", fmt(s)),
                            ))
                        }
                    };
                    let have = self.coords(kind, mesh)?;
                    if !(0..have.len() as i128).contains(&i) {
                        return Err(r.fail(
                            st,
                            "L1.UNIT.003",
                            format!(
                                "{name}[{i}]: this machine has {} in mesh {mesh}",
                                have.len()
                            ),
                        ));
                    }
                    return Ok((mesh, have[i as usize]));
                }
            }
        }
        Err(r.fail(
            st,
            "L1.UNIT.001",
            format!("wanted a unit vc[i] / mg[i] / meshM.mg[i], got {}", fmt(t)),
        ))
    }
}

/// Unit names in images and descriptors, resolved on a program's target.
pub struct MeshUnits<'a> {
    pub target: Target<'a>,
    pub reader: Reader,
}

impl Units for MeshUnits<'_> {
    fn resolve(&self, st: &Stmt, t: &Term) -> Result<(u32, Xy)> {
        self.target.resolve(&self.reader, st, t)
    }

    fn home(&self) -> u32 {
        self.target.mesh
    }
}
