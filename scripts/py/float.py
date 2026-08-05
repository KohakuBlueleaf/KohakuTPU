import numpy as np
from dataclasses import dataclass
from typing import Tuple


@dataclass
class FPNumber:
    """Custom floating point number representation"""

    sign: int
    exponent: int
    mantissa: int
    exp_bits: int  # Number of exponent bits
    man_bits: int  # Number of mantissa bits

    @classmethod
    def from_float(cls, value: float, exp_bits: int, man_bits: int) -> "FPNumber":
        """Convert float to custom floating point format"""
        if value == 0:
            return cls(0, 0, 0, exp_bits, man_bits)

        # Extract sign
        sign = 0 if value >= 0 else 1

        # Extract exponent and mantissa
        abs_val = abs(value)
        exp = int(np.floor(np.log2(abs_val)))
        man = abs_val / (2**exp) - 1  # Subtract 1 to get fractional part

        # Scale mantissa to have extra bits for rounding
        extra_bits = 3  # Guard, round, and sticky bits
        man_scaled = int(man * (1 << (man_bits + extra_bits)))

        # Extract rounding bits
        guard_bit = (man_scaled >> (extra_bits - 1)) & 1
        round_bit = (man_scaled >> (extra_bits - 2)) & 1
        sticky_bit = (man_scaled & ((1 << (extra_bits - 2)) - 1)) != 0

        # Perform round to nearest even
        man_int = man_scaled >> extra_bits
        round_up = False

        if guard_bit == 1:
            if round_bit == 1 or sticky_bit == 1:
                round_up = True
            elif man_int & 1 == 1:  # If mantissa is odd, round up
                round_up = True

        if round_up:
            man_int += 1
            # Handle mantissa overflow
            if man_int >= (1 << man_bits):
                man_int >>= 1
                exp += 1

        # Normalize to our custom format
        bias = (1 << (exp_bits - 1)) - 1
        exp_biased = exp + bias

        # Handle overflow/underflow
        if exp_biased >= (1 << exp_bits):
            exp_biased = (1 << exp_bits) - 1
            man_int = (1 << man_bits) - 1
        elif exp_biased < 0:
            exp_biased = 0
            man_int = 0

        return cls(sign, exp_biased, man_int, exp_bits, man_bits)

    def to_float(self) -> float:
        """Convert back to float"""
        if self.exponent == 0 and self.mantissa == 0:
            return 0.0

        bias = (1 << (self.exp_bits - 1)) - 1
        exp = self.exponent - bias
        man = 1.0 + (self.mantissa / (1 << self.man_bits))

        return man * (2.0**exp) * ((-1) ** self.sign)

    def to_bits(self) -> int:
        """Convert to bit representation"""
        return (
            (self.sign << (self.exp_bits + self.man_bits))
            | (self.exponent << self.man_bits)
            | self.mantissa
        )

    @classmethod
    def from_bits(cls, bits: int, exp_bits: int, man_bits: int) -> "FPNumber":
        """Create from bit representation"""
        man_mask = (1 << man_bits) - 1
        mantissa = bits & man_mask
        exponent = (bits >> man_bits) & ((1 << exp_bits) - 1)
        return cls(
            bits >> (exp_bits + man_bits), exponent, mantissa, exp_bits, man_bits
        )


if __name__ == "__main__":
    # Test conversion
    value = 16640.0
    a = FPNumber.from_float(value, 5, 6)
    ainv = 1/a.to_float()
    ainv_fp = FPNumber.from_float(ainv, 5, 10)
    print(f"Original : {a.to_bits():012b}")
    print(f"Inversion: {ainv_fp.to_bits():016b}")