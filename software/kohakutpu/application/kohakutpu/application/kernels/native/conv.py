"""2D convolution with spatial padding, 5D mover unfold, in-kernel quant and epilogue."""

import json
from dataclasses import asdict

from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.template import GraphKernel, TemplateCall
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import generated
from kohakutpu.language import dsl as kt
from kohakutpu.stack import rs


def unfold_kernel(
    batch,
    height,
    width,
    cin_pad,
    kh,
    kw,
    stride=1,
    padding=0,
    *,
    batch_offset=0,
    total_batch=None,
):
    """Unfold [B, H, W, Cin_pad] (Cin_pad a multiple of 16) into [rows_pad, K_pad]."""
    if cin_pad % 16:
        raise ValueError("channel lane axis must be a multiple of 16")
    total_batch = batch if total_batch is None else total_batch
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    k_logical = kh * kw * cin_pad
    k_pad = -(-k_logical // 32) * 32
    words = cin_pad // 16
    ports = {
        "x": kt.In(total_batch * height * width, cin_pad),
        "out": kt.Out(rows_pad, k_pad),
    }
    src_at = batch_offset * height * width * cin_pad * 2

    def body(x, out):
        src = x
        if padding:
            src = kt.buffer((batch * hp * wp, cin_pad), "f16")
            kt.fill_value(src, 0)
            kt.copy(
                src,
                x,
                at=src_at,
                walk=((batch * height * width * words, 32),),
                dwalk=(
                    (batch, hp * wp * cin_pad * 2),
                    (height, wp * cin_pad * 2),
                    (width * words, 32),
                ),
                dat=(padding * wp + padding) * cin_pad * 2,
            )
        kt.fill_value(out, 0)
        kt.copy(
            out,
            src,
            at=0 if padding else src_at,
            walk=(
                (batch, hp * wp * cin_pad * 2),
                (oh, stride * wp * cin_pad * 2),
                (ow, stride * cin_pad * 2),
                (kh, wp * cin_pad * 2),
                (kw * words, 32),
            ),
            dwalk=((rows, k_pad * 2), (kh * kw * words, 32)),
        )

    return kt.Kernel.build("native_conv_unfold", body, ports)


def kernel(
    batch,
    height,
    width,
    cin_pad,
    cout_pad,
    kh,
    kw,
    machine,
    *,
    stride=1,
    padding=0,
    activation="none",
    residual=False,
    ring_depth=2,
    edge_space="stage",
    nodes=None,
):
    """Fused unfold + in-kernel quant + ring contraction + epilogue in one package."""
    if cout_pad % 32:
        raise ValueError("padded output channels must be a multiple of 32")
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    k_pad = -(-(kh * kw * cin_pad) // 32) * 32
    if nodes is not None:
        extras = [Operand("col")]
        if residual:
            extras.append(Operand("full", pitch=cout_pad, rows=rows_pad))
        arguments = {
            "spec": {
                "batch": batch,
                "height": height,
                "width": width,
                "cin": cin_pad,
                "cout": cout_pad,
                "kh": kh,
                "kw": kw,
                "stride": [stride, stride],
                "padding": [padding, padding],
                "dilation": [1, 1],
                "groups": 1,
            },
            "product": {
                "e": expr(L.epilogue(activation, residual)),
                "extras": [asdict(o) for o in extras],
                "out": asdict(Operand("full", pitch=cout_pad, rows=rows_pad)),
                "ring_depth": ring_depth,
                "tail_bands": True,
                "expr_cse": True,
            },
        }
        source, selection = generated.split_graph(
            "conv.kernel",
            json.dumps(arguments, sort_keys=True),
            rs.machine_json(machine),
            rs._MUTANT.get(),
            tuple(nodes),
            generated.split_option("conv.kernel"),
        )
        return GraphKernel(
            json.loads(source), selection={**selection, "nodes": tuple(nodes)}
        )
    per_batch = oh * ow
    k_logical = kh * kw * cin_pad
    if (
        edge_space == "stage"
        and rows_pad * k_pad * 2 > 665600
        and k_logical == k_pad
        and per_batch % 4 == 0
        and per_batch * k_pad * 2 <= 665600
    ):
        chunk_b = max(
            b
            for b in range(1, batch + 1)
            if batch % b == 0 and b * per_batch * k_pad * 2 <= 665600
        )
        members = []
        for b0 in range(0, batch, chunk_b):
            call = local_call(
                batch,
                height,
                width,
                cin_pad,
                cout_pad,
                kh,
                kw,
                machine,
                stride=stride,
                padding=padding,
                activation=activation,
                residual=residual,
                ring_depth=ring_depth,
                patch_space="stage",
                first_batch=b0,
                batches=chunk_b,
                out_rows=rows_pad,
            )
            members.append({"gen": call.generator, "args": call.arguments})
        call = TemplateCall("kernel.sequence", {"members": members})
        return GraphKernel(
            G.graph(call.generator, call.arguments, machine), template_call=call
        )
    patch_space = (
        "stage" if edge_space == "stage" and rows_pad * k_pad * 2 <= 665600 else "dram"
    )
    eff_ring = (
        1
        if patch_space == "stage"
        and rows_pad * k_pad * 3 + ring_depth * machine.count("MG") * 25600 > 1048576
        else ring_depth
    )
    call = local_call(
        batch,
        height,
        width,
        cin_pad,
        cout_pad,
        kh,
        kw,
        machine,
        stride=stride,
        padding=padding,
        activation=activation,
        residual=residual,
        ring_depth=eff_ring,
        patch_space=patch_space,
        first_batch=0,
        batches=batch,
        out_rows=rows_pad,
    )
    return GraphKernel(
        G.graph(call.generator, call.arguments, machine), template_call=call
    )


def local_call(
    batch,
    height,
    width,
    cin,
    cout,
    kh,
    kw,
    machine,
    *,
    stride,
    padding,
    activation,
    residual,
    ring_depth,
    patch_space,
    first_batch,
    batches,
    out_rows,
):
    """One retained bulk patch/product chunk, with global input and output views."""
    oh = (height + 2 * padding - kh) // stride + 1
    ow = (width + 2 * padding - kw) // stride + 1
    capacity = -(-(batches * oh * ow) // 4) * 4
    kpad = -(-(kh * kw * cin) // 32) * 32
    unfold = TemplateCall(
        "conv.unfold_kernel",
        {
            "spec": {
                "batch": batch,
                "height": height,
                "width": width,
                "cin": cin,
                "cout": cout,
                "kh": kh,
                "kw": kw,
                "stride": [stride, stride],
                "padding": [padding, padding],
                "dilation": [1, 1],
                "groups": 1,
            },
            "first_batch": first_batch,
            "batches": batches,
            "capacity": capacity,
            "scratch_space": patch_space,
        },
    )
    product = G.linear_call(
        capacity,
        kpad,
        cout,
        machine,
        activation,
        residual,
        ring_depth,
        out_offset=first_batch * oh * ow * cout,
        out_rows=out_rows,
    )
    call = G.chain_call(
        unfold,
        product,
        "a",
        "out",
        [("first", "x", "p_x")]
        + [
            ("second", name, "c_" + name)
            for name in ["b", "in1"] + (["in2"] if residual else []) + ["out"]
        ],
    )
    call.arguments["link_space"] = patch_space
    return call
