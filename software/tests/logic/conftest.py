"""The logic stage: host-side code checked by its outcome, no device model."""

import pathlib
import sys

#: The application's example models (`examples.tinygrad`) are a source tree, not
#: an installed package.
APPLICATION = pathlib.Path(__file__).resolve().parents[2] / "kohakutpu" / "application"
sys.path.insert(0, str(APPLICATION))
