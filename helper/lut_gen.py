import numpy as np
from dataclasses import dataclass
from typing import Tuple
from .float import FPNumber


def generate_inverse_lut(
    input_exp_bits: int, input_man_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for inverse function (1/x)"""
    lut = {}
    input_bits = input_man_bits  # We only need mantissa bits since 1.mmm format

    for i in range(1 << input_bits):
        # Convert i to FP12 number (1.mmm format)
        input_val = 1.0 + (i / (1 << input_bits))
        inv_val = 1.0 / input_val

        # Convert result to output format
        fp_result = FPNumber.from_float(inv_val, output_exp_bits, output_man_bits)
        lut[i] = fp_result.to_bits()

    return lut


def generate_inverse_lut_subnormal(
    input_exp_bits: int, input_man_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for inverse function (1/x), where x have exp=0 and man!=0"""
    lut = {}
    input_bits = input_man_bits  # We only need mantissa bits since 0.mmm format

    for i in range(1 << input_bits):
        # Convert i to FP12 number (0.mmm format)
        input_val = 2**(0 - 2**(input_exp_bits-1)+2) * (i / (1 << input_bits))
        if input_val == 0:
            inv_val = 0
        else:
            inv_val = 1.0 / input_val
        print((i / (1 << input_bits)), input_val, inv_val)

        # Convert result to output format
        fp_result = FPNumber.from_float(inv_val, output_exp_bits, output_man_bits)
        lut[i] = fp_result.to_bits()

    return lut


def generate_log_lut(
    input_man_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for log(1.mmm)"""
    lut = {}

    for i in range(1 << input_man_bits):
        # Convert i to 1.mmm format
        input_val = 1.0 + (i / (1 << input_man_bits))
        log_val = np.log(input_val)
        print(input_val, log_val)

        # Convert result to output format
        fp_result = FPNumber.from_float(log_val, output_exp_bits, output_man_bits)
        lut[i] = fp_result.to_bits()

    return lut


def generate_log_lut_subnorm(
    input_man_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for log(1.mmm)"""
    lut = {}

    for i in range(1 << input_man_bits):
        # Convert i to 1.mmm format
        input_val = 0.0 + (i / (1 << input_man_bits))
        log_val = np.log(input_val)
        print(input_val, log_val)

        # Convert result to output format
        try:
            fp_result = FPNumber.from_float(log_val, output_exp_bits, output_man_bits)
            lut[i] = fp_result.to_bits()
        except (ValueError, OverflowError):
            lut[i] = int("0"+"1"*(output_exp_bits + output_man_bits), 2)

    return lut


def generate_log_exp_lut(
    input_exp_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for e*log(2) e can be positive or negative"""
    lut = {}
    # convert i to exponent, when we have n exp bits, the offset is 2^(n-1)-1.
    exp_offset = (1 << (input_exp_bits - 1)) - 1

    for i in range(1 << input_exp_bits):
        e = i - exp_offset
        log_val = np.log(2) * e
        fp_result = FPNumber.from_float(log_val, output_exp_bits, output_man_bits)
        lut[i] = fp_result.to_bits()

    return lut


def generate_exp_lut_int(
    input_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for exp(x) where x is in [0, ln(2))"""
    lut = {}

    for sign in [0, 1]:
        for i in range(1 << input_bits):
            input_val = float(i) * (-1)**sign
            exp_val = np.exp(input_val)

            # Convert result to output format
            fp_result = FPNumber.from_float(exp_val, output_exp_bits, output_man_bits)
            lut[i + (sign << input_bits)] = fp_result.to_bits()

    return lut


def generate_exp_lut_high_frac(
    input_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for exp(x) where x is in [0, ln(2))"""
    lut = {}

    for sign in [0, 1]:
        for i in range(1 << input_bits):
            input_val = float(i)/(2**input_bits) * (-1)**sign
            exp_val = np.exp(input_val)

            # Convert result to output format
            fp_result = FPNumber.from_float(exp_val, output_exp_bits, output_man_bits)
            lut[i + (sign << input_bits)] = fp_result.to_bits()

    return lut


def generate_exp_lut_low_frac(
    input_bits: int, output_exp_bits: int, output_man_bits: int
) -> dict:
    """Generate LUT for exp(x) where x is in [0, ln(2))"""
    lut = {}

    for sign in [0, 1]:
        for i in range(1 << input_bits):
            input_val = float(i)/(2**(input_bits*2)) * (-1)**sign
            exp_val = np.exp(input_val)

            # Convert result to output format
            fp_result = FPNumber.from_float(exp_val, output_exp_bits, output_man_bits)
            lut[i + (sign << input_bits)] = fp_result.to_bits()

    return lut


# Example usage and test
if __name__ == "__main__":
    # Generate inverse LUT (FP12 to FP16)
    # inv_lut = generate_inverse_lut(5, 6, 5, 10)  # FP12(E5M6) to FP16
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{inv_lut[i]:016b}")
    #     print(f"{inv_lut[i]:016b}: {FPNumber.from_bits(inv_lut[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    invlut_subnorm = generate_inverse_lut_subnormal(5, 6, 5, 10)
    results = []
    for i in range(1 << 6):
        print(i)
        results.append(f"{invlut_subnorm[i]:016b}")
        print(f"{invlut_subnorm[i]:016b}: {FPNumber.from_bits(invlut_subnorm[i], 5, 10).to_float()}")
    for i in zip(*results):
        print("".join(reversed(list(i))))

    # Generate log LUT and log-exp LUT
    # log(x) = log(1.mmmmmm) + e*log(2)
    # log_lut = generate_log_lut(6, 5, 10)  # 6-bit mantissa input to FP16
    # log_lut_subnorm = generate_log_lut_subnorm(6, 5, 10)  # 6-bit mantissa input to FP16
    # log_exp_lut = generate_log_exp_lut(5, 5, 10)  # 5-bit exp input to FP16
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{log_lut[i]:016b}")
    #     # print(f"{log_lut[i]:016b}: {FPNumber.from_bits(log_lut[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # print()
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{log_lut_subnorm[i]:016b}")
    #     print(f"{log_lut_subnorm[i]:016b}: {FPNumber.from_bits(log_lut_subnorm[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # print()

    # results = []
    # for i in range(1 << 5):
    #     results.append(f"{log_exp_lut[i]:016b}")
    #     # print(f"{log_exp_lut[i]:016b}: {FPNumber.from_bits(log_exp_lut[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # Generate exp LUT
    # exp_lut_int = generate_exp_lut_int(5, 5, 10)
    # exp_lut_high_frac = generate_exp_lut_high_frac(5, 5, 10)
    # exp_lut_low_frac = generate_exp_lut_low_frac(5, 5, 10)
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{exp_lut_int[i]:016b}")
    #     print(f"{exp_lut_int[i]:016b}: {FPNumber.from_bits(exp_lut_int[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # print()
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{exp_lut_high_frac[i]:016b}")
    #     print(f"{exp_lut_high_frac[i]:016b}: {FPNumber.from_bits(exp_lut_high_frac[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # print()
    # results = []
    # for i in range(1 << 6):
    #     results.append(f"{exp_lut_low_frac[i]:016b}")
    #     print(f"{exp_lut_low_frac[i]:016b}: {FPNumber.from_bits(exp_lut_low_frac[i], 5, 10).to_float()}")
    # for i in zip(*results):
    #     print("".join(reversed(list(i))))

    # print()

    # # Test inverse function
    # print("\nTesting inverse LUT:")
    # test_values = [1.0, 1.25, 1.5, 1.75, 3.14]
    # for val in test_values:
    #     fp12 = FPNumber.from_float(val, 5, 6)
    #     lut_result = FPNumber.from_bits(inv_lut[fp12.mantissa], 5, 10)
    #     print(f"1/{val:.3f} = {lut_result.to_float():.6f}")

    # # Test log function
    # print("\nTesting log LUT:")
    # for val in test_values:
    #     fp_in = FPNumber.from_float(val, 5, 6)
    #     lut_result_man = FPNumber.from_bits(log_lut[fp_in.mantissa], 5, 10)
    #     lut_result_exp = FPNumber.from_bits(log_exp_lut[fp_in.exponent], 5, 10)
    #     final_result = lut_result_man.to_float() + lut_result_exp.to_float()
    #     diff = np.log(val) - final_result
    #     APE = abs(diff) / np.log(val) * 100 if diff else 0
    #     print(
    #         f"log({val:.3f}) = {final_result:.6f}"
    #         f" (actual = {np.log(val):.6f}, APE = {APE:.2f}%)"
    #     )

    # Test exp function
    # print("\nTesting exp LUT:")
    # test_values = [0.0, 0.2, 0.4, 0.6]
    # for val in test_values:
    #     idx = int((val) * (1 << 6))
    #     lut_result = FPNumber.from_bits(exp_lut[idx], 5, 10)
    #     diff = np.exp(val) - lut_result.to_float()
    #     APE = abs(diff) / np.exp(val) * 100 if diff else 0
    #     print(
    #         f"exp({val:.3f}) = {lut_result.to_float():.6f}"
    #         f" (actual = {np.exp(val):.6f}, APE = {APE:.2f}%)"
    #     )
