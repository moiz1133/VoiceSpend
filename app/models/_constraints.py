"""Helper for building CHECK constraints from a fixed StrEnum domain."""

from enum import StrEnum

from sqlalchemy import CheckConstraint


def enum_check(column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    """Build a CHECK(<column> IN (...)) constraint from a StrEnum's values.

    Pass only the column name — Base.metadata's naming_convention fills in
    the table name automatically (via the "ck" convention), so a name here
    would double up as "ck_<table>_ck_<table>_<column>".
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=column)
