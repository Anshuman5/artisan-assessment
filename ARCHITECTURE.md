# OutboundIQ — Architecture

A structural reference for the system: components, how they connect, the request
lifecycle, the data model, and deployment topology.

> For *why* decisions were made see [DESIGN.md](DESIGN.md); for a code-level
> walkthrough see [IMPLEMENTATION.md](IMPLEMENTATION.md); for a Q&A view see
> [INTERVIEW.md](INTERVIEW.md).

---

## 1. System context

OutboundIQ is a single-deployable web app that turns public company information
into outbound strategy. It has two user-facing modes:

1. **Sender analysis** — infer a value proposition + structured ICP from a sender
   company's own website.
2. **Target evaluation** — research a target account, score it against a saved
   ICP, draft two outbound emails, and produce a verified claim map.

It talks to two external services at runtime — the **Anthropic API** (Claude
models, incl. the hosted `web_search` tool) — and fetches **public web pages**
directly over HTTP. Embeddings run **locally** (no embeddings vendor).

```
                ┌──────────────────────────────────────────────┐
                │                  Browser                       │
                │        React SPA (Vite + Tailwind)             │
                └───────────────┬──────────────────────────────┘
                                │  HTTP/JSON  (/api/*)
                                ▼
                ┌──────────────────────────────────────────────┐
                │              FastAPI (server.py)               │
                │   API endpoints  +  serves the built SPA       │
                └───┬───────────────┬───────────────┬──────────┘
                    │               │               │
                    ▼               ▼               ▼
              agent.py         retrieval.py        db.py
           (orchestration)   (fetch/chunk/embed/  (SQLite
                    │          rank evidence)      persistence)
                    │               │
        ┌───────────┴───────┐       └────────► target/sender public web pages (HTTP)
        ▼                   ▼
  Anthropic Messages   Anthropic web_search
  API (Claude)         tool (hosted)
```

---

## 2. Components & responsibilities

### Backend (`backend/`, Python)

| Module | Responsibility |
|--------|----------------|
| `server.py` | FastAPI app. Defines the `/api/*` endpoints, CORS, and (when a build exists) mounts the React SPA at `/`. Thin layer — validates input with Pydantic, delegates to `agent`, persists via `db`, maps errors to HTTP codes. |
| `agent.py`  | The agentic orchestration / "agent graph." Owns the two pipelines (`analyze_sender`, `evaluate_target`), all LLM calls, model routing, structured-output enforcement, claim verification, constraint checks, and token metering. |
| `retrieval.py` | The evidence layer. Crawls a company's own site, extracts + chunks page text, (optionally) embeds chunks locally, prunes near-duplicates, and serves ranked snippets via semantic or keyword retrieval. Defines `Snippet` and `EvidenceStore`. |
| `db.py` | SQLite persistence for sender profiles and target evaluations. Stores the full JSON result plus a few indexed columns. |

### Frontend (`frontend/`, React + Vite + Tailwind)

| File | Responsibility |
|------|----------------|
| `src/App.jsx` | The whole SPA: the two-mode UI, result rendering (ICP, fit, signals, strategy panel, emails, claim map, token-usage footer), and the evidence drawer. |
| `src/components.jsx` | Presentational primitives (logo, score ring, bars, evidence pills, copy button, spinner). |
| `src/api.js` | `fetch` wrappers for the `/api/*` endpoints. |

The frontend is **built to static assets** (`frontend/dist`) and served by the
backend in production, so the whole product runs as one process.

### External services

- **Anthropic Messages API** — two model tiers (cheap/strong, see §6) and the
  hosted `web_search` tool for live external signals.
- **Public web** — the target/sender sites themselves, fetched directly with
  `httpx` and parsed with `trafilatura`.
- **Local embedding model** — `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX). No
  network at inference time after the one-time model download. *Optional* — see §5.

---

## 3. Request lifecycle

### Mode 1 — `POST /api/sender/analyze`

```
client → server.sender_analyze(url)
        → agent.analyze_sender(url)
            1. retrieval.crawl_company_site(url)      # fetch home + priority pages,
                                                      #   chunk, embed, dedupe
            2. _retrieve_facets(...)                  # semantic retrieval per ICP facet
            3. _complete_json(STRONG, SENDER_SCHEMA)  # value prop + ICP (tool-use JSON)
        → db.save_sender(...)                         # persist, return sender id
        → JSON { profile, evidence, meta, usage }
```

### Mode 2 — `POST /api/target/evaluate`

```
client → server.target_evaluate(sender_id, target_url, persona)
        → db.get_sender(sender_id)                    # load the saved ICP
        → agent.evaluate_target(profile, target_url, persona)
            1. crawl_company_site(target_url)         # research the target's own site
            2. research_target_signals(...)           # web_search → recent external signals
            3. _retrieve_facets(ICP-driven queries)   # pull snippets relevant to THIS fit
            4. _complete_json(STRONG, FIT_SCHEMA)      # 5-dimension ICP fit score
            5. _complete_json(CHEAP, STRATEGY_SCHEMA)  # angles + allowed-claims whitelist
            6. _complete_json(STRONG, EMAILS_SCHEMA)   # draft 2 emails (claim-gated)
            7. _verify_and_refine(...)                 # entailment verify + constraint
                                                       #   check + ≤1 corrective redraft
            8. _build_claim_map(...)                   # flatten claims → resolved sources
        → db.save_evaluation(...)
        → JSON { fit, strategy, emails, signals, evidence, claim_map, verification, usage }
```

The agent graph (Mode 2) as a pipeline:

```
crawl ─▶ embed + dedupe (optional → keyword mode in prod)
      ─▶ signal extraction (web_search) ─▶ facet retrieval ─▶ ICP fit scoring
      ─▶ messaging strategy (allowed-claims gate) ─▶ email drafting
      ─▶ claim verification (entailment) + constraint check
      ─▶ if any unsupported claim / out-of-spec: corrective redraft ─▶ re-verify (≤1 round)
      ─▶ claim map
```

Every box that calls a model records its token usage into a per-run `TokenMeter`,
surfaced as `result.usage` (and rendered in the UI footer).

---

## 4. Retrieval architecture (the evidence layer)

The core principle: **the model never sees raw pages.** Pages become a store of
short, citable snippets; only the most relevant snippets per reasoning facet are
fed to the model.

```
fetch HTML (httpx) ─▶ extract main text (trafilatura) ─▶ chunk (~700 chars / ~150 tokens)
   ─▶ EvidenceStore.add()  (exact-dup collapsed by content hash)
   ─▶ EvidenceStore.finalize():
         • embed_texts(all chunks)            # batched, local
         • cosine near-duplicate pruning      # drop boilerplate ≥0.93 similar
   ─▶ EvidenceStore.semantic_search(query)    # cosine top-k, per facet
```

- **`Snippet`** — `{id, url, title, text, source_type, page_kind, vec}`. `id` is a
  content hash (`s_…`) used as the citation token throughout the pipeline. `vec`
  is never serialized to the client (`to_public()` omits it).
- **Page prioritization** — the crawler discovers and prioritizes high-signal
  pages (home, product, pricing, customers, about, blog, careers) via
  `PRIORITY_PATH_HINTS`, fetched in parallel up to `max_pages`.
- **Facet retrieval** — `_retrieve_facets()` runs several targeted queries
  (industries, personas, pains, triggers, persona-fit) and unions the top hits,
  deduped by id, capped for token budget.

### Embeddings are optional (graceful degradation)

`fastembed`/`numpy` are **not required** for the app to run. The embedder is
loaded lazily and any failure (not installed, weights unavailable) flips the
store to a **keyword-ranking fallback**:

| Embeddings available | Behavior |
|----------------------|----------|
| Yes (local dev, full install) | Semantic cosine retrieval + near-duplicate pruning. |
| No (lean deploy) | `semantic_search` falls back to keyword term-frequency ranking; near-dup pruning is skipped. |

`meta.embedded` in the crawl result records which path was taken. This keeps the
production image small on platforms like Railway while giving full semantic RAG
locally.

---

## 5. Model usage & structured outputs

- **Two-tier routing** (`agent.py`): a **cheap** model (`CHEAP_MODEL`, default
  `claude-haiku-4-5`) handles signal mining, messaging strategy, and claim
  verification; a **strong** model (`STRONG_MODEL`, default `claude-sonnet-4-6`)
  handles ICP synthesis, fit scoring, and email drafting. Both are env-overridable.
- **Structured outputs** — every reasoning step uses **forced tool-use**
  (`_complete_json` with a JSON Schema and `tool_choice`), so malformed output
  can't silently yield a blank result. The web-search step is the exception (it
  needs free-form tool use), and parses a JSON array from text with a fallback
  extractor.
- **Live tool use** — `research_target_signals` calls the hosted
  `web_search_20250305` tool (capped via `max_uses`). Only URLs the search
  actually returned become citable signals.
- **Token metering** — `TokenMeter` accumulates `input_tokens`/`output_tokens`
  per step across the run and is returned as `usage`.

---

## 6. Data model

### SQLite (`db.py`)

```
senders(
  id TEXT PK,            -- "snd_…"
  url, domain, company_name, one_liner,
  data TEXT,             -- full JSON: profile + evidence + meta + usage
  created_at REAL
)

evaluations(
  id TEXT PK,            -- "evl_…"
  sender_id TEXT,        -- FK → senders.id (an ICP is reused across many targets)
  target_url, target_name, persona_role, persona_seniority,
  fit_score INTEGER,     -- denormalized for listing
  data TEXT,             -- full JSON result
  created_at REAL
)
```

The full agent result is stored as JSON in `data`; a few columns are promoted for
cheap listing/filtering. `DB_PATH` is env-configurable (e.g. a mounted volume).

### Key in-memory / wire shapes

- **`evidence`** — `{ snippet_id | url → {id, url, title, snippet, source_type, page_kind} }`.
  Page snippets keyed by content-hash id; external signals keyed by their URL.
- **`claim_map`** — flattened list of every factual claim in the emails:
  `{angle, claim, evidence_id, url, snippet, title, status, resolved}` where
  `status ∈ {supported, partial, unsupported}` (from the verifier) and `resolved`
  is true only when the claim is backed by a verified source.

---

## 7. API surface (`server.py`)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/sender/analyze` | Analyze a sender site → value prop + ICP |
| GET  | `/api/senders` | List saved sender profiles |
| GET  | `/api/sender/{sid}` | Get a saved sender profile |
| POST | `/api/target/evaluate` | Evaluate a target + persona → fit + emails + claim map |
| GET  | `/api/evaluations` | List saved evaluations (optionally by `sender_id`) |
| GET  | `/api/evaluation/{eid}` | Get a saved evaluation |
| GET  | `/api/health` | Health check |
| GET  | `/`, `/{path}` | Serve the built SPA (when `frontend/dist` exists) |

CORS is open (`*`) for ease of local dev. Errors from the pipeline surface as
`422` (analysis/evaluation failed) or `500` (unexpected), with a stack trace logged.

---

## 8. Deployment topology

**Monolith.** One process serves both the JSON API and the static SPA:

```
frontend (npm run build) ──▶ frontend/dist ──┐
                                             ├─▶ uvicorn server:app  (single service)
backend (FastAPI + agent) ───────────────────┘
        │
        ├─ ANTHROPIC_API_KEY        (loaded from backend/local.env via python-dotenv,
        │                            or real env vars — real env wins)
        ├─ CHEAP_MODEL / STRONG_MODEL   (optional model overrides)
        ├─ ANTHROPIC_BASE_URL       (optional proxy)
        └─ DB_PATH                   (optional; point at a persistent volume)
```

- **Config & secrets** — `backend/local.env` (gitignored) holds the API key for
  local runs; `local.env.example` is the committed template. On Railway, set the
  same variables as real environment variables.
- **State** — a single `data.db` SQLite file (gitignored). For persistence across
  deploys, point `DB_PATH` at a mounted volume.
- **Build vs lean image** — full local installs include `fastembed`/`numpy` for
  semantic retrieval; lean deploys omit them and fall back to keyword ranking
  (§4). The embedding model downloads once on first use and is cached.

---

## 9. Cross-cutting concerns

- **Grounding / anti-hallucination** — enforced at three layers: retrieval (only
  real snippets), the messaging strategist's allowed-claims whitelist, and the
  claim-verification agent (entailment check + corrective redraft). Anything that
  remains unverified is flagged in the claim map rather than hidden.
- **Token efficiency** — small chunks, per-facet retrieval, near-dup pruning,
  cheap-model routing for extraction, and a redraft that only fires on a real
  problem. Measured per run via `TokenMeter`.
- **Resilience** — crawl tolerates http/https/www variants and per-page fetch
  failures; structured outputs prevent silent JSON failures; embeddings degrade
  to keyword ranking; missing external signals degrade to site-only evidence.
- **Security** — secrets are env/`local.env` only (never committed); the SQLite
  DB, build output, and `.venv` are gitignored.

---

## 10. Technology stack

| Layer | Choice |
|-------|--------|
| API / server | FastAPI, Uvicorn |
| LLM | Anthropic Claude (Messages API + hosted `web_search` tool) |
| Crawl / extract | httpx (async), trafilatura, tldextract |
| Embeddings (optional) | fastembed (`bge-small`, ONNX) + numpy |
| Validation / config | Pydantic, python-dotenv |
| Persistence | SQLite |
| Frontend | React, Vite, Tailwind CSS |
