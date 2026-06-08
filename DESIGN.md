# OutboundIQ — Design Document

This document explains **what** OutboundIQ is, **why** it is built the way it is, and the
**design trade-offs** behind every major decision. Read it alongside
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
| Answers/emails must be based on **retrieved snippets, not full-context stuffing** | A RAG layer chunks fetched pages and retrieves only the snippets relevant to each reasoning facet via a **pluggable retriever** — local embeddings when available, keyword ranking otherwise. Every claim must cite a snippet id, and each claim is **verified for entailment** against that snippet before it ships. |
| Implement an **agentic** solution that *plans → retrieves evidence → finds signals → drafts* | A multi-step agent graph where each node is a discrete, single-responsibility LLM call: research → signal mining (tool use) → ICP fit → messaging strategy → drafting → claim verification → corrective redraft. |
| **Optimize for token usage and quality** | Two-tier model routing (cheap for mining/strategy/verification, strong for synthesis/drafting), semantic retrieval with hard snippet caps, dedup, small structured prompts, `max_tokens` budgets, and **per-step token metering surfaced in the UI** so the optimization is measurable. |

---

## 2. High-level architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Frontend (Vite + React + Tailwind SPA)                            │
│  Mode 1 · Mode 2 · Strategy panel · Claim map · Evidence drawer    │
│  · Token-usage footer                                              │
└───────────────────────────────────────────────────────────────────┘
                    │  JSON over HTTP (/api/*)
                    ▼
┌───────────────────────────────────────────────────────────────────┐
│  Backend (FastAPI)  — server.py                                    │
│   ┌─────────────────────────────────────────────────────────────┐ │
│   │  agent.py — agentic orchestration + TokenMeter              │ │
│   │   Mode 1: analyze_sender()                                  │ │
│   │   Mode 2: signals → fit → strategy → draft → verify/refine │ │
│   └─────────────────────────────────────────────────────────────┘ │
│   ┌──────────────────────────┐   ┌──────────────────────────────┐ │
│   │ retrieval.py             │   │ db.py (SQLite)               │ │
│   │  crawl · chunk · embed   │   │  senders + evaluations       │ │
│   │  dedupe · semantic search│   │                              │ │
│   └──────────────────────────┘   └──────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
        │ httpx (own pages)   │ Anthropic API (LLM + web_search)   │ fastembed (optional, local, no key)
        ▼                     ▼                                     ▼
   Company websites      Claude models / live web            bge-small embeddings
```

> **Retrieval is pluggable.** Embeddings are an *optional* enhancement: when `numpy` +
> `fastembed` are installed (local dev) the retriever uses semantic search; on lean deploys
> (e.g. Railway) those native deps are omitted and the **same retrieval interface** falls back
> to keyword ranking. The application code is identical either way — see §4.1.

**Separation of concerns** is the organizing principle:

- `retrieval.py` knows nothing about LLMs — it fetches, extracts, chunks, optionally **embeds**,
  dedupes, stores, and **retrieves** evidence (semantic or keyword) behind one interface.
- `agent.py` knows nothing about HTTP or SQL — it orchestrates the agent graph over evidence.
- `server.py` is a thin transport + persistence boundary; `db.py` is pure persistence.

Each layer is independently testable and swappable (e.g. swap the embedding model, or
SQLite for Postgres, without touching the others).

---

## 3. The agentic pipeline

"Agentic" here means the system **plans a sequence of steps, decides what evidence to
retrieve, uses tools to gather live signals, gates drafting behind a strategy, and verifies
its own output** — rather than one prompt that does everything.

### Mode 1 — Sender analysis (`analyze_sender`)

```
crawl homepage → discover & fetch priority pages (parallel) → chunk
   → (if embeddings available) embed all snippets once + prune near-duplicates
   → facet retrieval (7 ICP-dimension queries) → top ~24 snippets
   → synthesize value prop + structured ICP (1 strong-model call, JSON-schema forced)
   → resolve cited ids → evidence map → persist (+ token usage)
```

### Mode 2 — Target evaluation + drafting (`evaluate_target`)

```
crawl target site → (optional) embed + dedupe
   → [cheap + web_search]  mine live signals
   → facet retrieval driven by the sender's ICP + the persona
   → [strong]  score ICP fit across 5 dimensions
   → [cheap]   messaging strategy: angles + ALLOWED-claims whitelist (+ off-limits list)
   → [strong]  draft 2 emails (pain-led, trigger-led), constrained to allowed claims
   → [cheap]   verify every claim's entailment against its cited snippet
   → constraint check (length / subject / placeholders)
   → [strong]  one corrective redraft if anything is unsupported or out of spec
   → build claim map (with per-claim status) → persist (+ token usage)
```

The key agentic properties: **tool use** (live `web_search`), a **plan-before-draft gate**
(the strategist whitelists claims), and a **self-correction loop** (verifier → redraft).

---

## 4. Key design decisions & trade-offs

### 4.1 Pluggable RAG (semantic when available, keyword otherwise), not context stuffing

**Decision.** Chunk every fetched page and retrieve only the snippets relevant to each
reasoning facet (industries, personas, pains, triggers, persona-fit…) behind a **single
retrieval interface** with two interchangeable backends:
- **Semantic** (preferred, local dev): embed all chunks with `BAAI/bge-small-en-v1.5` via
  `fastembed`, prune near-duplicates by cosine similarity, and retrieve by cosine to each
  facet query.
- **Keyword** (fallback / production default): term-frequency ranking over the same chunks.

Either way, the model gets a small numbered subset and must cite snippet ids.

**Why.**
- **Token cost.** A company site is tens of thousands of tokens; facet retrieval caps each
  reasoning call to ~18–24 short snippets regardless of backend.
- **Relevance & quality.** Semantic retrieval surfaces the right evidence even when wording
  differs from the query; per-facet queries guarantee *coverage* of every ICP dimension rather
  than just the top keyword hits.
- **Grounding.** A finite, cited snippet set makes hallucination structurally harder and
  yields the claim map for free.

**Why make embeddings optional (graceful degradation).** Local embeddings need native wheels
(`numpy`, and `onnxruntime` via `fastembed`) plus a one-time ~130 MB model download and CPU/
memory at runtime. On a constrained PaaS (Railway/Nixpacks) those native libs are painful
(missing `libstdc++.so.6`) and the model download/memory can OOM a small instance. So the
deploy ships **pure-Python with keyword retrieval**, and embeddings are a one-line opt-in
(`pip install numpy fastembed`) for local use. The retriever auto-detects: if `numpy`/the model
can't load, `semantic_search` transparently calls the keyword ranker. **The same code path
runs both ways** — semantic locally, keyword in production — with no branching at the call
sites.

**Trade-off.** Production retrieval is lexical (less precise on paraphrase) than local. Accepted
deliberately: a deploy that *always boots* beats a semantically-richer one that crashes on
native libs, and the facet-query design keeps keyword coverage reasonable. Restoring semantic
parity in production is just installing the extras on a base image that has the C++ runtime.

### 4.2 Near-duplicate pruning before retrieval (when embeddings are on)

**Decision.** When embeddings are available, greedily drop any chunk whose cosine similarity to
an already-kept chunk exceeds 0.93. (In keyword-only mode this step is skipped; exact-duplicate
chunks are still removed by content-hash ids at ingestion.)

**Why.** Shared nav/footer/CTA boilerplate survives per-page content hashing and would
otherwise waste tokens and **over-count the same "evidence."** Pruning keeps the snippet set
diverse and the claim map honest.

**Trade-off.** A too-aggressive threshold could drop genuinely distinct text; 0.93 is
conservative.

### 4.3 Structured outputs via forced tool-use

**Decision.** Every structured step calls the model with a single tool (`emit_result`) whose
`input_schema` is the desired JSON shape, and `tool_choice` forces that tool. A best-effort
text JSON parser remains only as a fallback.

**Why.** The UI renders structured artifacts (ICP fields, dimension scores, strategy, claim
map). Schema-forced tool use means malformed output **can't silently produce a blank result**
— the model is constrained to the contract. This is far more robust than parsing free-text
JSON.

**Trade-off.** Slightly less flexible than free-text; mitigated by keeping the brace-matching
`_extract_json` as a fallback (still used for the web-search signal step, whose final message
is a plain JSON array).

### 4.4 A messaging strategist that gates the drafter (allowed-claims whitelist)

**Decision.** Insert a dedicated *strategy* step between fit scoring and drafting. It outputs
the two angles **plus an explicit `claims_allowed` whitelist** (each claim tied to an
evidence id/url) and a `claims_not_allowed` list. The drafter may assert **only** whitelisted
claims.

**Why.** This is a **plan-before-act** pattern that attacks hallucination at the source:
rather than hoping the drafter stays grounded, we pre-compute the exact set of supportable
facts and forbid tempting-but-unsupported ones. It also separates *strategy* (cheap model)
from *prose* (strong model), saving tokens.

**Trade-off.** An extra LLM call. Worth it: it measurably reduces unsupported claims and
makes the redraft loop converge faster.

### 4.5 Claim verification (entailment) + constraint check + corrective redraft

**Decision.** After drafting, a **verifier** (cheap model) judges each `(claim, cited
snippet)` pair as supported / partial / unsupported using **only** the snippet text. A
deterministic checker validates spec constraints (80–130-word body, ≤7-word subject, no
`[placeholder]` tokens). If any claim is unsupported or any constraint is violated, the strong
model does **one corrective redraft** against targeted feedback.

**Why.** The brief demands grounded emails; *asserting* grounding isn't enough. This is a
**self-correction loop** that catches the model's own mistakes before they reach the user.
Each claim ships with a status the UI surfaces (✓ verified / ~ partial / ⚠ unsupported).

**Trade-off.** Up to two extra calls (verify + redraft) and latency. Capped at **one** redraft
round to bound cost; remaining issues are reported transparently rather than looped forever.

### 4.6 Two-tier model routing (cheap vs strong)

**Decision.** `CHEAP_MODEL` (Haiku) for signal mining, messaging strategy, and claim
verification; `STRONG_MODEL` (Sonnet) for ICP synthesis, fit scoring, and email drafting
(incl. redraft).

**Why.** Mining/strategy/verification are extraction- and judgment-style tasks the cheap model
handles well; synthesis and persuasive writing reward the strong model. This is the core
token/quality lever — spend strong-model tokens only where they move the needle. Both ids are
env-driven.

### 4.7 Token metering as a first-class output

**Decision.** A `TokenMeter` accumulates input/output tokens per step across the whole run;
the result includes a `usage` summary and the UI shows a token footer.

**Why.** The brief says "optimize for token usage." Making usage **measurable and visible**
turns that from a claim into an observable property, and makes regressions obvious.

**Trade-off.** None material — it's read from the API's `usage` field already returned by each
call.

### 4.8 Self-crawling the company's own site

**Decision.** Fetch the company's own pages directly with `httpx` + `trafilatura`, discovering
and classifying priority pages (about/product/pricing/customers/blog/careers/contact).

**Why.** The company's own site is the highest-signal, most-citable source for value prop and
ICP, and fetching it directly is cheap and deterministic.

**Trade-off.** JS-heavy sites that render client-side may yield thin text (no headless
browser). `favor_recall=True` and http/www fallbacks reduce misses.

### 4.9 Live tool use for external signals (`web_search`)

**Decision.** Use Anthropic's server-side `web_search` tool for the "find signals" step.

**Why.** Funding/hiring/launch signals need *fresh, runtime* data the static site won't
reveal. Server-side tool use keeps the agent loop in one call and returns real source URLs.

**Safeguards.** Strict "only cite returned pages" instruction, capture of the URLs the tool
actually surfaced, and post-hoc validation (URL must start with `http`, finding must be
present); bad rows are dropped.

### 4.10 SQLite persistence + monolithic deploy

**Decision.** Persist senders/evaluations in SQLite (full-result JSON + a few promoted
columns); in production the FastAPI app serves the built SPA from one origin.

**Why.** SQLite enables ICP reuse across targets with zero config; one-origin deploy removes
CORS friction and is one process to run (Railway/nixpacks). `DB_PATH` is env-overridable for a
mounted volume; `VITE_API_BASE` allows a split deploy if needed.

---

## 5. Token & quality optimizations (summary)

- **Facet retrieval** (semantic or keyword) with hard caps (~24 sender / ~18 target snippets)
  bounds every reasoning call's input.
- **Near-duplicate pruning** (semantic mode) removes repeated boilerplate so tokens (and
  evidence counts) aren't wasted.
- **Cheap/strong model split** spends strong-model tokens only on synthesis and writing.
- **Strategist pre-computes allowed claims**, shrinking the drafter's job and the redraft loop.
- **`max_tokens` budgets** (1200–1800) cap output per call.
- **Boilerplate stripping** (`trafilatura`) and **content-hash dedup** before embedding.
- **Reusable sender ICP** amortizes Mode 1 across many Mode 2 runs.
- **Compact numbered snippet format** minimizes scaffolding tokens while preserving
  citability.
- **Per-step token metering** makes all of the above measurable.

---

## 6. Quality & safety of outputs

- **No placeholders.** The email prompt and a deterministic regex both ban `[First Name]`-style
  tokens; violations trigger a redraft.
- **Allowed-claims gate + entailment verification.** Every factual claim about the target must
  be whitelisted *and* verified against its cited snippet; unsupported claims trigger a redraft
  and are flagged in the claim map.
- **Honest scoring.** The fit prompt instructs low scores for poor fits, scores five
  dimensions (incl. buyer/persona fit), and the sender step emits a `confidence` field for thin
  evidence.
- **Two genuinely different angles.** Pain-led (a problem the persona owns) vs trigger-led (a
  recent event) are defined structurally and seeded by the strategist, not just stylistically.

---

## 7. What I would do next (known limitations)

| Limitation | Planned improvement |
|---|---|
| No headless rendering for JS-heavy sites | Optional Playwright fallback when extracted text is thin |
| Production retrieval is keyword-only (embeddings omitted for a lean deploy) | Run on a base image with the C++ runtime and install the `numpy`+`fastembed` extras, or use hosted embeddings + a cached vector store per domain |
| Single corrective redraft round | Make rounds adaptive/configurable; escalate persistent failures |
| Signals not cross-verified | Confirm each signal against a second independent source |
| No automated eval of email quality | LLM-as-judge rubric + a regression set of sender/target pairs |
| Synchronous, blocking requests | Stream real per-step progress over SSE/WebSocket (UI progress is currently time-driven) |
| No auth / multi-tenant | User accounts + per-user data scoping; SSRF allowlisting for fetched URLs |

These are deliberate scope decisions for a focused, reviewable build — each has a clear,
low-risk upgrade path thanks to the layered architecture.
