"""Demo script running Anima Text-to-Image Generation on KohakuTPU."""

import argparse
import time

from demos.kohakutpu.anima.config import AnimaConfig
from demos.kohakutpu.anima.pipeline import AnimaPipeline


def parse_args():
    parser = argparse.ArgumentParser(
        description="Anima Text-to-Image Demo on KohakuTPU"
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="1girl, solo, anime aesthetic, vibrant colors, masterpiece",
        help="Text prompt",
    )
    parser.add_argument(
        "--negative-prompt",
        type=str,
        default="low quality, blurry, deformed",
        help="Negative prompt",
    )
    parser.add_argument(
        "--height", type=int, default=128, help="Output image height (multiple of 16)"
    )
    parser.add_argument(
        "--width", type=int, default=128, help="Output image width (multiple of 16)"
    )
    parser.add_argument(
        "--steps", type=int, default=5, help="Number of flow matching inference steps"
    )
    parser.add_argument(
        "--cfg", type=float, default=4.0, help="Classifier-Free Guidance scale"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--config",
        type=str,
        choices=["tiny", "base_2b", "large_2_9b"],
        default="tiny",
        help="Model configuration profile",
    )
    parser.add_argument(
        "--device", type=str, default="KTPU", help="Execution device (KTPU or CPU)"
    )
    parser.add_argument(
        "--output", type=str, default="anima_sample.png", help="Output PNG file path"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 65)
    print("  KohakuTPU - Anima Text-to-Image Diffusion Model Demo")
    print("=" * 65)
    print(f"  Prompt       : {args.prompt}")
    print(f"  Resolution   : {args.height} x {args.width}")
    print(f"  Steps        : {args.steps}")
    print(f"  CFG Scale    : {args.cfg}")
    print(f"  Config       : {args.config}")
    print(f"  Device       : {args.device}")
    print(f"  Output Path  : {args.output}")
    print("=" * 65)

    if args.config == "tiny":
        cfg = AnimaConfig.tiny(device=args.device)
    elif args.config == "base_2b":
        cfg = AnimaConfig.base_2b(device=args.device)
    elif args.config == "large_2_9b":
        cfg = AnimaConfig.large_2_9b(device=args.device)
    else:
        cfg = AnimaConfig.tiny(device=args.device)

    print("[1/3] Building Anima pipeline on KohakuTPU...")
    pipe = AnimaPipeline.from_config(cfg)
    print("      Model instantiated with:")
    print(f"        - Model Channels: {cfg.model_channels}")
    print(f"        - Transformer Blocks: {cfg.num_blocks}")
    print(f"        - Attention Heads: {cfg.num_heads} (head_dim={cfg.head_dim})")
    print(f"        - AdaLN-LoRA Dim: {cfg.adaln_lora_dim}")

    print("\n[2/3] Generating image via Flow Matching ODE on KTPU...")
    t0 = time.time()
    img = pipe(
        prompt=args.prompt,
        negative_prompt=args.negative_prompt,
        height=args.height,
        width=args.width,
        num_inference_steps=args.steps,
        cfg_scale=args.cfg,
        seed=args.seed,
        verbose=True,
    )
    t_elapsed = time.time() - t0

    print(f"\n[3/3] Saving generated image to {args.output}...")
    img.save(args.output)
    print(f"      Image saved successfully! Total time: {t_elapsed:.2f}s")
    print("=" * 65)


if __name__ == "__main__":
    main()
