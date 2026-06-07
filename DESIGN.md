# OutboundIQ — Design Document

This document explains **what** OutboundIQ is, **why** it is built the way it is, and the
**design trade-offs** behind every major decision. It is written to be read alongside
[`IMPLEMENTATION.md`](./IMPLEMENTATION.md) (the *how*) and [`INTERVIEW.md`](./INTERVIEW.md)
(anticipated questions).

---

## 1. Problem statement

Turn **public company information** into **outbound strategy**. Two modes:

1. **ICP & value proposition generation** — given a *sender* company's website
   (e.g. `artisan.co`), fetch and analyze public pages to infer a concise value
   proposition and a structured ICP (industries, size bands, triggers, buyers).
2. **Target evaluation & outbound drafting** — given a *target* company's website
   and a recipient persona (role + seniority), research the account from public
   sources, score how well it fits the inferred ICP, draft **two outbound emails**
   with meaningfully different angles, and produce an **evidence panel / claim map**
   that ties every factual claim to a citation.

### The three hard technical requirements (and how the design answers them)

| Requirement | Design response |
|---|---|
| Answers/emails must be based on **retrieved snippets, not full-context stuffing** | A retrieval layer chunks fetched pages into small snippets, keyword-ranks them, and feeds the model only a **bounded, numbered subset**. Every claim must cite a snippet id. |
| Implement an **agentic** solution that *plans → retrieves evidence → finds signals → drafts* | A multi-step pipeline where each step is a discrete LLM call with a single responsibility, plus a real tool-using agent step (Claude `web_search`) for live signal mining. |
| **Optimize for token usage and quality** | Two-tier model routing (cheap model for extraction/search, strong model for synthesis/drafting), strict snippet caps and per-page-kind quotas, compact prompts, and `max_tokens` budgets per call. |

---

## 2. High-level architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Frontend (Vite + React + Tailwind SPA)                            │
│  Mode 1 panel · Mode 2 panel · Evidence drawer · Claim map         │
└───────────────────────────────────────────────────────────────────┘
                    │  JSON over HTTP (/api/*)
                    ▼
┌───────────────────────────────────────────────────────────────────┐
│  Backend (FastAPI)  — server.py                                    │
│   ┌─────────────────────────────────────────────────────────────┐ │
│   │  agent.py — agentic orchestration                           │ │
│   │   Mode 1: analyze_sender()                                  │ │
│   │   Mode 2: research_target_signals → evaluate fit → draft   │ │
│   └─────────────────────────────────────────────────────────────┘ │
│   ┌──────────────────────────┐   ┌──────────────────────────────┐ │
│   │ retrieval.py             │   │ db.py (SQLite)               │ │
│   │  crawl · extract · chunk │   │  senders + evaluations       │ │
│   │  EvidenceStore + ranking │   │                              │ │
│   └──────────────────────────┘   └──────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
        │ httpx fetch (own pages)      │ Anthropic API (LLM + web_search tool)
        ▼                              ▼
   Company websites              Claude models / live web
```

**Separation of concerns** is the organizing principle:

- `retrieval.py` knows nothing about LLMs — it only fetches, extracts, chunks, stores, and ranks evidence.
- `agent.py` knows nothing about HTTP or SQL — it orchestrates LLM calls over evidence.
- `server.py` is a thin transport + persistence boundary.
- `db.py` is pure persistence.

This makes each layer independently testable and swappable (e.g. replace SQLite with Postgres, or swap the keyword ranker for embeddings, without touching the others).

---

## 3. The agentic pipeline

The word "agentic" here means: **the system plans a sequence of steps, decides what
evidence to retrieve, uses tools to gather live signals, and composes intermediate
outputs into a final deliverable** — rather than a single prompt that does everything.

### Mode 1 — Sender analysis (`analyze_sender`)

```
plan → fetch homepage → discover priority pages → fetch in parallel
     → chunk into snippets → select balanced subset → synthesize ICP+value prop (1 LLM call)
     → attach evidence map → persist
```

1. **Plan / crawl.** Fetch the homepage, then *discover* internal links and classify
   them into known page kinds (about, product, pricing, customers, blog, careers,
   contact) using URL-path heuristics. Fetch the priority pages **in parallel**.
2. **Retrieve.** Extract main text (boilerplate-stripped) and chunk it into ~480-char
   snippets stored in an `EvidenceStore`.
3. **Select.** Pick a **token-bounded, representative** snippet set using per-page-kind
   quotas (e.g. 6 home, 6 product, 5 customers…) capped at 26 snippets.
4. **Synthesize.** A single strong-model call produces structured JSON: value prop,
   differentiators, and a structured ICP — **each field citing snippet ids**.
5. **Ground.** The cited ids are resolved back to `{url, title, snippet}` for the UI.

### Mode 2 — Target evaluation + drafting (`evaluate_target`)

```
crawl target site → (cheap LLM + web_search) mine live signals
   → (strong LLM) score fit vs ICP using snippets + signals
   → (strong LLM) draft 2 emails (pain-led, trigger-led) with mandatory claims
   → resolve all citations → build claim map → persist
```

1. **Retrieve target's own pages** (same crawler as Mode 1).
2. **Find signals.** A **cheap model** with Claude's `web_search` tool runs 2–4 queries
   (funding, hiring, launches, news) and returns a strict JSON array of
   `{finding, url, title, date_hint}`. This is the "find signals" step, and it is a
   genuine tool-using agent loop.
3. **Evaluate fit.** A **strong model** scores the target against the sender's ICP across
   four dimensions (Industry, Company size, Pain match, Triggers), each with a rationale
   and evidence references, plus suggested outreach hooks.
4. **Draft.** A **strong model** writes two emails with *meaningfully different angles*
   (pain-led vs trigger-led), each carrying a mandatory `claims` array mapping every
   factual claim about the target to a snippet id or signal URL.
5. **Claim map.** All claims across both emails are flattened and each citation resolved
   to its source, with a `resolved` flag so the UI can flag any unsupported claim.

---

## 4. Key design decisions & trade-offs

### 4.1 Snippet-grounded retrieval, not context stuffing

**Decision.** Never feed full pages to the model. Chunk → store → rank → feed a small
numbered subset; require the model to cite snippet ids.

**Why.**
- **Token cost.** A company site can be tens of thousands of tokens. Snippet selection
  caps each reasoning call to ~26 short snippets.
- **Grounding & verifiability.** Forcing citations to a finite snippet set makes
  hallucination structurally harder and gives us a *claim map* for free — every claim
  resolves to a real URL + text.
- **Quality.** Curated, relevant snippets beat a wall of boilerplate (nav menus, cookie
  banners) the model would otherwise have to wade through.

**Trade-off.** A naive keyword ranker can miss semantically-relevant-but-lexically-different
snippets. Accepted for v1 because it is zero-dependency, instant, and deterministic; the
`search()` API is designed so an embedding ranker can drop in later.

### 4.2 Two-tier model routing (cheap vs strong)

**Decision.** `CHEAP_MODEL` (Haiku) for signal mining / extraction-style work;
`STRONG_MODEL` (Sonnet) for synthesis, fit scoring, and email drafting.

**Why.** Signal mining is mostly "run searches and reformat results" — cheap-model work.
Synthesis and persuasive writing reward the stronger model. This is the core
**token/quality optimization**: spend strong-model tokens only where they move the needle.

**Trade-off.** More moving parts (two model ids). Mitigated by env-driven configuration so
both can be overridden per environment.

### 4.3 Discrete pipeline steps vs one mega-prompt

**Decision.** Separate LLM calls for sender synthesis, signal mining, fit scoring, and
drafting — not one giant prompt.

**Why.**
- **Single responsibility per call** → tighter, smaller prompts → fewer tokens and clearer
  failure isolation.
- **Different inputs/tools per step** (drafting needs fit hooks + signals; signal mining
  needs the web tool).
- **Reusability.** A sender ICP is computed once and reused across many target evaluations
  (persisted in SQLite), so Mode 1's cost is amortized.

**Trade-off.** More round-trips (latency). Acceptable for an interactive analysis tool;
the UI masks it with a staged progress indicator.

### 4.4 Live tool use for external signals (`web_search`)

**Decision.** Use Anthropic's server-side `web_search` tool for the "find signals" step
rather than scraping search engines ourselves.

**Why.** It is the part of the task that genuinely needs *fresh, runtime* information
(funding, hiring, launches) that the target's own static site won't reveal. Server-side
tool use keeps the agent loop inside one API call and returns real source URLs we can cite.

**Trade-off / safeguards.** Models can fabricate URLs. We defend with (a) a strict
instruction to only cite returned pages, (b) post-hoc validation that every signal URL
starts with `http` and has a finding, and (c) capturing the search-result URLs the tool
actually surfaced. Bad rows are dropped, not trusted.

### 4.5 Structured JSON as the contract between model and UI

**Decision.** Every LLM step returns strict JSON with an explicit schema embedded in the
prompt; the backend extracts and defensively parses it.

**Why.** The UI renders structured artifacts (ICP fields, dimension scores, claim map). A
JSON contract decouples model output from rendering and makes the evidence/claim plumbing
mechanical.

**Trade-off.** LLMs occasionally wrap JSON in prose or code fences. Handled by a
brace-matching `_extract_json` that scans for the first balanced object/array and tolerates
`<cite>` tags from web-search responses, rather than a brittle `json.loads`.

### 4.6 Self-crawling the company's own site

**Decision.** Fetch the company's own pages directly with `httpx` + `trafilatura` instead
of relying solely on search.

**Why.** The company's own site is the highest-signal, most-citable source for value prop
and ICP, and fetching it directly is cheap and deterministic. Link discovery + page-kind
classification ensures we read the *right* pages (pricing, customers) rather than just the
homepage.

**Trade-off.** JS-heavy sites that render content client-side may yield thin text
(no headless browser). Accepted for scope; `favor_recall=True` and the http/www fallback
variants reduce misses.

### 4.7 SQLite persistence

**Decision.** Persist senders and evaluations in SQLite (file-based, zero-config).

**Why.** Enables ICP reuse across targets, a "saved senders" UX, and revisiting past
evaluations — without standing up a database server. `DB_PATH` is env-overridable for a
mounted volume in deployment.

**Trade-off.** Single-writer, not horizontally scalable. Correct choice for a local-first
tool; the `db.py` interface is small enough to reimplement on Postgres if needed.

### 4.8 Monolithic deploy (backend serves the built SPA)

**Decision.** In production the FastAPI app serves the built React bundle from one origin;
the frontend uses relative API paths.

**Why.** One origin removes CORS complexity, one process to deploy (Railway/nixpacks), and
the API base "just works" in prod while still allowing `localhost:8000` in dev and a
`VITE_API_BASE` override.

**Trade-off.** Couples frontend and backend lifecycles. Fine for this scale; the split is
still possible via `VITE_API_BASE`.

---

## 5. Token & quality optimizations (summary)

- **Snippet caps + per-kind quotas** bound every reasoning call's input.
- **Chunk size (~480 chars)** keeps snippets granular enough to cite precisely without
  bloating the prompt.
- **Cheap model for search/extraction**, strong model only for synthesis/writing.
- **`max_tokens` budgets** per call (1500–1800) cap output cost.
- **Boilerplate stripping** (`trafilatura`) removes nav/footer noise before chunking.
- **De-duplication** in `EvidenceStore` (content-hash ids) avoids feeding the same text
  twice.
- **Reusable sender ICP** amortizes Mode 1 cost across many Mode 2 runs.
- **Compact, numbered snippet format** (`[id] (kind · url)\ntext`) minimizes scaffolding
  tokens while preserving citability.

---

## 6. Quality & safety of outputs

- **No placeholders.** The email prompt hard-bans `[First Name]`-style tokens; the model
  greets with "Hi there," and uses the real target name.
- **Mandatory claim mapping.** Every factual statement about the target must map to
  evidence; unsupported sentences are instructed to be removed, and the UI surfaces any
  unresolved claim with a ⚠ marker.
- **Honest scoring.** The fit prompt explicitly instructs low scores for poor fits and a
  `confidence` field for thin evidence — the tool is built to say "weak fit," not to
  flatter.
- **Two genuinely different angles.** Pain-led vs trigger-led are defined structurally
  (problem ownership vs recent event), not just stylistically.

---

## 7. What I would do next (known limitations)

| Limitation | Planned improvement |
|---|---|
| Keyword ranking misses semantic matches | Add embedding-based retrieval; keep keyword as a cheap pre-filter |
| No headless rendering for JS-heavy sites | Optional Playwright fallback when extracted text is thin |
| JSON parsing is best-effort | Move to Anthropic **tool/structured output** for guaranteed schemas |
| No automated eval of email quality | Add an LLM-as-judge rubric + regression set of sender/target pairs |
| Signals not cross-verified | Add a verification pass that confirms each signal against a second source |
| Synchronous, blocking requests | Stream pipeline progress over SSE/WebSocket for true step-by-step UX |
| No auth / multi-tenant | Add user accounts + per-user data scoping |

These are deliberate scope decisions for a focused, reviewable v1 — each has a clear,
low-risk upgrade path because of the layered architecture.
