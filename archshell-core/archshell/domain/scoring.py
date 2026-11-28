"""Provisional pattern scoring: drivers -> one proposal (story 2.1 tracer).

Pure (AD-4), `Decimal` only. Two provisional cards are fixed domain data;
story 2.3 replaces them with the real pattern cards (and 2.4 the scoring).
Clean + Screaming scores higher when the drivers name two or more modules,
Clean otherwise. Ties break by card id in codepoint order, so the same drivers
always give the same proposal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# A driver-establishing request needs at least this many business modules for
# the module-per-folder (Screaming) layout to pay off.
SCREAMING_MIN_MODULES = 2


@dataclass(frozen=True)
class Card:
    """A provisional pattern card: the patterns it proposes and how it scores."""

    id: str
    title: str
    patterns: tuple[str, ...]
    score: Callable[[Mapping[str, Any]], Decimal]


@dataclass(frozen=True)
class Proposal:
    card: str
    title: str
    patterns: tuple[str, ...]
    scores: tuple[tuple[str, Decimal], ...]


def _module_count(drivers: Mapping[str, Any]) -> int:
    modules = drivers.get("modules")
    return len(modules) if isinstance(modules, list) else 0


CARDS: tuple[Card, ...] = (
    Card("clean", "Clean", ("clean",), lambda drivers: Decimal("0.5")),
    Card(
        "clean-screaming",
        "Clean + Screaming",
        ("clean", "screaming"),
        lambda drivers: Decimal("1")
        if _module_count(drivers) >= SCREAMING_MIN_MODULES
        else Decimal("0"),
    ),
)


def propose(drivers: Mapping[str, Any], cards: tuple[Card, ...] = CARDS) -> Proposal:
    """The highest-scoring card; a tie goes to the smaller card id (codepoints)."""
    if not cards:
        raise ValueError("no pattern cards to score")
    scored = [(card.score(drivers), card) for card in cards]
    for score, card in scored:
        if not isinstance(score, Decimal):
            raise TypeError(f"card {card.id} scored a {type(score).__name__}, not a Decimal")
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    best = scored[0][1]
    scores = tuple(sorted((card.id, score) for score, card in scored))
    return Proposal(best.id, best.title, best.patterns, scores)
