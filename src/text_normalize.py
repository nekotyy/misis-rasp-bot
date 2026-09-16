"""Общие текстовые примитивы для сопоставления названий групп, преподавателей и аудиторий."""

from __future__ import annotations

import re

LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "B": "В",
        "E": "Е",
        "e": "е",
        "K": "К",
        "k": "к",
        "M": "М",
        "H": "Н",
        "O": "О",
        "o": "о",
        "P": "Р",
        "p": "р",
        "C": "С",
        "c": "с",
        "T": "Т",
        "Y": "У",
        "y": "у",
        "X": "Х",
        "x": "х",
    }
)

_DASH_VARIANTS = ("—", "–", "‑", "−")


def normalize_dashes(text: str) -> str:
    for dash in _DASH_VARIANTS:
        text = text.replace(dash, "-")
    return text


def strip_non_word_chars(text: str) -> str:
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)
