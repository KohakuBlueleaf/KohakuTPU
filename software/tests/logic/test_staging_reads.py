"""Host reads of staging go in pieces of at most one kilobyte."""

from kohakuaccel.driver.transport.base import Transport
from kohakuaccel.driver.transport.rebase import UnitGlobal

WINDOWS = [(n + 1) << 40 for n in range(4)]


class Log(Transport):
    bulk = True

    def __init__(self):
        self.writes = []
        self.reads = []

    def read_block(self, addr, nbytes):
        self.reads.append((addr, nbytes))
        return bytes(nbytes)

    def write64(self, addr, data):
        self.writes.append(addr)

    def read64(self, addr):
        return 0

    def write_block(self, addr, data):
        self.writes.append(addr)


def memory():
    log = Log()
    bases = [n << 36 for n in range(4)]
    return log, UnitGlobal(log, bases, WINDOWS, 1 << 36, staging=True)


def test_a_staging_read_takes_at_most_one_kilobyte_at_a_time():
    log, mem = memory()
    stage = 1 << 39
    assert len(mem.read_block(stage, 2560)) == 2560
    assert log.reads == [
        (WINDOWS[0] | stage, 1024),
        (WINDOWS[0] | stage + 1024, 1024),
        (WINDOWS[0] | stage + 2048, 512),
    ]


def test_a_dram_read_stays_whole():
    log, mem = memory()
    mem.read_block(0x1000, 8192)
    assert log.reads == [(WINDOWS[0] + 0x1000, 8192)]
