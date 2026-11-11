"""What this extension contributes to the core — the whole contract.

An extension is a Frappe app that implements the ``frappe_mcp_tools`` hook.
This module is what that hook points at: ``TOOLS`` (a list of ``ToolSpec``)
and ``TIERS`` (every name classified).  The core validates both atomically and
absorbs them tagged with this app's name, so uninstalling removes exactly these
tools and nothing else.

Why the surface looks like this
-------------------------------
Seven tools and **no flow**, and the absence is the deliberate half.

``healthcare-mcp`` ships one flow — ``admit_patient`` — because admitting a
patient is three documents that must all succeed or none may.  Extraction is
the opposite shape: *extract*, then *review*, then *promote*, and the middle
step is a **human decision**.  A flow is several tool calls with a transaction
of its own, and there is no transaction that spans a person reading a draft.
Making it one would be a way of skipping the review, which is the entire point
of the draft-then-promote gate.

So the steps are separate tools, and the gate between them is a permission and
an audit row rather than a rollback.

Every write says how it is undone
---------------------------------
``extract_document`` is undone by ``discard_extraction`` — a draft nobody
promoted is a draft that can be thrown away.  ``promote_extraction`` is undone
by ``revert_extraction``, which restores the values the draft recorded before
it overwrote them.  The three terminal actions — revert, reject, discard — are
irreversible and say why, rather than hiding behind prose.

Nothing here imports Frappe
---------------------------
The manifest is the contract, and the contract must be importable on a machine
with none of this app's dependencies installed — that is what lets the core
validate it before anything runs.  The handlers live in ``extraction.py``.
"""

from __future__ import annotations

from albanna_ai_chat.core.flow import CompensationSpec
from albanna_ai_chat.core.tools import READ, WRITE, ToolSpec

#: The DocTypes this extension owns.  One module, and the doctypes its domain
#: actually has — not one doctype per tool.
EXTRACTION_DRAFT = "Extraction Draft"
SETTINGS = "Document Intelligence MCP Settings"


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


_FILE_ARG = {
    "file": {
        "type": "string",
        "description": (
            "The file to extract from — its URL (/files/... or /private/files/...) "
            "or the name of its File record. Never a path."
        ),
    }
}

_FILTERS_ARG = {
    "filters": {
        "type": "array",
        "description": (
            "Filter conditions, each one exactly three strings: "
            "[fieldname, operator, value]. Omit to list everything."
        ),
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
    }
}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

LIST_EXTRACTIONS = ToolSpec(
    name="list_extractions",
    description=(
        "Find extraction drafts on this site. Use this to answer 'what is "
        "waiting for review', 'what did we extract from that invoice', 'which "
        "extractions failed'. Returns a compact list of drafts with their "
        "status and confidence."
    ),
    parameters=_schema({**_FILTERS_ARG}, []),
    tier=READ,
)

GET_EXTRACTION = ToolSpec(
    name="get_extraction",
    description=(
        "Fetch one extraction draft by name, with every field it proposes, "
        "each field's confidence, and which model and prompt produced it. Use "
        "this before promoting, so the reader sees exactly what would change."
    ),
    parameters=_schema(
        {
            "name": {
                "type": "string",
                "description": "The draft's name, e.g. 'EXT-0001'.",
            }
        },
        ["name"],
    ),
    tier=READ,
)


# --------------------------------------------------------------------------
# The write that starts the work
# --------------------------------------------------------------------------

EXTRACT_DOCUMENT = ToolSpec(
    name="extract_document",
    description=(
        "Extract fields from an uploaded document into a draft, ready for "
        "review. Use this when the user has uploaded an invoice, purchase "
        "order or resume and wants its fields read. The extraction runs in the "
        "background — this call returns the draft's name, not the values. "
        "Nothing is written to the target document: the result is a draft a "
        "person reviews and promotes. Say that, rather than implying the "
        "record has been filled in."
    ),
    parameters=_schema(
        {
            **_FILE_ARG,
            "target_doctype": {
                "type": "string",
                "description": (
                    "The DocType the extracted fields belong to, e.g. "
                    "'Sales Invoice', 'Purchase Order', 'Job Applicant'."
                ),
            },
            "fields": {
                "type": "array",
                "description": (
                    "The fieldnames to extract, e.g. ['grand_total', "
                    "'customer', 'due_date']. Omit to extract every field the "
                    "extension is configured to read for this DocType."
                ),
                "items": {"type": "string"},
            },
        },
        ["file", "target_doctype"],
    ),
    tier=WRITE,
    doctype_arg="target_doctype",
    write_permission="create",
    # A draft nobody promoted is a draft that can be thrown away, and
    # discard_extraction is the tool that throws it away.
    compensation=CompensationSpec(mechanism="reverse", undo_with=("discard_extraction",)),
)


# --------------------------------------------------------------------------
# The write that ends it — the human decision
# --------------------------------------------------------------------------

PROMOTE_EXTRACTION = ToolSpec(
    name="promote_extraction",
    description=(
        "Apply a reviewed extraction draft to its target document. Use this "
        "only after the reader has seen the draft and agreed to it — never on "
        "a draft they have not read. For a new document this creates it; for "
        "an existing one it writes only the fields the draft proposes, and "
        "records what they were before so the change can be reverted."
    ),
    parameters=_schema(
        {
            "name": {
                "type": "string",
                "description": "The draft's name, e.g. 'EXT-0001'.",
            },
            "target_name": {
                "type": "string",
                "description": (
                    "The document to write into, when the draft updates an "
                    "existing one. Omit to create a new document."
                ),
            },
        },
        ["name"],
    ),
    tier=WRITE,
    doctype_arg="target_doctype",
    write_permission="write",
    # The draft records the prior values, so reverting is a real inverse and
    # not a claim.
    compensation=CompensationSpec(mechanism="reverse", undo_with=("revert_extraction",)),
)

REVERT_EXTRACTION = ToolSpec(
    name="revert_extraction",
    description=(
        "Undo a promotion: restore the values the draft recorded before it "
        "overwrote them, or delete the document the promotion created. Use "
        "this when a promoted extraction turns out to have been wrong."
    ),
    parameters=_schema(
        {"name": {"type": "string", "description": "The draft's name."}},
        ["name"],
    ),
    tier=WRITE,
    write_permission="write",
    # Irreversible, and it says why: reverting a revert would need the
    # promoted values, which the revert has already replaced.
    compensation=CompensationSpec(
        mechanism="none",
        reason=(
            "Reverting replaces the promoted values with the prior ones. "
            "Re-reverting would need the promoted values, which this action "
            "has already overwritten; the draft's own history is the record."
        ),
    ),
)

REJECT_EXTRACTION = ToolSpec(
    name="reject_extraction",
    description=(
        "Reject an extraction draft, with a reason. Use this when the reader "
        "has looked at the draft and decided it is wrong. The reason is kept "
        "as a labelled example, so the next extraction of a similar document "
        "can be shown what was wrong."
    ),
    parameters=_schema(
        {
            "name": {"type": "string", "description": "The draft's name."},
            "reason": {
                "type": "string",
                "description": "Why the draft is wrong, in the reader's words.",
            },
        },
        ["name", "reason"],
    ),
    tier=WRITE,
    write_permission="write",
    # A rejection is a record, not a state to undo: un-rejecting would lose
    # the reason, which is the only part with any value.
    compensation=CompensationSpec(
        mechanism="none",
        reason=(
            "A rejection is kept as a labelled example of what was wrong. "
            "Un-rejecting it would discard the reason, which is the only part "
            "worth keeping."
        ),
    ),
)

DISCARD_EXTRACTION = ToolSpec(
    name="discard_extraction",
    description=(
        "Discard an extraction draft that was never promoted. Use this to "
        "clear a draft the reader does not want. A draft that has already "
        "been promoted must be reverted instead, not discarded."
    ),
    parameters=_schema(
        {"name": {"type": "string", "description": "The draft's name."}},
        ["name"],
    ),
    tier=WRITE,
    write_permission="delete",
    compensation=CompensationSpec(
        mechanism="none",
        reason=(
            "A discarded draft is deleted. Its proposed values are not kept "
            "anywhere, because a draft nobody wanted is not an example of "
            "anything."
        ),
    ),
)


#: Every tool this extension contributes.
TOOLS = [
    LIST_EXTRACTIONS,
    GET_EXTRACTION,
    EXTRACT_DOCUMENT,
    PROMOTE_EXTRACTION,
    REVERT_EXTRACTION,
    REJECT_EXTRACTION,
    DISCARD_EXTRACTION,
]

#: Every tool classified.  The validator refuses a tool that is not here, so
#: this dict and TOOLS are kept in step by a test rather than by memory.
TIERS = {
    "list_extractions": READ,
    "get_extraction": READ,
    "extract_document": WRITE,
    "promote_extraction": WRITE,
    "revert_extraction": WRITE,
    "reject_extraction": WRITE,
    "discard_extraction": WRITE,
}

#: The core version range this extension is built against.
#:
#: ``0.3.0`` is the first core version carrying the four primitives this
#: extension depends on — the async job contract, the binary file port, the
#: proposal gate and the restricted template engine all landed in it.  An
#: earlier core has none of them, so declaring a lower bound below 0.3.0 would
#: promise a compatibility that cannot exist.
REQUIRES_CORE = ">=0.3.0,<0.4.0"
