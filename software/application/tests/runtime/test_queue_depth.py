"""The eager and replay paths obey the same user-selected in-flight cap."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakutpu.application.runtime.device import QUEUE_DEPTH_ENV, Device, Recorded


def device(depth=None):
    return Device(kind="offline", board="vlt_v9t6s_1n", queue_depth=depth)


def fake_card(dev):
    card = dev.card
    card.submissions, card.collections = [], []

    def submit(program):
        card.submissions.append(program)
        return len(card.submissions)

    def collect(tag):
        card.collections.append(tag)
        return {"cycles": tag}

    card.submit = submit
    card.submit_recorded = submit
    card.collect = collect
    return card


def compiled():
    return SimpleNamespace(
        name="depth",
        scratch=[],
        l1=SimpleNamespace(params=lambda *a: []),
        programs=lambda binds: {None: dict(binds)},
    )


def assert_bound(dev, replay):
    card = fake_card(dev)
    if replay:
        log = Recorded(
            [("run", {0: (b"bytes", ())})] * 3,
            [],
            dev,
            [("depth", "dsl", {}, [], [], (0,))] * 3,
        )
        pending = dev.replay(log, wait=False)
        log.close()
    else:
        pending = [dev.submit(compiled(), {}) for _ in range(3)]
    assert card.collections == [1]
    assert len(dev.inflight) == 2
    assert [dev.collect(p).cycles for p in pending] == [1, 2, 3]
    assert card.collections == [1, 2, 3]
    dev.close()


@pytest.mark.parametrize("replay", [False, True])
def test_depth_applies_to_eager_and_replay(replay, monkeypatch):
    monkeypatch.delenv(QUEUE_DEPTH_ENV, raising=False)
    assert_bound(device(2), replay)


@pytest.mark.parametrize("replay", [False, True])
def test_depth_bypass_mutant_is_caught(replay, monkeypatch):
    monkeypatch.delenv(QUEUE_DEPTH_ENV, raising=False)
    dev = device(2)
    with patch.object(dev, "max_inflight", None), pytest.raises(AssertionError):
        assert_bound(dev, replay)
    dev.close()


def test_environment_and_argument_precedence(monkeypatch):
    monkeypatch.setenv(QUEUE_DEPTH_ENV, "3")
    a, b = device(), device(2)
    assert (a.max_inflight, b.max_inflight) == (3, 2)
    a.close()
    b.close()


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_invalid_environment_refused_before_open(value, monkeypatch):
    monkeypatch.setenv(QUEUE_DEPTH_ENV, value)
    with pytest.raises(ValueError):
        device()


def test_default_preserves_memory_and_ring_bounds(monkeypatch):
    monkeypatch.delenv(QUEUE_DEPTH_ENV, raising=False)
    dev = device()
    assert dev.max_inflight is None
    dev.close()
