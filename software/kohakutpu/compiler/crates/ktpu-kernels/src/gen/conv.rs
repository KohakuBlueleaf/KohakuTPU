//! Destination-owned BHWC convolution patches.

use super::vector::{self, Operand};
use super::Hw;
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::split::{PlannedKernel, SplitRequest};
use crate::trace::buffer::f16;
use crate::trace::{fail, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde::{Deserialize, Serialize};

/// Channels are physical BHWC lanes. Groups occupy contiguous channel slices.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ConvSpec {
    pub batch: i64,
    pub height: i64,
    pub width: i64,
    pub cin: i64,
    pub cout: i64,
    pub kh: i64,
    pub kw: i64,
    pub stride: [i64; 2],
    pub padding: [i64; 2],
    pub dilation: [i64; 2],
    pub groups: i64,
}

#[derive(Clone, Copy)]
struct Geometry {
    oh: i64,
    ow: i64,
    rows: i64,
    channels: i64,
    k: i64,
    kp: i64,
    source: i64,
}

fn product(values: &[i64]) -> R<i64> {
    values.iter().try_fold(1i64, |a, &b| {
        a.checked_mul(b)
            .ok_or_else(|| ktpu_machine::Refusal::new("API.OP.001", "convolution extent overflow"))
    })
}

impl ConvSpec {
    fn geometry(&self, m: &Machine) -> R<Geometry> {
        if [
            self.batch,
            self.height,
            self.width,
            self.cin,
            self.cout,
            self.kh,
            self.kw,
            self.groups,
            self.stride[0],
            self.stride[1],
            self.dilation[0],
            self.dilation[1],
        ]
        .iter()
        .any(|&n| n <= 0)
            || self.padding.iter().any(|&n| n < 0)
            || self.cin % self.groups != 0
            || self.cout % self.groups != 0
        {
            return fail("API.OP.001", "invalid convolution domain");
        }
        let channels = self.cin / self.groups;
        let hw = Hw::of(m);
        if channels % (hw.word_bytes / 2) != 0 {
            return fail(
                "API.OP.001",
                "BHWC patch lanes must occupy whole mover words",
            );
        }
        let extent = |size: i64, kernel: i64, axis: usize| -> R<i64> {
            let effective = product(&[kernel - 1, self.dilation[axis]])?.checked_add(1);
            let padded = product(&[2, self.padding[axis]])?.checked_add(size);
            match (padded, effective) {
                (Some(padded), Some(effective)) if padded >= effective => {
                    Ok((padded - effective) / self.stride[axis] + 1)
                }
                _ => fail("API.OP.001", "invalid convolution spatial extent"),
            }
        };
        let oh = extent(self.height, self.kh, 0)?;
        let ow = extent(self.width, self.kw, 1)?;
        let rows = product(&[self.batch, oh, ow])?;
        let k = product(&[self.kh, self.kw, channels])?;
        let kp = k
            .checked_add(hw.kblock - 1)
            .map(|k| k / hw.kblock * hw.kblock);
        let Some(kp) = kp else {
            return fail("API.OP.001", "convolution patch extent overflow");
        };
        let source = product(&[self.batch, self.height, self.width, self.cin])?;
        product(&[source, 2])?;
        product(&[self.groups, rows, kp, 2])?;
        Ok(Geometry {
            oh,
            ow,
            rows,
            channels,
            k,
            kp,
            source,
        })
    }
}

/// Canonical destination is [group * B * Ho * Wo, Kpad]. A share owns
/// complete patch rows. Padding and halo reads do not create other writers.
pub fn patch_kernel(spec: &ConvSpec, first: i64, count: i64, m: &Machine) -> R<Kernel> {
    let geom = spec.geometry(m)?;
    let total = spec.groups * geom.rows;
    if first < 0 || count < 0 || first > total || count > total - first {
        return fail("API.OP.001", "patch share outside convolution domain");
    }
    let input = [geom.source / spec.cin, spec.cin];
    let output = [total, geom.kp];
    let ports = vec![
        Port::input("x", &input, g::Dtype::F16),
        Port::output("patches", &output, g::Dtype::F16),
    ];
    let given = vec![
        ("x".into(), f16(&input)?),
        ("patches".into(), f16(&output)?),
    ];
    let key = name(
        "conv_patches",
        vec![
            Py::str(&format!("{spec:?}")),
            Py::Int(first),
            Py::Int(count),
        ],
    );
    build(&key, m, ports, given, |t, buffers| {
        let hw = Hw::of(m);
        let words = geom.channels * 2 / hw.word_bytes;
        let mut row = first;
        while row < first + count {
            let group = row / geom.rows;
            let spatial = row % geom.rows;
            let batch = spatial / (geom.oh * geom.ow);
            let oh = spatial / geom.ow % geom.oh;
            let ow = spatial % geom.ow;
            let end = (first + count - row).min(geom.ow - ow);
            for kh in 0..spec.kh {
                let ih = oh * spec.stride[0] - spec.padding[0] + kh * spec.dilation[0];
                for kw in 0..spec.kw {
                    let base = kw * spec.dilation[1] - spec.padding[1];
                    // Half-open output-column interval whose source coordinates are in bounds.
                    let lo = (-base).div_euclid(spec.stride[1]);
                    let lo = lo + i64::from(lo * spec.stride[1] < -base);
                    let hi = (spec.width - 1 - base).div_euclid(spec.stride[1]) + 1;
                    let valid_lo = lo.max(ow).min(ow + end);
                    let valid_hi = hi.max(valid_lo).min(ow + end);
                    let (valid_lo, valid_hi) = if (0..spec.height).contains(&ih) {
                        (valid_lo, valid_hi)
                    } else {
                        (ow, ow)
                    };
                    for (begin, stop, valid) in [
                        (ow, valid_lo, false),
                        (valid_lo, valid_hi, true),
                        (valid_hi, ow + end, false),
                    ] {
                        if begin == stop {
                            continue;
                        }
                        let dat = ((row + begin - ow) * geom.kp
                            + (kh * spec.kw + kw) * geom.channels)
                            * 2;
                        let dst = [(stop - begin, geom.kp * 2), (words, hw.word_bytes)];
                        if valid {
                            let iw = begin * spec.stride[1] + base;
                            let at = (((batch * spec.height + ih) * spec.width + iw) * spec.cin
                                + group * geom.channels)
                                * 2;
                            let src = [
                                (stop - begin, spec.stride[1] * spec.cin * 2),
                                (words, hw.word_bytes),
                            ];
                            t.copy(
                                &buffers[1].whole(),
                                &buffers[0].whole(),
                                Some((&src, Some(at))),
                                Some((&dst, Some(dat))),
                            )?;
                        } else {
                            t.fill_value(&buffers[1].whole(), 0, None, Some((&dst, Some(dat))))?;
                        }
                    }
                }
            }
            if geom.k < geom.kp {
                let dst = [
                    (end, geom.kp * 2),
                    ((geom.kp - geom.k) * 2 / hw.word_bytes, hw.word_bytes),
                ];
                t.fill_value(
                    &buffers[1].whole(),
                    0,
                    None,
                    Some((&dst, Some((row * geom.kp + geom.k) * 2))),
                )?;
            }
            row += end;
        }
        Ok(())
    })
}

/// The producer partitions destinations independently of the product's channel
/// readers. A channel split shares this one patch allocation, not P producers.
pub fn patch_split(spec: &ConvSpec, request: &SplitRequest, m: &Machine) -> R<PlannedKernel> {
    let geom = spec.geometry(m)?;
    let rows = spec.groups * geom.rows;
    let shape = vector::split::Shape {
        rows,
        quantum: 1,
        reads: vec![vector::split::Tensor {
            name: "x".into(),
            operand: Operand::new(vector::Kind::Col),
            width: geom.source,
            planes: vec![0],
        }],
        writes: vec![vector::split::Tensor {
            name: "patches".into(),
            operand: Operand::full(geom.kp, rows),
            width: geom.kp,
            planes: vec![0],
        }],
        tokens: false,
    };
    let signature = patch_kernel(spec, 0, 1, m)?;
    vector::split::kernel(&shape, signature, request, m, |first, count| {
        patch_kernel(spec, first, count, m)
    })
}

/// One-owner reference uses the identical patch destination walk.
pub fn patches(spec: &ConvSpec, m: &Machine) -> R<Kernel> {
    let geom = spec.geometry(m)?;
    patch_kernel(spec, 0, spec.groups * geom.rows, m)
}

/// Product epilogues and weight packing remain matmul contracts. The caller
/// supplies their recorded arguments; spatial dimensions come from ConvSpec.
/// Output and affine views use matmul's group-major layout: group origins are
/// separated by the planned padded M extent, not the logical spatial extent.
pub fn kernel(
    spec: &ConvSpec,
    product: &serde_json::Value,
    request: &SplitRequest,
    m: &Machine,
) -> R<PlannedKernel> {
    use crate::split::{Region, ShardRole, SplitAxis};
    use serde_json::json;

    let geom = spec.geometry(m)?;
    let mut args = product.clone();
    let Some(object) = args.as_object_mut() else {
        return fail(
            "API.OP.001",
            "convolution product must be recorded matmul arguments",
        );
    };
    object.insert("activation_any".into(), json!(true));
    let mut mm = object
        .get("mm")
        .cloned()
        .unwrap_or_else(|| json!({"made_by": {"gen": "matmul.plan", "args": {}}}));
    let Some(shape) = mm
        .pointer_mut("/made_by/args")
        .and_then(serde_json::Value::as_object_mut)
    else {
        return fail("API.OP.001", "convolution product needs a matmul.plan call");
    };
    shape.insert("bt".into(), json!(spec.groups));
    shape.insert("m".into(), json!(geom.rows));
    shape.insert("n".into(), json!(spec.cout / spec.groups));
    shape.insert("k".into(), json!(geom.kp));
    shape.insert("shared".into(), json!(false));
    object.insert("mm".into(), mm);
    let mut plans = Vec::new();
    let e = crate::expr::from_table(&args["e"])?;
    let out = crate::call::operand(&args["out"])?;
    let mut axes = vec![("spatial", "m", SplitAxis::Spatial)];
    if crate::expr::reduces(&e).is_empty() && out.kind != vector::Kind::Row {
        axes.push(("channels", "n", SplitAxis::Channels));
    }
    if spec.groups > 1 {
        axes.push(("groups", "groups", SplitAxis::Groups));
    }
    for (id, product_id, axis) in axes {
        if request.force.as_deref().is_some_and(|forced| forced != id) {
            continue;
        }
        let mut stage_request = request.clone();
        stage_request.force = Some(product_id.into());
        let mut plan = crate::call::call_split_with_timing(
            "matmul.mm_kernel",
            &args,
            &stage_request,
            m,
            true,
        )?;
        let signature = patch_kernel(spec, 0, 1, m)?;
        let (producer, owners) = if product_id == "n" {
            stage_request.force = Some("rows".into());
            let patch = patch_split(spec, &stage_request, m)?;
            (patch.kernel, patch.split.output.ownership)
        } else {
            let mut kernels = Vec::new();
            let mut owners = Vec::new();
            for share in &plan.split.shards {
                if share.role == ShardRole::Empty || share.valid_extent[0] == 0 {
                    continue;
                }
                let first = share.logical_origin[0] * geom.rows + share.logical_origin[1];
                let count = share.valid_extent[0];
                kernels.push(crate::kernel::on_node(
                    request.nodes.ids()[share.logical_node],
                    || patch_kernel(spec, first, count, m),
                )?);
                owners.push((
                    share.logical_node,
                    Region {
                        allocation: "patches".into(),
                        bytes: (first * geom.kp * 2) as u64..((first + count) * geom.kp * 2) as u64,
                    },
                ));
            }
            (super::chain::shares(&signature, kernels)?, owners)
        };
        let product_scratch: Vec<_> = plan
            .kernel
            .graph
            .scratch
            .iter()
            .map(|s| s.name.clone())
            .collect();
        let mut composed = super::chain::kernel(producer, plan.kernel, "a", "patches", false)?;
        for scratch in &mut composed.graph.scratch {
            if scratch.name == "patches" {
                scratch.spec.space = g::BufSpace::Dram;
            }
        }
        let Some(names) = composed.graph.consts["chain"]["ports"].as_object() else {
            return fail(
                "API.OP.001",
                "convolution composition has no product port map",
            );
        };
        let mut names: std::collections::BTreeMap<String, String> = names
            .iter()
            .filter_map(|(old, new)| new.as_str().map(|new| (old.clone(), new.into())))
            .collect();
        names.insert("a".into(), "patches".into());
        let scratch_start = composed.graph.scratch.len() - product_scratch.len();
        for (old, new) in product_scratch
            .iter()
            .zip(&composed.graph.scratch[scratch_start..])
        {
            names.insert(old.clone(), new.name.clone());
        }
        let rename = |region: &mut Region| {
            if let Some(name) = names.get(&region.allocation) {
                region.allocation = name.clone();
            }
        };
        for region in &mut plan.split.output.valid_region {
            rename(region);
        }
        for (_, region) in &mut plan.split.output.ownership {
            rename(region);
        }
        for (name, _) in &mut plan.split.output.tensors {
            if let Some(new) = names.get(name.as_str()) {
                *name = new.clone();
            }
        }
        let mut local_cycles = vec![0; request.nodes.ids().len()];
        let mut product_cycles = plan.split.cost.local_cycles.iter();
        for (rank, cycles) in local_cycles.iter_mut().enumerate() {
            if plan
                .split
                .shards
                .iter()
                .any(|s| s.logical_node == rank && s.role != ShardRole::Empty)
            {
                *cycles = product_cycles.next().copied().ok_or_else(|| {
                    ktpu_machine::Refusal::new("L2.COST.001", "missing convolution product timing")
                })?;
            }
        }
        for (rank, region) in &owners {
            let row_bytes = (geom.kp * 2) as u64;
            let first = (region.bytes.start / row_bytes) as i64;
            let count = ((region.bytes.end - region.bytes.start) / row_bytes) as i64;
            let patch = patch_kernel(spec, first, count, m)?;
            local_cycles[*rank] += ktpu_graph::cycles(&patch.graph, m)? - m.vc.launch_front as i64;
        }
        let mut attached = vec![false; request.nodes.ids().len()];
        for share in &mut plan.split.shards {
            for region in share
                .read_regions
                .iter_mut()
                .chain(&mut share.write_regions)
                .chain(&mut share.scratch_regions)
            {
                rename(region);
            }
            let rank = share.logical_node;
            if !attached[rank] && owners.iter().any(|(owner, _)| *owner == rank) {
                attached[rank] = true;
                let owned: Vec<_> = owners
                    .iter()
                    .filter(|(owner, _)| *owner == rank)
                    .map(|(_, r)| r.clone())
                    .collect();
                share.scratch_regions.extend(owned.clone());
                share.write_regions.extend(owned);
                share.read_regions.push(Region {
                    allocation: "x".into(),
                    bytes: 0..(geom.source * 2) as u64,
                });
                if share.role == ShardRole::Empty {
                    share.role = ShardRole::Compute;
                    share.local_schedule = "patch-producer".into();
                }
            }
        }
        crate::split::validate_ownership(&crate::split::OutputLayout {
            valid_region: vec![Region {
                allocation: "patches".into(),
                bytes: 0..(spec.groups * geom.rows * geom.kp * 2) as u64,
            }],
            ownership: owners,
            ..Default::default()
        })?;
        let active: Vec<_> = (0..request.nodes.ids().len())
            .filter(|rank| {
                plan.split
                    .shards
                    .iter()
                    .any(|s| s.logical_node == *rank && s.role != ShardRole::Empty)
            })
            .collect();
        plan.split.active_nodes = active.len();
        plan.split.cost.local_cycles = active.into_iter().map(|rank| local_cycles[rank]).collect();
        plan.split.id = id.into();
        plan.split.axis = axis;
        plan.split.cost.makespan = ktpu_graph::cycles(&composed.graph, m)?;
        plan.split.cost.provenance = "ktpu-l2 convolution patch/product sequence".into();
        plan.kernel = composed;
        plan.request = request.clone();
        plans.push(plan);
    }
    let selected = crate::split::cost::select_split(
        plans.iter().map(|p| p.split.clone()).collect(),
        request.force.as_deref(),
    )?;
    plans
        .into_iter()
        .find(|p| p.split.id == selected.id)
        .ok_or_else(|| ktpu_machine::Refusal::new("DSL.KERN.020", "no convolution option emitted"))
}
