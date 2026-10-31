"""The card daemon: one owner process per card.

Everything that must not be shared or interrupted lives here and only
here. The daemon opens the transport once and closes it once, so a
client crash never tears down a device mid-transfer; it holds the
JTAG/Tcl session warm, so a client command never pays a session open;
it executes hardware operations from ONE queue, so "one op at a time"
is structure rather than convention; and it is the resident process a
clock policy needs -- a library cannot down-clock after its script has
exited.

Clients keep the ordinary driver API: :class:`DaemonTransport` is a
:class:`kohakuaccel.driver.transport.Transport`, so ``Card``/``Device`` run
unchanged over IPC. Direct JTAG remains possible when no daemon runs
(the Tcl session admits one owner, so contention resolves itself);
XDMA access is daemon-only by design, never direct.

A project starts the daemon from its own CLI, which injects the board's clock
control, unit-global memory and firmware loader.
"""

from kohakuaccel.driver.daemon.client import (
    DaemonClient,
    DaemonTransport,
    RemoteNodeQueue,
)
from kohakuaccel.driver.daemon.governor import ClockGovernor
from kohakuaccel.driver.daemon.server import DEFAULT_PORT, Daemon

__all__ = [
    "DEFAULT_PORT",
    "ClockGovernor",
    "Daemon",
    "DaemonClient",
    "DaemonTransport",
    "RemoteNodeQueue",
]
