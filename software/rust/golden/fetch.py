"""Download the frozen golden witnesses next to this file.

The JSON witnesses (about 820 MB) live in the Hugging Face dataset
`KBlueLeaf/kohakutpu-golden`; git tracks only this directory's tooling and
REVISION, the dataset commit this tree's code agrees with. A fetch writes
the revision it downloaded to `.revision`, which the ktpu `golden_revision`
test compares with REVISION.

    python software/rust/golden/fetch.py [--revision REV]
"""

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

REPO = "KBlueLeaf/kohakutpu-golden"
HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revision",
        default=(HERE / "REVISION").read_text().strip(),
        help="dataset commit (default: REVISION)",
    )
    args = parser.parse_args()
    snapshot_download(
        REPO,
        repo_type="dataset",
        revision=args.revision,
        local_dir=HERE,
        allow_patterns=["*.json"],
    )
    (HERE / ".revision").write_text(args.revision + "\n")


if __name__ == "__main__":
    main()
