"""Extraction Draft — one document's fields, held for review."""

from __future__ import annotations

import frappe
from frappe.model.document import Document


class ExtractionDraft(Document):
    """A draft, never a write. ``promote_extraction`` is the only thing that
    touches the target document, and it records the prior values so a revert
    is a real inverse."""
