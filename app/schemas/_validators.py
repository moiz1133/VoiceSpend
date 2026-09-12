"""Small validators shared across schemas."""


def normalize_currency_code(value: str) -> str:
    value = value.strip().upper()
    if len(value) != 3:
        raise ValueError("currency code must be exactly 3 letters (ISO-4217)")
    return value
