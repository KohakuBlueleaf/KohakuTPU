"""A far peer drain whose burst would end in a one-flit interlink packet is
encoded as the hand-written split: the first count - 1 words, then the last
word on a spare descriptor at the next granule (L1.VC.010)."""

import json

import pytest

from kohakutpu.language import machine as MF
from kohakutpu.stack.rs import _rs

MACHINE = (MF.MACHINES / "multimesh_v9t8.json").read_text(encoding="utf-8")
FACTS = json.loads(MACHINE)["machine"]
SPARE = FACTS["vc"]["descriptors"] - 1
BEATS = FACTS["il"]["packet_beats"]
FAR = "mesh=1, fin=(2, 1)"


def module(body: str) -> str:
    return f"target ktpu.v9\n\nimage send(x)\n{body}"


def send(n: int, base: str, drains: str, extra: str = "") -> str:
    return module(
        f"    desc a0 = x walk=(32, {n})\n"
        f"    desc a1 = {base} walk=(32, {n})\n"
        f"{extra}"
        f"    vfill a0 -> l1[0]\n"
        f"    vsync drain on=fill\n"
        f"{drains}"
        f"    halt\n"
    )


def encode(text: str):
    return _rs.image(text, "send", [4096], MACHINE)


def split_by_hand(n: int, flags: str, ainc: str = "") -> str:
    """The split written out: a1 walks n - 1 words, a<SPARE> the last."""
    peer = f"peer(64, count=1, {FAR})"
    tail = f"peer({64 + n - 1}, count=1, {FAR})"
    lead = "" if not ainc else f"    ainc a1 += {ainc}\n    ainc a{SPARE} += {ainc}\n"
    return module(
        f"    desc a0 = x walk=(32, {n})\n"
        f"    desc a1 = {peer} walk=(32, {n - 1})\n"
        f"    desc a{SPARE} = {tail} walk=(32, 1)\n"
        f"{lead}"
        f"    vfill a0 -> l1[0]\n"
        f"    vsync drain on=fill\n"
        f"    vdrain a1 <- l1[0] to=(0, 1){flags.replace(' signal', '')}\n"
        f"    vdrain a{SPARE} <- l1[{n - 1}] to=(0, 1){flags}\n"
        f"    halt\n"
    )


@pytest.mark.parametrize("n", [BEATS, 2 * BEATS])
def test_a_far_drain_closing_a_one_flit_packet_is_the_hand_split(n):
    peer = f"peer(64, count=1, {FAR})"
    got = encode(send(n, peer, "    vdrain a1 <- l1[0] to=(0, 1) signal\n"))

    want = encode(split_by_hand(n, " signal"))
    assert got[0] == want[0]
    assert dict(got[1]) == dict(want[1])


def test_a_relative_split_steps_both_descriptors():
    peer = f"peer(64, count=1, {FAR})"
    drains = "    vdrain a1 <- l1[0] to=(0, 1) rel\n"

    stale = encode(send(BEATS, peer, drains))
    assert stale[0] == encode(split_by_hand(BEATS, " rel"))[0]
    stepped = encode(send(BEATS, peer, drains, "    ainc a1 += 64\n"))
    assert stepped[0] == encode(split_by_hand(BEATS, " rel", "64"))[0]


@pytest.mark.parametrize(
    "n, base",
    [
        (BEATS - 1, f"peer(64, count=1, {FAR})"),
        (BEATS + 1, f"peer(64, count=1, {FAR})"),
        (BEATS, "peer(64, count=1)"),
    ],
)
def test_other_drains_keep_one_descriptor(n, base):
    _, descs = encode(send(n, base, "    vdrain a1 <- l1[0] to=(0, 1)\n"))

    assert {ad for ad, _ in descs} == {0, 1}
