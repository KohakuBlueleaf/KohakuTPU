"""Dispatch harness on the protocol double: no tensor arithmetic or cycle model."""

from unittest.mock import patch

from examples.tinygrad.dispatch_measure import main
from hseam_counting import CountingCard

from kohakutpu.application.runtime import device as D

if __name__ == "__main__":
    with patch.object(D, "OfflineCard", CountingCard):
        main()
