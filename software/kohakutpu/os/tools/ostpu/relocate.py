"""Bind a trusted capture's physical spans into a chosen execution arena."""

from dataclasses import replace


def relocate(events, oldbase, base, extent):
    def address(pa):
        if not oldbase <= pa < oldbase + extent:
            raise ValueError(f"capture address outside tensor arena: {pa:#x}")
        return base + pa - oldbase

    output = []
    for event in events:
        if event[0] == "write":
            output.append(("write", address(event[1]), event[2]))
        else:
            programs = {}
            for node, p in event[1].items():
                if p.translations is not None:
                    translations = tuple(
                        (va, address(pa), size) for va, pa, size in p.translations
                    )
                    bound = replace(p, translations=translations).bound(False)
                else:
                    bound = replace(
                        p, bindings=tuple(address(pa) for pa in p.bindings)
                    ).bound(False)
                programs[node] = (bound, ())
            output.append(("run", programs))
    return output
