"""A node that dispatches its own work: firmware boot and the host<->node queue.

The node's RV64 processor runs the dispatcher firmware (`firmware/`); the host
only submits packages and polls for completions (docs/spec/node-queue.md).
"""
