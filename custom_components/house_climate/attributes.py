"""Entity attribute snapshots small enough for the recorder (v0.5.24).

HA's recorder drops state attributes above 16 KB. `snapshot` returns a deep
copy (the coordinator mutates its maps in place, and HA compares new
attributes with the previous state's) with floats at 4 significant digits;
values of 1e6 and more (timestamps) are kept as they are.
"""

from math import floor, isfinite, log10

DIGITS = 4


def snapshot(value):
    if isinstance(value, dict):
        return {key: snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(item) for item in value]
    if isinstance(value, set):
        return sorted(snapshot(item) for item in value)
    if isinstance(value, float):
        if not isfinite(value) or value == 0 or abs(value) >= 1e6:
            return value
        return round(value, DIGITS - 1 - floor(log10(abs(value))))
    return value
