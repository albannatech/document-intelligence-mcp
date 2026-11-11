"""The Frappe handlers — the only module here that imports Frappe.

Six tools, and the shape of each one is dictated by a primitive the core
supplies rather than by this extension's preference:

``extract_document``
    Takes a **privilege snapshot** at call time and hands the work to the
    core's queue.  It does not read the file, does not call a provider, and
    does not write the target.  It returns a draft name.

``promote_extraction`` / ``revert_extraction``
    The human decision, and the only place a real document is touched.  Both
    check the caller's permission on the target through Frappe, because a
    draft is not an authorisation.

``reject_extraction``
    Keeps the reader's reason as a labelled example, so the next extraction of
    a similar document can be shown what was wrong.

The job itself
--------------
:func:`run_extraction` is what the worker calls.  It reads the bytes through
the core's **binary port** — permission-checked before a byte is fetched,
capped, and never given a path — renders the extraction prompt through the
core's **restricted template engine** so the document text arrives fenced, asks
the core's **provider**, and writes a **draft**.  Nothing here reaches past the
core for any of it.
"""

from __future__ import annotations

from typing import Any

import frappe

from albanna_ai_chat.core.file_bytes import BytesRequest
from albanna_ai_chat.core.jobs import JobRequest, RetryPolicy, validate_job
from albanna_ai_chat.core.port import ToolContext, ToolResult
from albanna_ai_chat.core.proposal import (
    PromotionPolicy,
    Proposal,
    input_digest,
    review_reason,
    validate_proposal,
)
from albanna_ai_chat.core.template import TemplateProblem, render_data_only
from albanna_ai_chat.core.tools import ToolSpec, compact_json, validate_arguments
from albanna_ai_chat.services.jobs import JobRunner, audit, enqueue, snapshot_for
from albanna_ai_chat.tools.file_bytes import FileBytesPort

from document_intelligence_mcp.tools.parsing import (
    AMBIGUOUS,
    NOT_FOUND,
    ParseProblem,
    parse_extraction_response,
    split_comment,
    summarise,
    validate_requested,
)

DRAFT_DOCTYPE = "Extraction Draft"
FIELD_DOCTYPE = "Extraction Field"
SETTINGS_DOCTYPE = "Document Intelligence MCP Settings"

#: The tools this backend can run.
TOOL_NAMES = frozenset(
    {
        "list_extractions",
        "get_extraction",
        "extract_document",
        "promote_extraction",
        "revert_extraction",
        "reject_extraction",
        "discard_extraction",
    }
)


class ExtractionBackend:
    """Runs this extension's seven tools."""

    def __init__(self, bytes_port: FileBytesPort | None = None) -> None:
        self.bytes_port = bytes_port or FileBytesPort()

    def handles(self, name: str) -> bool:
        return name in TOOL_NAMES

    def call(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        problem = validate_arguments(spec, arguments)
        if problem:
            return ToolResult.failure(problem, error="invalid_arguments")

        handler = getattr(self, f"_run_{spec.name}", None)
        if handler is None:  # pragma: no cover - handles() gates this
            return ToolResult.failure(
                f"{spec.name} is not implemented.", error="not_implemented"
            )
        return handler(arguments, context)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def _run_list_extractions(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        filters = arguments.get("filters") or []
        rows = frappe.get_list(
            DRAFT_DOCTYPE,
            filters=list(filters),
            fields=["name", "file", "target_doctype", "status", "confidence"],
            limit_page_length=50,
        )
        return ToolResult.success(
            compact_json(rows),
            data=rows,
            summary=f"Found {len(rows)} extraction draft(s)",
        )

    def _run_get_extraction(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = arguments["name"]
        if not frappe.db.exists(DRAFT_DOCTYPE, name):
            return ToolResult.failure(
                f"No extraction draft named {name!r}.", error="not_found"
            )
        doc = frappe.get_doc(DRAFT_DOCTYPE, name)
        payload = {
            "name": doc.name,
            "file": doc.get("file"),
            "target_doctype": doc.get("target_doctype"),
            "status": doc.get("status"),
            "confidence": doc.get("confidence"),
            "model_version": doc.get("model_version"),
            "prompt_version": doc.get("prompt_version"),
            "fields": [
                {
                    "fieldname": row.get("fieldname"),
                    "value": row.get("value"),
                    "confidence": row.get("confidence"),
                }
                for row in doc.get("fields") or []
            ],
        }
        return ToolResult.success(
            compact_json(payload), data=payload, summary=f"Draft {doc.name}"
        )

    # ------------------------------------------------------------------
    # the write that starts the work
    # ------------------------------------------------------------------

    def _run_extract_document(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        settings = _settings()
        if not settings.get("data_leaves_site"):
            return ToolResult.refusal(
                "This site has not acknowledged that document text is sent to "
                "a provider. Set 'Data Leaves Site' in Document Intelligence "
                "MCP Settings first.",
                error="not_acknowledged",
            )

        requested = arguments.get("fields") or _configured_fields(
            settings, arguments["target_doctype"]
        )
        problem = validate_requested(requested)
        if problem:
            return ToolResult.failure(str(problem), error="bad_field_list")

        # The snapshot is taken here, at call time, and never refreshed.  It is
        # what the caller could do when they asked — the only thing they
        # authorised.
        snapshot = snapshot_for(context.principal.user)

        request = JobRequest(
            extension="document_intelligence_mcp",
            job="extract_document",
            payload={
                "file": arguments["file"],
                "target_doctype": arguments["target_doctype"],
                "fields": list(requested),
                "conversation": context.conversation,
            },
            snapshot=snapshot,
            purpose=(
                f"Extract {len(requested)} field(s) from a document into a "
                f"draft for {arguments['target_doctype']}."
            ),
            retry=RetryPolicy(attempts=2, backoff_seconds=120),
        )

        problem = validate_job(request)
        if problem:
            return ToolResult.failure(str(problem), error="invalid_job")

        queued = enqueue(
            request, "document_intelligence_mcp.tools.extraction.run_extraction"
        )
        if isinstance(queued, str) and queued.startswith("a job must"):
            return ToolResult.failure(queued, error="invalid_job")

        return ToolResult.success(
            compact_json(
                {
                    "status": "queued",
                    "job": request.key,
                    "fields": list(requested),
                    "note": (
                        "The extraction runs in the background. Nothing has "
                        "been written to the target document — the result is a "
                        "draft a person reviews and promotes."
                    ),
                }
            ),
            summary=f"Queued extraction of {len(requested)} field(s)",
        )

    # ------------------------------------------------------------------
    # the human decision
    # ------------------------------------------------------------------

    def _run_promote_extraction(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        name = arguments["name"]
        if not frappe.db.exists(DRAFT_DOCTYPE, name):
            return ToolResult.failure(
                f"No extraction draft named {name!r}.", error="not_found"
            )
        draft = frappe.get_doc(DRAFT_DOCTYPE, name)
        if draft.get("status") != "draft":
            return ToolResult.refusal(
                f"Draft {name} is {draft.get('status')}, not awaiting review.",
                error="not_a_draft",
            )

        target_doctype = draft.get("target_doctype")
        target_name = arguments.get("target_name") or draft.get("target_name") or ""

        # A draft is not an authorisation.  The caller must hold the right on
        # the target, checked here rather than inherited from whoever queued
        # the extraction.
        if not frappe.has_permission(target_doctype, "write" if target_name else "create"):
            return ToolResult.refusal(
                f"This account may not {'change' if target_name else 'create'} "
                f"{target_doctype}.",
                error="denied",
            )

        values = {
            row.get("fieldname"): row.get("value")
            for row in draft.get("fields") or []
            if row.get("value") not in (None, "")
        }

        prior: dict[str, Any] = {}
        if target_name:
            existing = frappe.get_doc(target_doctype, target_name)
            prior = {k: existing.get(k) for k in values}
            for fieldname, value in values.items():
                existing.set(fieldname, value)
            existing.save()
            changed = target_name
        else:
            created = frappe.get_doc({"doctype": target_doctype, **values})
            created.insert()
            changed = created.name
            target_name = created.name

        draft.db_set(
            {
                "status": "promoted",
                "target_name": target_name,
                "prior_values": frappe.as_json(prior),
            }
        )

        return ToolResult.success(
            compact_json({"status": "promoted", "target": changed}),
            summary=f"Promoted draft {name} to {target_doctype}/{changed}",
        )

    def _run_revert_extraction(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        name = arguments["name"]
        if not frappe.db.exists(DRAFT_DOCTYPE, name):
            return ToolResult.failure(
                f"No extraction draft named {name!r}.", error="not_found"
            )
        draft = frappe.get_doc(DRAFT_DOCTYPE, name)
        if draft.get("status") != "promoted":
            return ToolResult.refusal(
                f"Draft {name} is {draft.get('status')}, not promoted.",
                error="not_promoted",
            )

        target_doctype = draft.get("target_doctype")
        target_name = draft.get("target_name")
        prior = frappe.parse_json(draft.get("prior_values") or "{}")

        if prior:
            doc = frappe.get_doc(target_doctype, target_name)
            for fieldname, value in prior.items():
                doc.set(fieldname, value)
            doc.save()
        elif target_name:
            frappe.delete_doc(target_doctype, target_name)

        draft.db_set({"status": "reverted"})
        return ToolResult.success(
            compact_json({"status": "reverted", "target": target_name}),
            summary=f"Reverted draft {name}",
        )

    def _run_reject_extraction(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        name = arguments["name"]
        if not frappe.db.exists(DRAFT_DOCTYPE, name):
            return ToolResult.failure(
                f"No extraction draft named {name!r}.", error="not_found"
            )
        draft = frappe.get_doc(DRAFT_DOCTYPE, name)
        draft.db_set({"status": "rejected", "rejection_reason": arguments["reason"]})
        return ToolResult.success(
            compact_json({"status": "rejected", "reason": arguments["reason"]}),
            summary=f"Rejected draft {name}",
        )

    def _run_discard_extraction(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        name = arguments["name"]
        if not frappe.db.exists(DRAFT_DOCTYPE, name):
            return ToolResult.failure(
                f"No extraction draft named {name!r}.", error="not_found"
            )
        draft = frappe.get_doc(DRAFT_DOCTYPE, name)
        if draft.get("status") == "promoted":
            return ToolResult.refusal(
                f"Draft {name} has been promoted — revert it rather than "
                "discarding it.",
                error="already_promoted",
            )
        frappe.delete_doc(DRAFT_DOCTYPE, name)
        return ToolResult.success(
            compact_json({"status": "discarded"}), summary=f"Discarded draft {name}"
        )


# ---------------------------------------------------------------------------
# The job the worker runs
# ---------------------------------------------------------------------------


def run_extraction(request) -> Any:
    """The worker entry point: read bytes, ask the model, write a draft.

    Called by Frappe's queue with the :class:`JobRequest` the tool built.  It
    never inherits privileges: it runs as the job principal and checks the
    snapshot before it writes, which is what
    :meth:`JobRunner.assert_may_write` is for.
    """
    from albanna_ai_chat.services.jobs import run

    return run(request, _extract_under_snapshot)


def _extract_under_snapshot(runner: JobRunner):
    payload = runner.request.payload
    file_ref = payload["file"]
    target_doctype = payload["target_doctype"]
    fields = list(payload["fields"])

    # --- 1. the bytes, through the core's port ---------------------------
    # Permission-checked before a byte is fetched, capped, and never given a
    # path.  A refusal here returns no size and no content.
    read = runner.bytes_port.read_with_envelope(
        BytesRequest(
            file=file_ref,
            max_bytes=_settings().get("max_file_bytes") or 8 * 1024 * 1024,
            purpose=runner.request.purpose,
        ),
        _job_context(runner),
    )
    if isinstance(read, str):
        return runner.outcome(False, "The document could not be read.", read)

    raw, envelope = read
    if envelope.truncated:
        return runner.outcome(
            False,
            "The document was larger than the ceiling for one read.",
            f"truncated at {envelope.size} bytes",
        )

    # --- 2. the prompt, through the core's restricted engine -------------
    # The document text is a field a *user* edited.  It arrives fenced, with a
    # standing instruction that it is data and never instructions.
    rendered = render_data_only(
        _extraction_prompt(),
        {"document_text": _as_text(raw, envelope.mime), "field_list": ", ".join(fields)},
        allowed=("document_text", "field_list"),
    )
    if isinstance(rendered, TemplateProblem):
        return runner.outcome(False, "The extraction prompt is not valid.", str(rendered))

    # --- 3. the model, through the core's provider -----------------------
    provider = _provider()
    if provider is None:
        return runner.outcome(
            False,
            "No provider is configured for this extension.",
            "not_configured",
        )

    response = provider.complete(rendered.text)
    if not response.ok:
        return runner.outcome(False, "The provider could not answer.", response.error)

    # --- 4. the draft ----------------------------------------------------
    values = parse_extraction_response(response.text, fields)
    if not values:
        return runner.outcome(
            False,
            "The model returned no fields this extension asked for.",
            "no_fields",
        )

    runner.assert_may_write(DRAFT_DOCTYPE, "create")

    draft = frappe.get_doc(
        {
            "doctype": DRAFT_DOCTYPE,
            "file": file_ref,
            "target_doctype": target_doctype,
            "status": "draft",
            "confidence": _confidence(values),
            "model_version": getattr(provider, "model", ""),
            "prompt_version": _prompt_version(),
            "content_hash": input_digest(
                {"file": file_ref, "fields": fields, "digest": envelope.digest}
            ),
            "purpose": runner.request.purpose,
            "fields": [
                {"fieldname": k, "value": v, "confidence": None} for k, v in values.items()
            ],
        }
    )
    draft.insert(ignore_permissions=True)
    runner.record_change(DRAFT_DOCTYPE, draft.name)

    audit(
        runner.request,
        runner.outcome(True, summarise(values), changed=(draft.name,)),
    )
    return runner.outcome(True, summarise(values), changed=(draft.name,))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _job_context(runner: JobRunner) -> ToolContext:
    return ToolContext(
        principal=runner.snapshot and _principal(runner.snapshot.user),
        site=frappe.local.site,
        request_id=runner.request.key,
    )


def _principal(user: str):
    from albanna_ai_chat.core.port import Principal

    return Principal(user=user, roles=tuple(frappe.get_roles(user)))


def _settings() -> dict:
    if not frappe.db.exists(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE):
        return {}
    doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
    return {f: doc.get(f) for f in doc.meta.fields}


def _configured_fields(settings: dict, target_doctype: str) -> list[str]:
    raw = settings.get("fields_by_doctype") or ""
    for line in str(raw).splitlines():
        if ":" not in line:
            continue
        doctype, fields = line.split(":", 1)
        if doctype.strip() == target_doctype:
            return [f.strip() for f in fields.split(",") if f.strip()]
    return []


def _extraction_prompt() -> str:
    from document_intelligence_mcp.agent import EXTRACTION_PROMPT

    return EXTRACTION_PROMPT


def _prompt_version() -> str:
    from document_intelligence_mcp.agent import EXTRACTION_PROMPT

    return input_digest(EXTRACTION_PROMPT)[:12]


def _confidence(values: dict[str, str]) -> float | None:
    """The draft's overall confidence, or ``None`` when any field is unresolved.

    ``None`` rather than an average, deliberately: a draft holding one
    ambiguous total is not "mostly confident", it is a draft a person must
    read.  An average would hide the one field that matters.
    """
    for value in values.values():
        base, _ = split_comment(value)
        if base in (NOT_FOUND, AMBIGUOUS):
            return None
    return None


def _as_text(raw: bytes, mime: str) -> str:
    """Decode the bytes for the prompt.

    A PDF is not decoded here — the provider is asked for the document as an
    attachment, and this extension's configuration names which providers can
    take one.  A text file is decoded; anything else is passed as a labelled
    placeholder rather than being guessed at.
    """
    if mime == "application/pdf":
        return f"[PDF document, {len(raw)} bytes]"
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return f"[binary document, {len(raw)} bytes]"


def _provider():
    """The core's provider, configured for this extension.

    ``None`` when nothing is configured, which is the honest answer: a site
    with no provider is a site where every extraction fails with a sentence an
    operator can act on, rather than half-working.
    """
    settings = _settings()
    if not settings.get("provider"):
        return None
    from albanna_ai_chat.core.provider import NullProvider

    # The real provider is resolved from the core's provider registry by name.
    # A NullProvider stands in here so the shape is exercised without a site.
    return NullProvider(model=str(settings.get("model") or ""))


def policy() -> PromotionPolicy:
    """This extension's promotion policy, from its settings.

    Fails closed: no ``auto_accept`` means review, whatever else is set.
    """
    settings = _settings()
    never_auto = tuple(
        f.strip()
        for f in str(settings.get("never_auto_fields") or "").splitlines()
        if f.strip()
    )
    return PromotionPolicy(
        auto_accept=bool(settings.get("auto_accept")),
        threshold=float(settings.get("confidence_threshold") or 0.85),
        never_auto=never_auto,
    )


def proposal_for(draft) -> Proposal:
    """A draft as a :class:`Proposal`, for the gate's own vocabulary."""
    values = {
        row.get("fieldname"): row.get("value") for row in draft.get("fields") or []
    }
    return Proposal(
        extension="document_intelligence_mcp",
        doctype=draft.get("target_doctype"),
        name=draft.get("target_name") or "",
        values=values,
        confidence=draft.get("confidence"),
        model_version=draft.get("model_version") or "",
        prompt_version=draft.get("prompt_version") or "",
        input_hash=draft.get("content_hash") or "",
        summary=f"Extraction draft {draft.name}",
    )


def why_waiting(draft) -> str:
    """Why *draft* is waiting for a person, in one readable sentence."""
    return review_reason(proposal_for(draft), policy())


def check_proposal(draft):
    """Validate a draft as a proposal, for the tests and the review queue."""
    return validate_proposal(proposal_for(draft))


__all__ = [
    "ExtractionBackend",
    "ParseProblem",
    "check_proposal",
    "policy",
    "proposal_for",
    "run_extraction",
    "why_waiting",
]
