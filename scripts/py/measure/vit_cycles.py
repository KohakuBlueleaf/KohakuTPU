"""ViT per-image firmware records; default three images, no hidden warmup."""

import os
import sys

from examples.tinygrad.dispatch_measure import main

if __name__ == "__main__":
    sys.argv[1:1] = ["vit", "--warmup", "0", "--inputs", "3"]
    if os.environ.get("KTPU_INFERENCE_REPLAY", "1") == "1":
        sys.argv.append("--replay")
    main()
