import random
from .float import FPNumber


def int_bits_to_float_bits(
    x, exp_bits: int, man_bits: int
) -> float:
    """Convert integer bits to float bits"""
    return FPNumber.from_bits(x, exp_bits, man_bits).to_float()


def random_int_for_float(exp_bits: int, man_bits: int) -> int:
    """Generate random integer for float"""
    exp_init = (1<<(exp_bits-1)) - 2
    exp_bits_gen = random.randint(0, 2**exp_init-1)
    exp_bits_list = reversed(list(int(i) for i in f"{exp_bits_gen:b}"))
    exp = exp_init
    for bit in exp_bits_list:
        if not bit: break
        exp -= 1
    man = random.randint(0, (1 << man_bits) - 1)
    return (exp << man_bits) | man


def float_rand(exp_bits: int, man_bits: int):
    x = random_int_for_float(exp_bits, man_bits)
    return int_bits_to_float_bits(x, exp_bits, man_bits)


if __name__ == "__main__":
    from matplotlib import pyplot as plt
    import numpy as np
    samples = [float_rand(5, 10) for _ in range(50000)]
    print(np.mean(samples), np.std(samples), np.min(samples), np.max(samples))
    plt.hist(samples, bins=100)
    plt.show()