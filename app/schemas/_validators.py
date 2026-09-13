"""Small validators shared across schemas."""


def normalize_currency_code(value: str) -> str:
    value = value.strip().upper()
    if len(value) != 3:
        raise ValueError("currency code must be exactly 3 letters (ISO-4217)")
    return value


def normalize_currency_code_or_none(value: str | None) -> str | None:
    """Same normalization, but for untrusted LLM output (app/api/v1/parse.py)
    rather than client-authored input: a non-ISO code from the model isn't
    a request we should reject outright, it's a field we don't trust —
    coerce to the 3-char uppercase invariant where possible, else drop it
    to None (never store/return a malformed currency code as if valid).
    """
    if value is None:
        return None
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        return None
    return normalized
