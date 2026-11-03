"""Exponential backoff that cannot overflow, however long the outage lasts."""

# 2**64 times any positive base already exceeds every cap used here. Clamping the exponent
# before exponentiation keeps the arithmetic cheap and finite: ``0.15 * 2 ** 1024`` raises
# OverflowError (int too large to convert to float), which once killed the stop guard
# thread after ~35 minutes with an offline camera.
MAX_EXPONENT = 64


def capped_exp(base, exponent, cap):
    """``min(base * 2 ** exponent, cap)`` with the exponent clamped to ``[0, MAX_EXPONENT]``.

    Pure. Integer arguments give an integer result, as the unclamped expression did.
    """
    return min(base * 2 ** min(max(0, exponent), MAX_EXPONENT), cap)
