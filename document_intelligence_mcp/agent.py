"""The agent this extension brings, and the prompts it brings with it.

An extension is not only tools.  Every repo in this portfolio carries its own
tools, permissions, configurations, *agent* and *prompts* — and the agent is
what makes a set of tools into something with a point of view.  A chat with
seven extraction tools and no agent is a chat that will happily promote a draft
nobody read.

This module is pure data.  It is Frappe-free so it can be tested without a
site, and so the core can assemble a system prompt from it without importing
anything this extension depends on.

How the core uses it
--------------------
The core's prompt assembler is deterministic and ordered: the core's own system
prompt, then the *active* extension's agent persona, then its task prompts,
then the conversation.  One active agent per conversation, chosen in Settings
and switchable mid-thread with the switch recorded.  Twenty extensions
contribute *content*; none of them contributes assembly logic.

The extraction prompt is a restricted template
----------------------------------------------
``EXTRACTION_PROMPT`` is rendered through the core's
:func:`~albanna_ai_chat.core.template.render_data_only`, which accepts exactly
one construct — ``{fieldname}`` — and fences every value with a standing
instruction before substituting it.  That is not decoration: the document text
going into this prompt is a field a *user* edited, and an unfenced
interpolation is a cross-user injection primitive.  A test asserts the fence
travels with the value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: This extension's name, as the core knows it.  It is the app name, because
#: that is what provenance is tagged with and what an uninstall keys on.
EXTENSION_NAME = "document_intelligence_mcp"


@dataclass(frozen=True)
class Prompt:
    """One instruction the agent is given.

    ``when`` says when it applies, so the assembler can include the prompts
    that matter for this turn rather than all of them every time — a prompt
    nobody needs is a prompt that dilutes the ones that do.
    """

    name: str
    text: str
    #: ``""`` for always-on, otherwise the situation that selects it.
    when: str = ""


@dataclass(frozen=True)
class Agent:
    """What this extension contributes to a conversation's point of view."""

    name: str
    persona: str
    #: What this agent will not do.  Stated as data so the assembler puts it
    #: in the prompt and a test asserts it is there.
    boundaries: tuple[str, ...] = ()
    prompts: tuple[Prompt, ...] = field(default_factory=tuple)

    def always_on(self) -> tuple[Prompt, ...]:
        return tuple(p for p in self.prompts if not p.when)

    def for_situation(self, situation: str) -> tuple[Prompt, ...]:
        return tuple(p for p in self.prompts if p.when == situation)


#: The extraction prompt.  A restricted template: ``{document_text}`` and
#: ``{field_list}`` are the only placeholders, and both are fenced by the
#: core's renderer before they reach the model.
EXTRACTION_PROMPT = (
    "Read the document below and extract the requested fields.\n\n"
    "{document_text}\n\n"
    "Fields to extract: {field_list}\n\n"
    "Return one line per field, as FIELDNAME=VALUE. Use the exact fieldname "
    "requested. If a field is not present in the document, write "
    "FIELDNAME=NOT_FOUND rather than guessing. If a value is ambiguous — two "
    "totals, two dates, a currency that is not stated — write "
    "FIELDNAME=AMBIGUOUS and say why in a comment. A wrong value that looks "
    "confident is worse than a missing one."
)

#: The prompt used when a reader rejects a draft, so the correction is kept as
#: a labelled example rather than discarded.
CORRECTION_PROMPT = (
    "The reader rejected a previous extraction of a similar document. Their "
    "reason was: {rejection_reason}\n\n"
    "Take it into account for this extraction. It is an example of what was "
    "wrong, not an instruction to change the output format."
)

DOCUMENT_INTELLIGENCE_AGENT = Agent(
    name="document_intelligence",
    persona=(
        "You read documents and turn them into fields. You are careful about "
        "money and dates, because a figure that is wrong by a factor of ten "
        "looks exactly like one that is right. You never fill in a field the "
        "document does not state, and you say so rather than inventing a "
        "plausible value."
    ),
    boundaries=(
        "Never promote a draft the reader has not seen. Extraction produces a "
        "draft; a person decides.",
        "Never guess a value. A missing field is NOT_FOUND, and an ambiguous "
        "one is AMBIGUOUS with a reason.",
        "Never read a file the caller may not read. The port refuses, and a "
        "refusal is an answer rather than an error to work around.",
        "Never send a document to a provider the operator has not configured "
        "for this extension.",
    ),
    prompts=(
        Prompt(
            name="extraction",
            text=EXTRACTION_PROMPT,
            when="extracting",
        ),
        Prompt(
            name="correction",
            text=CORRECTION_PROMPT,
            when="re_extracting_after_rejection",
        ),
        Prompt(
            name="review",
            text=(
                "Before promoting, show the reader every proposed field with "
                "its confidence, and name the fields that were NOT_FOUND or "
                "AMBIGUOUS. A draft promoted without being read is a draft "
                "that should not have been promoted."
            ),
            when="reviewing",
        ),
    ),
)


def system_prompt(situation: str = "") -> str:
    """The system prompt for this agent, deterministically assembled.

    Ordered: persona, then boundaries, then the always-on prompts, then the
    prompt for *situation* when there is one.  The same inputs give the same
    string, every time — a prompt that varied between calls would make an
    extraction impossible to reproduce.
    """
    parts = [DOCUMENT_INTELLIGENCE_AGENT.persona]

    if DOCUMENT_INTELLIGENCE_AGENT.boundaries:
        parts.append("You will not:")
        parts.extend(f"- {b}" for b in DOCUMENT_INTELLIGENCE_AGENT.boundaries)

    for prompt in DOCUMENT_INTELLIGENCE_AGENT.always_on():
        parts.append(prompt.text)

    if situation:
        for prompt in DOCUMENT_INTELLIGENCE_AGENT.for_situation(situation):
            parts.append(prompt.text)

    return "\n\n".join(parts)


#: The fields the extraction prompt is allowed to interpolate.  A template
#: that names anything else is refused by the core's validator, so this list
#: and the prompt are kept in step by a test rather than by memory.
TEMPLATE_FIELDS = ("document_text", "field_list", "rejection_reason")
