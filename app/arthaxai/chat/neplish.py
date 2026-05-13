from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SHARED_NEPLISH_RULES_PATH = _PROJECT_ROOT / "shared" / "nlu" / "neplish_rules.json"
ResponseLanguageMode = Literal["english", "neplish", "nepali"]


def _load_shared_neplish_rules() -> dict:
    try:
        with _SHARED_NEPLISH_RULES_PATH.open("r", encoding="utf-8") as fp:
            raw = json.load(fp)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"replacement_rules": [], "finance_keywords": [], "cue_words": []}


_SHARED_NEPLISH_RULES = _load_shared_neplish_rules()
_NEPLISH_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(str(item.get("pattern") or "").strip(), re.IGNORECASE),
        str(item.get("replacement") or ""),
    )
    for item in (_SHARED_NEPLISH_RULES.get("replacement_rules") or [])
    if isinstance(item, dict) and str(item.get("pattern") or "").strip()
)
_FINANCE_KEYWORDS = {
    str(item).strip().lower()
    for item in (_SHARED_NEPLISH_RULES.get("finance_keywords") or [])
    if str(item).strip()
}
_CUE_WORDS = {
    str(item).strip().lower()
    for item in (_SHARED_NEPLISH_RULES.get("cue_words") or [])
    if str(item).strip()
}
_RAW_NEPLISH_MARKERS = {
    "mero",
    "ma",
    "maile",
    "malai",
    "hamro",
    "timro",
    "tapai",
    "tapaii",
    "tapailai",
    "kati",
    "ko",
    "le",
    "lai",
    "sanga",
    "bata",
    "cha",
    "chha",
    "xa",
    "chaina",
    "tirnu",
    "tirna",
    "tirne",
    "baki",
    "linu",
    "paunu",
    "parcha",
    "parne",
    "dekhaunus",
    "dekhaunu",
    "garya",
    "garyo",
    "gareko",
}
_DEVANAGARI_PATTERN = re.compile(r"[\u0900-\u097F]")


def normalize_neplish_text(query: str) -> str:
    normalized = str(query or "").strip().lower()
    if not normalized:
        return ""
    for pattern, replacement in _NEPLISH_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def detect_response_language_mode(query: str) -> ResponseLanguageMode:
    raw = str(query or "").strip()
    if not raw:
        return "english"

    has_devanagari = bool(_DEVANAGARI_PATTERN.search(raw))
    latin_tokens = re.findall(r"[a-zA-Z]+", raw)
    raw_tokens = {token.lower() for token in latin_tokens if token.strip()}

    if has_devanagari:
        if len(latin_tokens) >= 3:
            return "neplish"
        return "nepali"

    raw_marker_hits = raw_tokens & (_FINANCE_KEYWORDS | _CUE_WORDS | _RAW_NEPLISH_MARKERS)
    if raw_marker_hits:
        return "neplish"

    normalized = normalize_neplish_text(raw)
    if not normalized:
        return "english"

    tokens = set(normalized.split())
    if tokens & (_FINANCE_KEYWORDS | _CUE_WORDS):
        return "neplish"
    return "english"
