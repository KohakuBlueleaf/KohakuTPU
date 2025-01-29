import torch
import numpy as np
import matplotlib.pyplot as plt

# Set random seed for reproducibility
torch.manual_seed(42)

# Generate data points
x = torch.linspace(-0.5 * np.pi, 0.5 * np.pi, 10000).reshape(-1, 1).half()
cos_target = torch.cos(x)
sin_target = torch.sin(x)

# Plotting
plt.figure(figsize=(12, 5))

# Plot cosine approximation
plt.subplot(1, 2, 1)
with torch.no_grad():
    y_pred_cos = 1 - x**2 * 0.5 + x**4 * 1/24 #- x**6 * 1/720
plt.plot(x.numpy(), cos_target.numpy(), label='True cos(πx)')
plt.plot(x.numpy(), y_pred_cos.numpy(), '--', label='Approximation')
plt.title('Cosine Approximation')
plt.legend()
plt.grid(True)

# Plot sine approximation
x = x #+ 0.5 * np.pi
plt.subplot(1, 2, 2)
with torch.no_grad():
    y_pred_sin = x - x**3 * 1/6 + x**5 * 1/120 #- x**7 * 1/5040 #+ x**9 * 1/362880
plt.plot(x.numpy(), sin_target.numpy(), label='True sin(πx)')
plt.plot(x.numpy(), y_pred_sin.numpy(), '--', label='Approximation')
plt.title('Sine Approximation')
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()