"""Keyword rules that turn OCR text into a popup category."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9' ]+")
_OCR_FIXES = [
    (re.compile(r"\b1ive\b"), "live"),
    (re.compile(r"\bl1ve\b"), "live"),
    (re.compile(r"\bllve\b"), "live"),
    (re.compile(r"\b0\b"), "o"),
]


def normalize_text(text: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace so rule phrases
    match despite OCR noise. Keeps apostrophes so "can't" survives."""
    t = text.lower().replace("’", "'").replace("‘", "'")
    t = t.replace("-\n", "").replace("\n", " ")
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    for pattern, repl in _OCR_FIXES:
        t = pattern.sub(repl, t)
    return t


def _norm_phrase(p: str) -> str:
    return normalize_text(p)


@dataclass
class Category:
    key: str
    label: str
    severity: str = "high"
    priority: int = 0
    manual_attention: bool = False
    any: list[str] = field(default_factory=list)
    all: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)

    def match(self, normalized: str) -> Optional[list[str]]:
        """Return the phrases that matched, or None if the category does not apply."""
        hits = [p for p in self.any if p in normalized]
        if self.any and not hits:
            return None
        for p in self.all:
            if p not in normalized:
                return None
            hits.append(p)
        for p in self.none:
            if p in normalized:
                return None
        return hits or None


@dataclass
class Match:
    category: Category
    phrases: list[str]
    text: str

    @property
    def key(self) -> str:
        return self.category.key

    @property
    def label(self) -> str:
        return self.category.label

    @property
    def manual_attention(self) -> bool:
        return self.category.manual_attention


class RuleSet:
    def __init__(self, categories: list[Category], min_text_chars: int = 12) -> None:
        self.categories = sorted(categories, key=lambda c: c.priority, reverse=True)
        self.min_text_chars = min_text_chars

    @classmethod
    def from_dict(cls, data: dict) -> "RuleSet":
        cats = []
        for key, spec in (data.get("categories") or {}).items():
            cats.append(Category(
                key=key,
                label=str(spec.get("label") or key.replace("_", " ").title()),
                severity=str(spec.get("severity", "high")),
                priority=int(spec.get("priority", 0)),
                manual_attention=bool(spec.get("manual_attention", False)),
                any=[_norm_phrase(p) for p in spec.get("any", []) if p.strip()],
                all=[_norm_phrase(p) for p in spec.get("all", []) if p.strip()],
                none=[_norm_phrase(p) for p in spec.get("none", []) if p.strip()],
            ))
        if not cats:
            raise ValueError("rules file defines no categories")
        return cls(cats, int(data.get("min_text_chars", 12)))

    def match_all(self, text: str) -> list[Match]:
        normalized = normalize_text(text)
        if len(normalized) < self.min_text_chars:
            return []
        out = []
        for cat in self.categories:
            hits = cat.match(normalized)
            if hits:
                out.append(Match(cat, hits, text))
        return out

    def match(self, text: str) -> Optional[Match]:
        """Highest-priority match, or None."""
        matches = self.match_all(text)
        return matches[0] if matches else None

    def category(self, key: str) -> Optional[Category]:
        return next((c for c in self.categories if c.key == key), None)


def load_rules(path: Path) -> RuleSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RuleSet.from_dict(data)
