"""The installer — what has to exist before this extension is usable.

Two things, both idempotent, because a restored site repairs itself on the next
migrate rather than failing on a missing document the first time somebody opens
the widget.

The role is the load-bearing half.  ``Extraction Draft`` grants read/write to
``Document Intelligence MCP User``, and that row is the gate: an account may
use these tools when it holds that role.  Creating it here rather than in a
fixture means a site restored from a backup gets it back.

The settings row is the other half, and it is deliberately *restrictive*: no
provider configured, no auto-accept, retention at its shortest.  An extension
that installed itself ready to send documents to a model would be an extension
that made a decision the operator never made.
"""

from __future__ import annotations

import frappe

ROLE = "Document Intelligence MCP User"
SETTINGS = "Document Intelligence MCP Settings"


def after_install() -> None:
    _ensure_role()
    _ensure_settings()


def after_migrate() -> None:
    """Called on every migrate, so a restored site repairs itself."""
    _ensure_role()
    _ensure_settings()


def _ensure_role() -> None:
    if frappe.db.exists("Role", ROLE):
        return
    frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(
        ignore_permissions=True
    )


def _ensure_settings() -> None:
    """The Single settings row, restrictive by default.

    ``data_leaves_site`` is 0 and there is no way to extract without setting
    it: the operator has to say out loud that document text will be sent to a
    provider.  A default of 1 would be a decision made on their behalf.
    """
    if frappe.db.exists(SETTINGS, SETTINGS):
        return
    frappe.get_doc(
        {
            "doctype": SETTINGS,
            # No provider until the operator configures one.  A site with no
            # provider is a site where every extraction fails with a sentence
            # an operator can act on, rather than half-working.
            "provider": "",
            "model": "",
            # Review by default.  The gate fails closed.
            "auto_accept": 0,
            "confidence_threshold": 0.85,
            # Fields that may never be auto-accepted, whatever the confidence.
            "never_auto_fields": "grand_total\ncustomer\ndue_date",
            # The shortest retention that is still useful.
            "retention_days": 30,
            # Off until the operator says otherwise.
            "data_leaves_site": 0,
            # The most bytes one extraction will read.
            "max_file_bytes": 8388608,
        }
    ).insert(ignore_permissions=True)
