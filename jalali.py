"""Gregorian -> Jalali 1405 conversion, anchor-based (mirrors vault-map.md's table).

Valid only for 1405 (2026-03-21 .. 2027-03-20). Extend ANCHORS for the next year when
the vault crosses into 1406 rather than reaching for a general-purpose calendar library
this project has no other use for.
"""
import datetime as dt

YEAR = 1405
MONTH_NAMES = ["Farvardin", "Ordibehesht", "Khordad", "Tir", "Amordad", "Shahrivar",
               "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand"]
ANCHORS = [
    (1, dt.date(2026, 3, 21)), (2, dt.date(2026, 4, 21)), (3, dt.date(2026, 5, 22)),
    (4, dt.date(2026, 6, 22)), (5, dt.date(2026, 7, 23)), (6, dt.date(2026, 8, 23)),
    (7, dt.date(2026, 9, 23)), (8, dt.date(2026, 10, 23)), (9, dt.date(2026, 11, 22)),
    (10, dt.date(2026, 12, 22)), (11, dt.date(2027, 1, 21)), (12, dt.date(2027, 2, 20)),
]
NEXT_YEAR_START = dt.date(2027, 3, 21)


def greg_to_jalali(d):
    if d < ANCHORS[0][1] or d >= NEXT_YEAR_START:
        raise ValueError(f"{d} is outside the mapped 1405 range ({ANCHORS[0][1]}..{NEXT_YEAR_START}) "
                          f"— extend jalali.ANCHORS for the new year")
    month, start = 1, ANCHORS[0][1]
    for m, s in ANCHORS:
        if d >= s:
            month, start = m, s
        else:
            break
    return YEAR, month, (d - start).days + 1


def folder_name(month):
    return f"({month:02d}) {MONTH_NAMES[month - 1]} {YEAR}"


def jalali_str(month, day):
    return f"{YEAR}-{month:02d}-{day:02d}"


def file_rel(month, day):
    return f"{folder_name(month)}/Daily worklogs/{jalali_str(month, day)}.md"
