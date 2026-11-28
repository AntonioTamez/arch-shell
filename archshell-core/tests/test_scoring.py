"""Provisional pattern scoring (story 2.1): both proposals, ties, and determinism."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import archshell
from archshell.domain import charter, scoring
from archshell.domain.scoring import Card, propose

SCHEMAS = Path(archshell.__file__).resolve().parent / "schemas"


def modules(*names: str) -> dict:
    return {"modules": [{"name": name, "capabilities": []} for name in names]}


@pytest.mark.parametrize(
    ("drivers", "card", "patterns"),
    [
        (modules("Links"), "clean", ("clean",)),
        (modules("Appointments", "Billing"), "clean-screaming", ("clean", "screaming")),
        (modules("Appointments", "Patients", "Billing"), "clean-screaming", ("clean", "screaming")),
        ({**modules("Links"), "domain_complexity": "high", "scale": "large"}, "clean", ("clean",)),
        ({}, "clean", ("clean",)),
    ],
)
def test_proposal_table(drivers, card, patterns):
    proposal = propose(drivers)
    assert (proposal.card, proposal.patterns) == (card, patterns)
    assert all(isinstance(score, Decimal) for _, score in proposal.scores)


def test_titles_name_the_patterns():
    assert propose(modules("Links")).title == "Clean"
    assert propose(modules("A", "B")).title == "Clean + Screaming"


def test_same_drivers_give_the_same_proposal():
    drivers = modules("Appointments", "Patients", "Billing")
    proposals = {propose(json.loads(json.dumps(drivers))) for _ in range(20)}
    assert len(proposals) == 1


def test_scores_are_reported_by_card_id():
    assert propose(modules("A", "B")).scores == (
        ("clean", Decimal("0.5")), ("clean-screaming", Decimal("1"))
    )


def test_a_tie_goes_to_the_smaller_card_id_in_codepoint_order():
    same = lambda drivers: Decimal("1")  # noqa: E731
    cards = (Card("b", "B", ("screaming",), same), Card("B", "Upper", ("clean",), same),
             Card("a", "A", ("clean",), same))
    assert propose({}, cards).card == "B"  # "B" (U+0042) sorts before "a" and "b"
    assert propose({}, tuple(reversed(cards))).card == "B"


def test_a_card_must_score_a_decimal():
    with pytest.raises(TypeError):
        propose({}, (Card("x", "X", ("clean",), lambda drivers: 1.0),))
    with pytest.raises(ValueError):
        propose({}, ())


def test_schemas_hold_the_domain_driver_enums():
    for name in ("drivers-v1.json", "charter-v1.json"):
        schema = json.loads((SCHEMAS / name).read_bytes().decode("utf-8"))
        properties = schema["properties"]
        drivers = properties if name == "drivers-v1.json" else properties["drivers"]["properties"]
        found = {key: tuple(value["enum"]) for key, value in drivers.items() if key != "modules"}
        assert found == charter.DRIVERS, name
        assert all("unknown" in values for values in found.values())


def test_every_card_pattern_is_a_charter_pattern():
    schema = json.loads((SCHEMAS / "charter-v1.json").read_bytes().decode("utf-8"))
    allowed = set(schema["properties"]["patterns"]["items"]["enum"])
    assert {p for card in scoring.CARDS for p in card.patterns} == allowed


def test_a_kept_proposal_reads_back_as_scored_without_scores():
    proposal = propose(modules("A", "B"))
    kept = json.loads(json.dumps(scoring.as_result(proposal)))
    assert kept == {"card": "clean-screaming", "title": "Clean + Screaming",
                    "patterns": ["clean", "screaming"]}
    back = scoring.from_result(kept)
    assert (back.card, back.title, back.patterns, back.scores) == (
        proposal.card, proposal.title, proposal.patterns, ()
    )
