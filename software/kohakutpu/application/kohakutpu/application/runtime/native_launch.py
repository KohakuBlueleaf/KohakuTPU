"""Cold construction of native launch descriptors from the admitted submission."""

from kohakutpu.compiler import _rs


def descriptor(dev, compiled, full, progs, submitted, explicit):
    names = tuple(full)
    scratch = []
    allocation_index = {}
    bindings = []
    for name, address in full.items():
        if name in explicit:
            bindings.append((0, tuple(explicit).index(name), 0))
            continue
        allocation, _ = dev.mmu.resolve(address)
        if allocation.virtual not in allocation_index:
            allocation_index[allocation.virtual] = len(scratch)
            node = next(
                (
                    n
                    for n, arena in dev.memory.staging.items()
                    if arena is allocation.arena
                ),
                None,
            )
            space = "dram" if node is None else "stage"
            scratch.append(
                (
                    submitted.extents[allocation.virtual],
                    space,
                    node,
                    f"scratch:{compiled.name}",
                )
            )
        bindings.append(
            (1, allocation_index[allocation.virtual], address - allocation.virtual)
        )
    packages = []
    for mesh, p in progs.items():
        mesh = dev.card.nodes[0] if mesh is None else mesh
        native = _rs.LaunchRecipe(
            p.package,
            p.machine_json,
            mesh,
            p.template_json,
            p.template_params,
            p.images_json,
            (dev.recipe_epoch, dev.recipe_epoch.generation),
            wire=dev.wire_recipe,
        )
        slots = []
        for handle in p.bindings:
            slots.append(
                next(i for i, name in enumerate(names) if full[name] == handle)
            )
        packages.append((mesh, native, slots))
    return _rs.NativeSubmit(
        compiled.name,
        submitted.kind,
        submitted.info,
        scratch,
        bindings,
        packages,
        dev.resident_mode.resident,
        (dev.recipe_epoch, dev.recipe_epoch.generation),
    )
