"""2D convolution with spatial padding, 5D mover unfold, in-kernel quant and epilogue."""

from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native.composition import (
    GraphKernel,
    compose,
    sequence,
)
from kohakutpu.language import dsl as kt
from kohakutpu.stack.kernel import dsl_machine


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
):
    """Fused unfold + in-kernel quant + ring contraction + epilogue in one package."""
    if cout_pad % 32:
        raise ValueError("padded output channels must be a multiple of 32")
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    k_pad = -(-(kh * kw * cin_pad) // 32) * 32
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
        chunks = []
        for b0 in range(0, batch, chunk_b):
            u_graph = (
                unfold_kernel(
                    chunk_b,
                    height,
                    width,
                    cin_pad,
                    kh,
                    kw,
                    stride,
                    padding,
                    batch_offset=b0,
                    total_batch=batch,
                )
                .trace(
                    {"x": (batch * height * width, cin_pad)},
                    machine=dsl_machine(machine),
                )
                .graph
            )
            u_graph = dict(
                u_graph,
                scratch=[
                    dict(s, spec=dict(s["spec"], space="stage"))
                    for s in u_graph["scratch"]
                ],
            )
            p_graph = G.linear_graph(
                chunk_b * per_batch,
                k_pad,
                cout_pad,
                machine,
                activation,
                residual,
                ring_depth,
                out_offset=b0 * per_batch * cout_pad,
                out_rows=rows_pad,
            )
            chunks.append(compose(u_graph, p_graph, "out", ("a",), "stage"))
        return GraphKernel(sequence(chunks, space="stage"))
    unfold = (
        unfold_kernel(batch, height, width, cin_pad, kh, kw, stride, padding)
        .trace({"x": (batch * height * width, cin_pad)}, machine=dsl_machine(machine))
        .graph
    )
    patch_space = (
        "stage" if edge_space == "stage" and rows_pad * k_pad * 2 <= 665600 else "dram"
    )
    unfold = dict(
        unfold,
        scratch=[
            dict(s, spec=dict(s["spec"], space=patch_space)) for s in unfold["scratch"]
        ],
    )
    eff_ring = (
        1
        if patch_space == "stage"
        and rows_pad * k_pad * 3 + ring_depth * machine.count("MG") * 25600 > 1048576
        else ring_depth
    )
    product = G.linear_graph(
        rows_pad, k_pad, cout_pad, machine, activation, residual, eff_ring
    )
    return GraphKernel(compose(unfold, product, "out", ("a",), patch_space))
