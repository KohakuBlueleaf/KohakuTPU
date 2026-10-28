"""Download the frozen golden witnesses next to this file.

The JSON witnesses (about 820 MB) live in the Hugging Face dataset
`KBlueLeaf/kohakutpu-golden`; git tracks only this directory's tooling.

    python software/rust/golden/fetch.py [--revision REV]
"""

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

REPO = "KBlueLeaf/kohakutpu-golden"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default=None, help="dataset commit or tag")
    args = parser.parse_args()
    snapshot_download(
        REPO,
        repo_type="dataset",
        revision=args.revision,
        local_dir=Path(__file__).resolve().parent,
        allow_patterns=["*.json"],
    )


if __name__ == "__main__":
    main()
