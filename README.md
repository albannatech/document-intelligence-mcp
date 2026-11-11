# Document Intelligence MCP

Extract fields from invoices, purchase orders and resumes — **the reference
extension for the four primitives** the `frappe-mcp` core supplies.

An extension to [`albanna-ai-chat`](https://github.com/albannatech/albanna-ai-chat).
Install the core first; this app contributes tools, an agent and prompts to it.

```bash
bench get-app https://github.com/albannatech/document-intelligence-mcp.git
bench --site {site} install-app document_intelligence_mcp
```

---

## What it demonstrates

This extension exists to prove that the core's four primitives are enough to
build a real one. Each of the five reference extensions specified the same
defect in different clothes; this one exercises all four corrections at once.

| Primitive | Where it is exercised |
|---|---|
| **Async + hook prohibition** | `extract_document` takes a privilege snapshot and enqueues. It never reads the file, never calls a provider, never writes the target. |
| **Binary file port** | The job reads bytes through the core's port — permission-checked before a byte is fetched, capped, never given a path. |
| **Draft-then-promote** | Extraction produces a draft. `promote_extraction` is a separate, permissioned, audited action a person takes after reading it. |
| **Restricted templates** | The extraction prompt is a restricted template; the document text arrives fenced with a standing instruction that it is data and never instructions. |

### The shape of the surface

Seven tools and **no flow** — and the absence is the deliberate half.

`healthcare-mcp` ships one flow, `admit_patient`, because admitting a patient is
three documents that must all succeed or none may. Extraction is the opposite
shape: *extract*, then *review*, then *promote*, and the middle step is a
**human decision**. A flow is several tool calls with a transaction of its own,
and there is no transaction that spans a person reading a draft. Making it one
would be a way of skipping the review.

| Tool | Tier | Undone by |
|---|---|---|
| `list_extractions` | read | — |
| `get_extraction` | read | — |
| `extract_document` | write | `discard_extraction` |
| `promote_extraction` | write | `revert_extraction` |
| `revert_extraction` | write | irreversible — says why |
| `reject_extraction` | write | irreversible — says why |
| `discard_extraction` | write | irreversible — says why |

Every write declares its compensation, and a test asserts the tool it names
actually exists in the registry.

---

## What it will not do

- **Never promote a draft the reader has not seen.** Extraction produces a
  draft; a person decides.
- **Never guess a value.** A missing field is `NOT_FOUND`; an ambiguous one is
  `AMBIGUOUS` with a reason. Both are *values*, not absences — a field the
  model could not read is a field the reader must be told about.
- **Never read a file the caller may not read.** The port refuses, and a
  refusal is an answer rather than an error to work around.
- **Never send a document to a provider the operator has not configured**, and
  never without `Data Leaves Site` being set in Settings. The installer ships
  it **off**.

---

## Configuration

`Document Intelligence MCP Settings` (Single), restrictive by default:

| Field | Default | Why |
|---|---|---|
| `provider` / `model` | empty | No provider until configured. A site with none fails with a sentence an operator can act on. |
| `auto_accept` | **off** | The gate fails closed. |
| `confidence_threshold` | 0.85 | Only meaningful with auto-accept on. |
| `never_auto_fields` | `grand_total`, `customer`, `due_date` | Never auto-accepted, whatever the confidence. |
| `retention_days` | 30 | Drafts are an audit trail, not a business record. |
| `data_leaves_site` | **off** | The operator says out loud that document text is sent to a provider. |
| `max_file_bytes` | 8 MiB | The ceiling on one read. |

---

## Testing

```bash
python -m pytest tests/
```

41 tests. They import the **real core** from a sibling checkout and run this
manifest through the core's own validator — a reference extension that only
tests itself proves nothing about whether the seam works.

The core is expected at `../albanna-ai-chat`; see `pythonpath` in
`pyproject.toml`.

---

## Requires core

`>=0.3.0,<0.4.0` — the first core version carrying the four primitives. An
earlier core has none of them.
