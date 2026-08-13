"""Grid instances -> artifacts: dealing, round packing, and the two hard rules.

Level 0. Nothing above states a node coordinate or a staging slot. Both rules
here fail as a hang rather than as a wrong number:

* **A round restages from slot 0.** A slot past the `stage_flits`-deep staging
  RAM is STALE, not empty; a unit fed the last program's leftovers runs garbage
  and never retires.
* **A node kicked twice in a round is awaited ONCE, for the total.** Completion
  counts are cumulative and the poll is an equality, so an intermediate count is
  a number the machine steps straight past -- observed as a timeout holding
  `0x21e1` against a wanted `0x21dd`, four completions AHEAD.
"""

from kohakuaccel.artifact import Artifact, Await, Kick, SeedCredits


class DispatchError(ValueError):
    """Work that cannot be placed on this machine, and why."""


def deal(keys, nodes) -> dict:
    """Which node each instance lands on: round-robin over sorted `keys`.

    Raises :class:`DispatchError` when `nodes` is empty.
    """
    if not nodes:
        raise DispatchError("no units of the required kind on this machine")
    return {key: nodes[i % len(nodes)] for i, key in enumerate(sorted(keys))}


def plan(
    payloads: dict,
    nodes,
    stage_flits: int = 128,
    credits: int = 512,
    name: str = "kernel",
    acks: dict | None = None,
) -> list:
    """Deal `payloads` over `nodes` and pack them into artifacts.

    `payloads` maps a grid coordinate to that instance's instruction words.
    Instances are dealt round-robin, so a grid smaller than the mesh leaves
    units idle rather than serialising, and a larger one wraps.

    `acks` maps an instance to ``(coord, count)`` completions to wait for on a
    node this round never kicked -- a peer that answers the instance's transfer.

    Returns one :class:`~kohakuaccel.artifact.Artifact` per round. Raises
    :class:`DispatchError` if there are no nodes to run on.
    """
    placed = deal(payloads, nodes)
    acks = acks or {}

    work: list = []
    for key, words in sorted(payloads.items()):
        windows = [
            words[at : at + stage_flits] for at in range(0, len(words), stage_flits)
        ]
        for n, window in enumerate(windows):
            # The peer's answer is awaited in the round that kicked the LAST
            # window of its sender: `loader` rebaselines NODE_STATUS per round.
            due = acks.get(key) if n == len(windows) - 1 else None
            work.append((placed[key], window, due))

    out: list = []
    batch: list = []
    used = 0
    for item in work:
        if used and used + len(item[1]) > stage_flits:
            out.append(_artifact(batch, credits, name, len(out)))
            batch, used = [], 0
        batch.append(item)
        used += len(item[1])
    if batch:
        out.append(_artifact(batch, credits, name, len(out)))

    for a in out:
        a.meta["of"] = len(out)
    return out


def _artifact(batch: list, credits: int, name: str, index: int) -> Artifact:
    """One round: every kick staged from slot 0, then one await per node."""
    flits: list[int] = []
    steps: list = [SeedCredits(credits)]
    owed: dict = {}
    for node, window, due in batch:
        steps.append(Kick(node, len(flits), len(window)))
        owed[node] = owed.get(node, 0) + len(window)
        flits.extend(window)
        if due is not None:
            owed[due[0]] = owed.get(due[0], 0) + due[1]
    steps += [Await(node, n) for node, n in owed.items()]
    return Artifact(machine=name, flits=flits, steps=steps, meta={"round": index})
