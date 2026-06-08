# Architecture

An agentic RAG system that turns public company websites into outbound strategy. Two modes:

1. **Sender analysis** — crawl a sender company's site, infer its value proposition and a structured ICP, store it.
2. **Target evaluation** — crawl a target company's site, extract signals, score ICP fit, and draft two evidence-verified outbound emails with a claim map.

## System overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser — React SPA (Vite + Tailwind)                          │
│  Mode 1 / Mode 2 forms · results · evidence drawer              │
└───────────────────────────────┬─────────────────────────────────┘
                                 │  HTTP / JSON  (/api/*)
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI (server.py) — JSON API + serves the built SPA          │
│                                                                 │
│         ┌────────────────────────────────────────────┐         │
│         │ agent.py — hand-rolled orchestration        │         │
│         │   analyze_sender() / evaluate_target()      │         │
│         └───────┬───────────────┬──────────┬─────────┘         │
│                 │               │          │                   │
│        ┌────────▼─────┐ ┌───────▼──────┐ ┌─▼──────────┐        │
│        │ retrieval.py │ │ Anthropic    │ │ db.py      │        │
│        │ fetch+chunk+ │ │ Claude API + │ │ SQLite     │        │
│        │ embed+rank   │ │ web_search   │ │ senders,   │        │
│        │ (in-memory)  │ │ sonnet/haiku │ │ evaluations│        │
│        └──────┬───────┘ └──────────────┘ └────────────┘        │
│               │                                                │
│               ▼ public web pages (httpx)                       │
└─────────────────────────────────────────────────────────────────┘
```

The frontend is built to static assets and served by the backend, so the whole product runs as a single process.

## The two pipelines

The "agent graph" is **hand-rolled orchestration in `backend/agent.py`** — plain Python calling the Anthropic Messages API directly. There is no graph framework (no LangGraph/LangChain).

### Mode 1 — Sender (`analyze_sender()`)

```
crawl → embed + dedupe → faceted retrieve → ICP + value prop (one structured call) → persist
```

### Mode 2 — Target (`evaluate_target()`)

```
crawl → embed + dedupe → web_search signals → faceted retrieve → fit score
      → strategy (allowed-claims gate) → draft 2 emails → verify + constraint check
      → repair → claim map → persist
                    ▲          │
                    └──────────┘
```

The `verify → repair` step is the claim-verification loop: a fact-checking pass over every cited claim plus a length/format constraint check, with **one** repair attempt that rewrites unsupported claims or removes them (≤1 round).

## Core architectural rule: no full-context stuffing

LLM steps never see raw web pages. The only path from a website to a model prompt is:

```
pages → cleaned text (trafilatura) → ~700-char chunks (~150–180 tokens) → embeddings
      → faceted retrieval (top-k per facet, deduped by id)
      → a capped set of short snippets
      → compact structured signals
      → downstream steps consume signals + citations only
```

The email drafter receives distilled input only: value prop, ICP, fit summary, top signals, persona, and the approved claim list — never chunks, never pages.

## Retrieval (the evidence layer, `retrieval.py`)

- Crawler discovers and prioritizes high-signal pages (home, product, pricing, customers, about, blog, careers) via `PRIORITY_PATH_HINTS` — a heuristic, **not** an LLM planner.
- Chunks are embedded **once per run**, in memory. Embeddings are **optional**: with `fastembed` (`BAAI/bge-small-en-v1.5`, local, no extra API key) installed it does semantic cosine search with near-duplicate pruning (≥0.93 similarity dropped); without it, retrieval falls back to keyword term-frequency ranking. Lean deploys (Railway) run keyword mode and stay pure-Python.
- `_retrieve_facets()` runs several targeted queries (industries, personas, pains, triggers, persona-fit) and unions the top hits, deduped by id and capped for token budget.

## Citation integrity

Citations cannot be hallucinated by construction:

- Snippets carry a **content-hash id** (`s_…`); models reference ids, and the code re-attaches the real `{url, snippet}` to results.
- Web-search signals carry their citation forward verbatim (only URLs the search actually returned become citable).
- The strategist's `claims` whitelist must each reference an evidence id / signal url; the drafter may only assert approved claims; the verifier checks each claim ⇄ snippet entailment.
- The final claim map is assembled in code (`_build_claim_map`) by walking claim → evidence id → snippet.

## Agent responsibilities

| Step (`agent.py`) | Model | Input | Output |
|---|---|---|---|
| ICP + value prop | sonnet (strong) | numbered snippets | value proposition + structured ICP + per-field evidence ids |
| Signal extractor | haiku (cheap) + `web_search` | target name/domain | recent citable signals with source URLs |
| ICP / fit scoring | sonnet (strong) | sender ICP + snippets + signals | 5-dimension fit score + cited rationale |
| Messaging strategist | haiku (cheap) | value prop, ICP, fit, top signals, persona | angles + allowed-claims whitelist |
| Email drafter | sonnet (strong) | strategy + distilled context | 2 emails with claim references |
| Claim verifier | haiku (cheap) | cited claims + their snippets | supported / partial / unsupported per claim |
| Repair / redraft | sonnet (strong) | failing claims + verdicts + violations | revised emails |

Cheap model (`claude-haiku-4-5`) handles signal mining, strategy, and verification; the strong model (`claude-sonnet-4-6`) handles ICP synthesis, fit scoring, drafting, and repair. Both are env-overridable (`CHEAP_MODEL` / `STRONG_MODEL`).

## Structured outputs & constraints

- Every reasoning step uses **forced tool-use** (`_complete_json` with a JSON Schema + `tool_choice`), so malformed output can't silently yield a blank result; if the model returns text instead of a tool call, a best-effort JSON extractor is the fallback.
- Email constraints are enforced in code: body **80–130 words**, subject **≤7 words**, and no bracket placeholder tokens. A violation triggers the single repair pass.

## Token metering

Every model call records `input_tokens` / `output_tokens` per step into a per-run `TokenMeter`, returned as `result.usage` and rendered in the UI footer — so the cheap/strong routing and the no-stuffing design are measurable.

## Data model (SQLite, `db.py`)

- `senders` — one row per analyzed sender site: id (`snd_…`), url, domain, company name, one-liner, full JSON result (`data`), created_at.
- `evaluations` — one row per target evaluation: id (`evl_…`), `sender_id` FK (an ICP is reused across many targets), target url/name, persona, denormalized `fit_score`, full JSON result, created_at.

The full agent result is stored as JSON in `data`; a few columns are promoted for cheap listing. `DB_PATH` is env-configurable (default `data.db`; point at a mounted volume for persistence). Snippets and their embedding vectors live **in memory for the duration of a run only** — they are not persisted; vectors are never sent to the client.

## Request lifecycle

Requests are **synchronous JSON** (no SSE/streaming): the client POSTs, the pipeline runs to completion, and the full result (`fit`, `strategy`, `emails`, `signals`, `evidence`, `claim_map`, `verification`, `usage`) is returned and persisted. Input is validated with Pydantic.

## Error handling

- Crawl: per-page 15s timeout, pages fetched in parallel up to `max_pages` (~7), follows redirects, tolerates per-page failures. (No robots.txt handling.)
- LLM calls: forced tool-use with a JSON schema; falls back to best-effort text JSON extraction if the model returns no tool call.
- API: pipeline failures surface as HTTP `422` (analysis/evaluation failed), unexpected errors as `500`, missing records as `404`.
</content>

## Technology stack

| Layer | Choice |
|-------|--------|
| API / server | FastAPI, Uvicorn |
| LLM | Anthropic Claude (Messages API + hosted `web_search` tool) |
| Crawl / extract | httpx (async), trafilatura, tldextract |
| Embeddings (optional) | fastembed (`bge-small`, ONNX) + numpy |
| Validation / config | Pydantic, python-dotenv |
| Persistence | SQLite |
| Frontend | React, Vite, Tailwind CSS |

## What I would do next (known limitations)

| Limitation | Planned improvement |
|---|---|
| No headless rendering for JS-heavy sites | Optional Playwright fallback when extracted text is thin |
| Embeddings are CPU-bound and add a heavy dep | Optional hosted embeddings, or cache vectors per domain |
| Single corrective redraft round | Make rounds adaptive/configurable; escalate persistent failures |
| Signals not cross-verified | Confirm each signal against a second independent source |
| No automated eval of email quality | LLM-as-judge rubric + a regression set of sender/target pairs |
| Synchronous, blocking requests | Stream real per-step progress over SSE/WebSocket (UI progress is currently time-driven) |
| No auth / multi-tenant | User accounts + per-user data scoping; SSRF allowlisting for fetched URLs |
