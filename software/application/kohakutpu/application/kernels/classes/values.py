"""Serializable host arithmetic for row-class count and allocation values."""


def const(value):
    return {"op": "const", "value": value}


def param(name):
    return {"op": "param", "name": name}


def add(a, b):
    return {"op": "add", "a": a, "b": b}


def mul(a, b):
    return {"op": "mul", "a": a, "b": b}


def div(a, divisor):
    return {"op": "div", "a": a, "divisor": divisor}


def ceildiv(a, divisor):
    return div(add(a, const(divisor - 1)), divisor)


def transpose(info):
    a, b, batch = param("A:units16"), param("B:units16"), param("nb")
    area = mul(a, b)
    lines = mul(mul(batch, area), const(2))
    trips = ceildiv(lines, 8)
    cores = info["cores"]
    result = {
        "value/area": area,
        "value/lines": lines,
        "value/trips": trips,
        "value/padded": mul(ceildiv(trips, cores), const(cores * 8)),
    }
    for core in range(cores):
        result[f"value/core{core}_trips"] = div(
            add(trips, const(cores - 1 - core)), cores
        )
    return result


def walk(info):
    result = {}
    counts = [param(f"levels/{i}/0") for i in range(info["levels"])]
    total = const(1)
    for count in counts:
        total = mul(total, count)
    for name, dims, base, extent in (
        ("levels", counts, "at", "src_bytes"),
        (
            "dlevels",
            counts if info["levels"] == info["dlevels"] else [total],
            "dat",
            "dst_bytes",
        ),
    ):
        suffix = const(1)
        words = add(param(base + ":units32"), const(1))
        for i in reversed(range(len(dims))):
            result[f"value/{name}{i}_suffix"] = suffix
            result[f"value/{name}{i}_count"] = dims[i]
            stride = param(f"{name}/{i}/1:units32")
            positive = add(
                stride, mul({"op": "min", "a": stride, "b": const(0)}, const(-1))
            )
            words = add(words, mul(add(dims[i], const(-1)), positive))
            suffix = mul(suffix, dims[i])
        result[f"value/{name}_count"] = suffix
        result[f"value/{extent}"] = mul(
            add(words, param(f"walk/{extent}_tail")), const(32)
        )
    return result


def quant(info):
    tile, chunk = info["g"] * 4, info["nk"] * info["kblock"]
    ru, cu = (tile, chunk) if info["direct"] else (1, 16)
    rows = mul(param(f"quant/rows:units{ru}"), const(ru))
    cols = mul(param(f"quant/cols:units{cu}"), const(cu))
    rp = mul(add(ceildiv(rows, tile), param(f"quant/rpad:units{tile}")), const(tile))
    cp = mul(add(ceildiv(cols, chunk), param(f"quant/cpad:units{chunk}")), const(chunk))
    pitch = add(cols, mul(param("quant/pitch_extra:units16"), const(16)))
    batch = param("batches")
    return {
        "value/chunks": div(cp, chunk),
        "value/tile_rows": div(mul(batch, rp), tile),
        "value/padded_words": div(cp, 16),
        "value/rows": rows,
        "value/cols": cols,
        "value/rp": rp,
        "value/cp": cp,
        "value/pitch": pitch,
        "value/source_rows": add(
            mul(batch, rows),
            {
                "op": "quotient",
                "a": add(
                    mul(param("quant/offset:units16"), const(16)), add(pitch, const(-1))
                ),
                "b": pitch,
            },
        ),
        "value/padded_rows": mul(batch, rp),
        "value/output_elements": mul(mul(batch, rp), cp),
        "value/input_elements": mul(mul(batch, rows), pitch),
        "value/batch_input": mul(rows, pitch),
        "value/batch_output": mul(rp, cp),
        "value/valid_elements": mul(rows, cols),
    }


def mm(info):
    batch = param("mm/fields/bt")
    height = mul(param(f"mm/fields/mp:units{info['tm']}"), const(info["tm"]))
    width = mul(param(f"mm/fields/n:units{info['tn']}"), const(info["tn"]))
    chunk = info["nk"] * info["kblock"]
    depth = mul(param(f"mm/fields/kp:units{chunk}"), const(chunk))
    total_rows = mul(mul(batch, height), div(width, info["tn"]))
    trips = div(total_rows, 4)
    result = {
        "value/total_rows": total_rows,
        "value/trips": trips,
        "value/tile_rows": div(mul(batch, height), info["tm"]),
        "value/tiles_batch": mul(div(height, info["tm"]), div(width, info["tn"])),
        "value/tiles": div(total_rows, info["tm"]),
        "value/ak": mul(mul(batch, height), depth),
        "value/bk": mul(mul(batch, width), depth),
        "value/output_elements": mul(mul(batch, height), width),
        "value/output_extent": mul(mul(batch, height), param("out/fields/pitch")),
    }
    for core in range(info["cores"]):
        result[f"value/core{core}_trips"] = div(
            add(trips, const(info["cores"] - 1 - core)), info["cores"]
        )
    return result


def rows(info, *, aligned_width=False):
    """Recipes depend only on the recorded class plan, never package contents."""
    trip, cores = info["trip_rows"], info["cores"]
    width = mul(param("W:columns32"), const(32)) if aligned_width else param("W")
    row_count = ceildiv(param("n"), 128) if info.get("flat") else param("R")
    if info.get("flat"):
        width = const(128)
    row_trips = ceildiv(row_count, trip)
    trips = (
        mul(row_trips, div(width, info["width"]))
        if info.get("wide") and not info.get("fold")
        else row_trips
    )
    padded = mul(ceildiv(trips, cores), const(cores * trip))
    result = {
        "value/rows": row_count,
        "value/block_rows": mul(row_trips, const(trip)),
        "value/trips": trips,
        "value/padded_rows": padded,
        "value/stat_words": ceildiv(row_count, 16),
        "value/row_elements": mul(row_count, width),
    }
    for core in range(cores):
        if info.get("fold"):
            active = div(
                add(
                    {"op": "min", "a": row_trips, "b": const(cores)},
                    const(cores - 1 - core),
                ),
                cores,
            )
            result[f"value/core{core}_columns"] = mul(div(width, info["width"]), active)
        result[f"value/core{core}_trips"] = div(
            add(trips, const(cores - 1 - core)), cores
        )
    return result
