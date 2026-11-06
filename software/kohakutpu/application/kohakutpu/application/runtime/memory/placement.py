"""Graph-owned pins on main-heap spans, reused by allocation site and signature."""


class Placement:
    """Stable physical extents without reusing virtual handles or child heaps.

    Each planned call site has its own signature-indexed allocation sequence.
    A released span is leased again only after all other pins are released.
    Live forms keep their owners; closing the placement drops only graph pins.
    """

    def __init__(self, dev):
        self.dev = dev
        self.slots = {}
        self.references = []
        self.closed = False

    def alloc(self, site, cursors, nbytes, space, node, owner):
        if self.closed or self.dev.closed:
            raise RuntimeError("graph placement is closed")
        signature = (int(nbytes), space, node, owner)
        index = cursors.get(signature, 0)
        try:
            cursors[signature] = index + 1
            key = (site, signature, index)
            ref = self.slots.get(key)
            if ref is not None:
                arena, physical = ref
                # A queued scratch owner releases only when its completion is observed.
                conflicts = [s for s in self.dev.inflight if ref in s.references]
                if conflicts:
                    self.dev.collect_through(conflicts[-1])
                if physical in arena.released and arena.references[physical] == 1:
                    arena.reuse_pinned(physical)
                    line = self.dev.line_bytes
                    extent = max(line, (int(nbytes) + line - 1) // line * line)
                    try:
                        return self.dev.mmu.register(physical, extent, arena)
                    except BaseException:
                        arena.release(physical)
                        raise
            at = self.dev._alloc(nbytes, space, node, owner)
            prior = ref
            retained = None
            count = len(self.references)
            try:
                entry, physical = self.dev.mmu.resolve(at, owner=True)
                ref = (entry.arena, physical)
                entry.arena.retain(physical)
                retained = ref
                self.references.append(ref)
                self.slots[key] = ref
                return at
            except BaseException:
                if prior is None:
                    self.slots.pop(key, None)
                else:
                    self.slots[key] = prior
                del self.references[count:]
                if retained is not None:
                    retained[0].unretain(retained[1])
                self.dev.free(at)
                raise
        except BaseException:
            if index:
                cursors[signature] = index
            else:
                cursors.pop(signature, None)
            raise

    @property
    def nbytes(self):
        """Reserved physical bytes, counted once per graph-owned span."""
        return sum(a.live.get(at, 0) for a, at in self.references)

    def close(self):
        if not self.closed:
            for arena, at in self.references:
                arena.unretain(at)
            self.references.clear()
            self.slots.clear()
            self.closed = True

    def __del__(self):
        self.close()
