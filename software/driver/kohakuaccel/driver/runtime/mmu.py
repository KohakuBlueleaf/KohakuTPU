"""Driver-owned card virtual ranges and live physical translations."""

import bisect
from collections import OrderedDict
from dataclasses import dataclass

#: Retired allocation records retained in `SoftwareMMU.entries` for inspection.
RETIRED_KEPT = 4096
#: Staging/special bit and mesh domain shift (die.address.special_bit, mesh_shift).
SPECIAL_BIT = 39
DOMAIN_SHIFT = 36
#: Non-zero base offset and alignment inside each 64 GiB virtual address domain.
VIRTUAL_OFFSET = 1 << 32
VIRTUAL_ALIGN = 256


@dataclass(frozen=True)
class Allocation:
    virtual: int
    physical: int
    extent: int
    arena: object
    generation: int

    @property
    def live(self):
        return (
            self.physical in self.arena.live
            and self.arena.generations.get(self.physical) == self.generation
        )


class SoftwareMMU:
    """Non-reused card virtual addresses, partitioned by hardware address domain."""

    def __init__(self):
        self.entries = {}
        self.next = {}
        self._live = {}
        self._retired = OrderedDict()
        self._keys = {}
        self._retired_below = {}
        self._prune_at = 256

    def _sync(self):
        if not self.entries and (self._live or self._retired or self._keys):
            self._live.clear()
            self._retired.clear()
            self._keys.clear()
            self._retired_below.clear()
            self._prune_at = 256

    def _retire(self, at: int):
        self._live.pop(at, None)
        if at in self.entries:
            self._retired[at] = None
        while len(self._retired) > RETIRED_KEPT:
            old, _ = self._retired.popitem(last=False)
            gone = self.entries.pop(old, None)
            if gone is None:
                continue
            domain = old >> DOMAIN_SHIFT
            keys = self._keys.get(domain)
            if keys:
                idx = bisect.bisect_left(keys, old)
                if idx < len(keys) and keys[idx] == old:
                    keys.pop(idx)
            self._retired_below[domain] = max(
                self._retired_below.get(domain, 0), old + gone.extent
            )

    def retire(self, address: int):
        entry = self._live.get(address)
        if entry is not None and not entry.live:
            self._retire(address)

    def register(self, physical, extent, arena):
        self._sync()
        if len(self._live) >= self._prune_at:
            dead = [k for k, e in self._live.items() if not e.live]
            for k in dead:
                self._retire(k)
            self._prune_at = max(256, len(self._live) * 2)
        # Preserve the staging/node bits used by compiler address classification.
        domain = physical >> DOMAIN_SHIFT if physical & (1 << SPECIAL_BIT) else 0
        start = (domain << DOMAIN_SHIFT) + VIRTUAL_OFFSET
        at = self.next.get(domain, start)
        size = max(
            VIRTUAL_ALIGN, (extent + VIRTUAL_ALIGN - 1) // VIRTUAL_ALIGN * VIRTUAL_ALIGN
        )
        if at + size >= (domain << DOMAIN_SHIFT) + (1 << DOMAIN_SHIFT):
            raise MemoryError("RUL.023: virtual address domain exhausted")
        self.next[domain] = at + size
        entry = Allocation(at, physical, extent, arena, arena.generations[physical])
        self.entries[at] = entry
        self._live[at] = entry
        self._keys.setdefault(domain, []).append(at)
        return at

    def resolve(self, address, nbytes=1, owner=False):
        self._sync()
        entry = self.entries.get(address)
        if entry is None:
            keys = self._keys.get(address >> DOMAIN_SHIFT)
            if keys:
                i = bisect.bisect_right(keys, address) - 1
                if i >= 0:
                    cand = self.entries.get(keys[i])
                    if cand is not None and address < cand.virtual + cand.extent:
                        entry = cand
            if entry is None:
                # Fallback if `entries` was populated or mutated directly.
                for cand in self.entries.values():
                    if cand.virtual <= address < cand.virtual + cand.extent:
                        entry = cand
                        break
        if entry is not None:
            off = address - entry.virtual
            if 0 <= off < entry.extent:
                if not entry.live or (owner and entry.physical in entry.arena.released):
                    if not entry.live:
                        self._retire(entry.virtual)
                    raise ValueError("RUL.023: allocation handle has no live owner")
                if nbytes < 0 or nbytes > entry.extent - off:
                    raise ValueError("RUL.023: access exceeds allocation extent")
                return entry, entry.physical + off
        domain = address >> DOMAIN_SHIFT
        start = (domain << DOMAIN_SHIFT) + VIRTUAL_OFFSET
        if start <= address < self._retired_below.get(domain, 0):
            raise ValueError("RUL.023: allocation handle has no live owner")
        raise KeyError(f"RUL.023: unknown allocation handle {address:#x}")

    def translations(self):
        self._sync()
        dead = [at for at, e in self._live.items() if not e.live]
        for at in dead:
            self._retire(at)
        return tuple((e.virtual, e.physical, e.extent) for e in self._live.values())

    def translations_for(self, handles):
        self._sync()
        seen = {}
        for address in handles:
            entry = self._live.get(address)
            if entry is None:
                keys = self._keys.get(address >> DOMAIN_SHIFT)
                if keys:
                    i = bisect.bisect_right(keys, address) - 1
                    if i >= 0:
                        cand = self._live.get(keys[i])
                        if cand is not None and address < cand.virtual + cand.extent:
                            entry = cand
                if entry is None:
                    for cand in self.entries.values():
                        if cand.virtual <= address < cand.virtual + cand.extent:
                            entry = cand
                            break
            if entry is not None:
                if entry.live:
                    seen[entry.virtual] = (
                        entry.virtual,
                        entry.physical,
                        entry.extent,
                    )
                else:
                    self._retire(entry.virtual)
        return tuple(seen.values())
