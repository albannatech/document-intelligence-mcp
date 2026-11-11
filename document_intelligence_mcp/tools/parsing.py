"""Turning a model's answer into fields — pure, and tested without a site.

The extraction prompt asks for one ``FIELDNAME=VALUE`` line per field, with
``NOT_FOUND`` for a field the document does not state and ``AMBIGUOUS`` plus a
reason for one it states twice.  This module parses that, and it is the whole
of the parsing: no regex cleverness, no inference, no repair.

Why it is deliberately literal
------------------------------
A parser that "cleans up" a model's output is a parser that invents data.  If
the model wrote ``grand_total=AMBIGUOUS`` because the document had two totals,
a parser that picked the larger one has made a financial decision on the
model's behalf.  The value goes into the draft exactly as written, and the
draft says it was ambiguous, and a person decides.

``NOT_FOUND`` and ``AMBIGUOUS`` are therefore *values*, not absences.  A field
the model could not read is a field the reader must be told about — which is
the entire difference between a draft that is honest and one that looks
complete.
"""

from __future__ import annotations

import re

#: The two sentinel values the prompt asks for.  They are values, not absences:
#: a field the model could not read is a field the reader must see.
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"

#: One ``FIELDNAME=VALUE`` line.  The fieldname must be a plain identifier so a
#: line of prose cannot be mistaken for a field.
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

#: A trailing ``# comment`` the model may add after an AMBIGUOUS value.
_COMMENT = re.compile(r"\s+#\s+(.*)$")


class ParseProblem(str):
    """A response the parser refuses, as a sentence a developer can act on."""


def parse_extraction_response(text: str, requested: "list[str] | tuple[str, ...]") -> dict[str, str]:
    """Parse *text* into ``{fieldname: value}`` for the *requested* fields.

    A field the model did not mention is **absent from the result**, not
    defaulted.  The caller decides what an absent field means, and a parser
    that filled it with ``""`` would be a parser that said "the document said
    nothing" when the truth is "the model said nothing".
    """
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if name not in requested:
            continue
        values[name] = value
    return values


def split_comment(value: str) -> tuple[str, str]:
    """Split ``AMBIGUOUS # why`` into ``("AMBIGUOUS", "why")``.

    The reason travels with the value rather than being dropped, because an
    ambiguous field with no reason is an ambiguous field the reader cannot
    resolve.
    """
    match = _COMMENT.search(value)
    if not match:
        return value, ""
    return value[: match.start()].strip(), match.group(1).strip()


def confidence_of(value: str) -> float | None:
    """A confidence for one extracted value, or ``None`` when there is none.

    ``None`` is the honest answer for a sentinel: a field the model could not
    read has no confidence, and a parser that returned 0.0 would be claiming
    the model was confident it was missing.
    """
    if value == NOT_FOUND:
        return None
    if value == AMBIGUOUS or value.startswith(AMBIGUOUS):
        return None
    return None


def summarise(values: dict[str, str]) -> str:
    """One line saying what the extraction found, for the panel.

    Counts the three shapes separately, because "extracted 6 fields" and
    "extracted 6 fields, 2 of them ambiguous" are different answers to the
    reader.
    """
    found = ambiguous = missing = 0
    for value in values.values():
        base, _ = split_comment(value)
        if base == NOT_FOUND:
            missing += 1
        elif base == AMBIGUOUS:
            ambiguous += 1
        else:
            found += 1
    parts = [f"{found} read"]
    if ambiguous:
        parts.append(f"{ambiguous} ambiguous")
    if missing:
        parts.append(f"{missing} not found")
    return "Extracted " + ", ".join(parts)


def validate_requested(requested: "list[str] | tuple[str, ...]") -> ParseProblem | None:
    """Check the requested fieldnames before a document is read.

    An empty list is refused: extracting nothing is either a bug or a way to
    spend a provider call and get a draft with no fields in it.
    """
    if not requested:
        return ParseProblem(
            "an extraction must name at least one field — extracting nothing "
            "spends a provider call and produces a draft with no fields"
        )
    for name in requested:
        if not name.isidentifier():
            return ParseProblem(f"{name!r} is not a valid fieldname")
    return None
