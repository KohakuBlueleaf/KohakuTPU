"""Device memory: allocation, spill, windows, host read and write."""

from contextlib import contextmanager

from kohakutpu.application.runtime.arena import OutOfMemory


class Allocation:
    # ------------------------------------------------------------ memory
    def alloc(
        self,
        nbytes: int,
        space: str = "dram",
        node: int | None = None,
        owner: str = "tensor",
    ) -> int:
        if self._placement is not None:
            pool, site, cursors = self._placement
            address = pool.alloc(site, cursors, nbytes, space, node, owner)
        else:
            address = self._alloc(nbytes, space, node, owner)
        if self._recipe_recording is not None:
            self._recipe_recording.on_alloc(address, nbytes, space, node, owner)
        return address

    def _alloc(self, nbytes, space, node, owner):
        line = self.line_bytes
        extent = max(line, (int(nbytes) + line - 1) // line * line)
        if space == "stage":
            n = self.card.nodes[0] if node is None else node
            arena = self.memory.staging[n]
            physical = self._take(lambda: self.memory.stage(extent, n, owner=owner))
        else:
            arena = self.dram
            physical = self._take(lambda: self.memory.alloc(extent, owner=owner))
        arena.requested[physical] = int(nbytes)
        try:
            return self.mmu.register(physical, extent, arena)
        except BaseException:
            arena.release(physical)
            raise

    def _window(self, key: tuple, nbytes: int) -> int:
        """The scratch window of `key`, `(space, node)`, at least `nbytes`:
        where every kernel run lays out its scratch in that space, a node's
        staging store each, DRAM one (`node` None). A node runs its queue in
        order and `_fence` keeps the runs in flight on one node, a run over
        several alone, so no two runs' scratch is live at once, eager or
        replayed, and a graph pins one window a space, not a span a site. A window too small is freed to
        whatever still pins it (runs in flight, recordings)."""
        held = self.windows.get(key)
        if held is not None and held[1] >= nbytes:
            return held[0]
        if held is not None:
            self.free(held[0])
        space, node = key
        at = self._alloc(nbytes, space, node, f"scratch:window:{space}:{node}")
        self.windows[key] = (at, nbytes)
        return at

    def _release_windows(self) -> None:
        """Every scratch window no recording pins freed, once no run is in
        flight (API.MEM.007: a collected run's scratch is not kept)."""
        for key, (at, _) in list(self.windows.items()):
            entry, physical = self.mmu.resolve(at, owner=True)
            if not entry.arena.references[physical]:
                self.free(at)
                del self.windows[key]

    def _extents(self, spans, windowed) -> dict:
        """Each window's address with the bytes the run of `spans`, at
        `windowed`, lays out in it (a window can be larger)."""
        out = {}
        for name, nbytes, *_ in spans:
            at = self.mmu.resolve(windowed[name])[0].virtual
            out[at] = max(out.get(at, 0), windowed[name] - at + int(nbytes))
        return out

    def _windowed(self, spans) -> dict:
        """Each scratch span of one run, `(name, bytes, space, node,
        members)`, at its address in its window: laid out from the window's
        start in order, each at a whole line."""
        line = max(self.line_bytes, 256)
        offsets, ends = {}, {}
        for name, nbytes, space, node, _ in spans:
            if space == "stage":
                key = (space, self.card.nodes[0] if node is None else node)
            else:
                key = (space, None)
            offsets[name] = (key, ends.get(key, 0))
            ends[key] = offsets[name][1] + -(-int(nbytes) // line) * line
        bases = {key: self._window(key, end) for key, end in ends.items()}
        return {name: bases[key] + off for name, (key, off) in offsets.items()}

    def _spill(self, node: int, size: int) -> int:
        """A queue's package place: the arena's, for `node`."""
        near = self.memory.near(node)
        return self._take(lambda: self.memory.alloc(size, "package", near))

    def _take(self, alloc) -> int:
        """Retry after each oldest completion, then evict unused packages."""
        while True:
            try:
                return alloc()
            except OutOfMemory:
                if self.inflight:
                    self.collect(self.inflight[0])
                    continue
                self.card.drop_spilled()
                return alloc()

    def _retain(self, addresses) -> list:
        held = []
        try:
            for addr in set(addresses):
                entry, _ = self.mmu.resolve(addr, owner=True)
                arena, at = entry.arena, entry.physical
                if (arena, at) not in held:
                    arena.retain(at)
                    held.append((arena, at))
        except BaseException:
            for arena, at in held:
                arena.unretain(at)
            raise
        return held

    def _pin_record(self, addresses):
        if self.recording:
            for ref in self._retain(addresses):
                if ref in self._record_pins:
                    ref[0].unretain(ref[1])
                else:
                    self._record_pins.append(ref)

    def free(self, addr: int) -> None:
        if self.closed:
            return
        if self._recipe_recording is not None:
            self._recipe_recording.on_free(addr)
        entry, physical = self.mmu.resolve(addr, owner=True)
        if physical != entry.physical:
            raise ValueError("RUL.023: only an allocation's base can be freed")
        entry.arena.release(physical)
        self.mmu.retire(addr)

    def write(self, addr: int, raw: bytes) -> None:
        if self._recipe_recording is not None:
            self._recipe_recording.on_write(addr, raw)
        self.wait_address(addr, write=True)
        self._pin_record([addr])
        raw = bytes(raw)
        # Whole host lines: the memory endpoint fills a short line's rest
        # (a JTAG write), or the host reads it to merge (an XDMA write).
        pad = -len(raw) % self.card.line
        entry, physical = self.mmu.resolve(addr, len(raw))
        if (
            physical - entry.physical + len(raw) + pad
            > entry.arena.live[entry.physical]
        ):
            raise ValueError("host write line exceeds allocation reservation")
        self.card.write(physical, raw + bytes(pad))
        self.counters["bytes_in"] += len(raw)

    def read(self, addr: int, nbytes: int) -> bytes:
        if self._recipe_recording is not None:
            self._recipe_recording.on_read()
        self.wait_address(addr)
        self._pin_record([addr])
        n = -(-int(nbytes) // self.card.line) * self.card.line
        self.counters["bytes_out"] += nbytes
        entry, physical = self.mmu.resolve(addr, nbytes)
        if physical - entry.physical + n > entry.arena.live[entry.physical]:
            raise ValueError("host read line exceeds allocation reservation")
        return bytes(self.card.get(physical, n))[:nbytes]

    @contextmanager
    def placement_scope(self, pool, site):
        """One graph call site's allocation order, independent of other sites."""
        old = self._placement
        self._placement = None if pool is None else (pool, site, {})
        try:
            yield
        finally:
            self._placement = old
