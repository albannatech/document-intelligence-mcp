"""Extraction Field — one field a model proposed."""

from __future__ import annotations

import frappe
from frappe.model.document import Document


class ExtractionField(Document):
    """``NOT_FOUND`` and ``AMBIGUOUS`` are values, not absences: a field the
    model could not read is a field the reader must be told about."""
