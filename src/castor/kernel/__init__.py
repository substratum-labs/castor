"""Kernel: pure security decision logic for Castor.

This package contains zero-I/O functions that encode the Kernel's
security policy.  In castord (Rust daemon), these become the Ring 0
state machine — ``fn handle(KernelOp) -> Vec<Effect>``.
"""
