# OutboundIQ — Implementation Document

This is the **how**: a file-by-file, function-by-function walkthrough of how OutboundIQ
works in code. Pair it with [`DESIGN.md`](./DESIGN.md) (the *why*) and
[`INTERVIEW.md`](./INTERVIEW.md) (Q&A).

---

## 1. Tech stack

| Layer | Choice | Version | Why |
|---|---|---|---|
| Backend framework | **FastAPI** | 0.115 | Async, typed, Pydantic validation, trivial static-file serving |
| ASGI server | **uvicorn[standard]** | 0.30 | Standard FastAPI runtime |
| HTTP client | **httpx** (async) | 0.27 | Concurrent page fetches via `asyncio.gather` |
| HTML→text | **trafilatura** | 1.12 | Boilerplate-stripped main-content extraction |
| Domain parsing | **tldextract** | 5.3 | Reliable root-domain extraction for same-site link filtering |
| **Embeddings** *(optional)* | **fastembed** (`BAAI/bge-small-en-v1.5`) | 0.8 | **Local** semantic retrieval — no extra API key, no per-token cost. Omitted from the default deploy; install for local semantic RAG |
| **Vector math** *(optional)* | **numpy** | 2.4 | Cosine similarity for retrieval + near-dup pruning. Optional, paired with `fastembed` |
| LLM SDK | **anthropic** | 0.39 | Claude models, server-side `web_search`, forced tool-use for JSON |
| Validation | **pydantic** | 2.9 | Request body schemas |
| Config | **python-dotenv** | 1.0 | Loads `backend/local.env` so keys aren't shell-exported |
| Persistence | **SQLite** (stdlib `sqlite3`) | — | Zero-config, file-based |
| Frontend | **React 18 + Vite 5 + Tailwind 3** | — | Fast SPA, hot reload, utility styling |

Deployment: **nixpacks** builds the React bundle and runs uvicorn, which serves both the API
and the static SPA from one origin (Railway-ready). The default deploy is **pure-Python** —
`numpy`/`fastembed` are intentionally excluded so the image has no native-library dependencies
(no `libstdc++` / `onnxruntime`), and retrieval runs in keyword mode. Installing the two
optional packages locally re-enables semantic embeddings with no code change.

---

## 2. Repository layout

```
outbound-iq/
├── backend/
│   ├── retrieval.py     # Fetch + extract + chunk + embed + dedupe + semantic search
│   ├── agent.py         # Agent graph (Mode 1 & 2), TokenMeter, structured-output helpers
│   ├── db.py            # SQLite persistence (senders, evaluations)
│   ├── server.py        # FastAPI: API routes + serves built SPA
│   ├── requirements.txt
│   └── local.env(.example)  # ANTHROPIC_API_KEY + optional model/base overrides
├── frontend/
│   └── src/
│       ├── App.jsx        # Two modes, fit, signals, strategy, emails, claim map, usage footer
│       ├── components.jsx # Logo, ScoreRing, Bar, EvidencePill, etc.
│       ├── api.js         # Fetch wrapper + base-URL resolution
│       └── index.css / main.jsx
├── nixpacks.toml          # Railway build/run config
└── README.md
```

---

## 3. The retrieval layer — `backend/retrieval.py`

The grounding engine. Knows nothing about LLMs.

### 3.1 Local embeddings (optional, lazy, with fallback)
- `numpy` is imported **defensively** at module top: `try: import numpy as np / except: np =
  None`. So the module imports fine on a deploy where numpy isn't installed.
- `EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"`.
- `_get_embedder()` returns `None` immediately if `np is None`; otherwise it lazily imports
  `fastembed.TextEmbedding` on first use. Any failure (no numpy, no fastembed, model load
  error) sets `_embedder_failed` and returns `None` **once and forever** (no repeated retries).
- `embed_texts(texts)` returns an `(N, dim)` **L2-normalized** float32 matrix (so a dot product
  == cosine similarity), or `None` if embeddings are unavailable. This `None` is the single
  signal that triggers keyword fallback everywhere downstream — every `np.*` call lives behind
  it, so numpy is never touched when absent.

### 3.2 `Snippet` (dataclass)
`id, url, title, text, source_type ("page"|"search"), page_kind, vec (np.ndarray|None)`.
`to_public()` renames `text→snippet` for the API/UI and omits the vector.

### 3.3 IDs, cleaning, chunking
- `_mk_id(url, text)` → `"s_" + sha1(url + first-80-chars)[:10]` — stable, deduplicating ids.
- `_chunk(text, max_chars=700)` splits on sentence/paragraph boundaries and greedily packs
  ~700-char chunks (~150–180 tokens) — small enough to cite tightly, big enough to carry
  context.

### 3.4 Fetching & extraction
- `fetch_html` — async GET (15s timeout, follows redirects), guards on status ≥400 and
  non-HTML content types, swallows exceptions to `None` (a dead page never crashes the run).
- `extract_main_text` — `trafilatura.extract(..., favor_recall=True)` for main text + a regex
  `<title>` fallback.
- `discover_links` — scans homepage HTML for **same-domain** links and classifies each into a
  `page_kind` via `PRIORITY_PATH_HINTS` (path contains `pricing` → pricing, etc.).

### 3.5 `EvidenceStore`
In-memory `id→Snippet` with an embedding lifecycle.
- `add()` skips text < 40 chars and dedupes by content-hash id.
- **`finalize(dedupe_threshold=0.93)`** — embeds **all** snippets once (batched), assigns
  `s.vec`, then does **greedy near-duplicate pruning**: keep the first occurrence, drop any
  later snippet whose max cosine similarity to a kept one ≥ 0.93. **No-op** (and
  `_embedded=False`) if embeddings are unavailable — the keyword path needs no pre-processing.
- **`semantic_search(query, limit, prefer_kinds)`** — embeds the query, scores every snippet by
  `vec · q` (+0.05 for preferred page kinds), returns the top `limit`. **Falls back** to
  `search()` if embeddings are unavailable or any snippet lacks a vector.
- `search(query_terms, ...)` — the lexical keyword ranker (term-frequency, +2 page-kind boost),
  now the **fallback path**.

### 3.6 `crawl_company_site(url, max_pages)`
1. Normalize URL, derive root domain + name guess.
2. Fetch homepage; if it fails, retry `http://` and `https://www.` variants.
3. Extract + chunk the homepage into snippets.
4. `discover_links` → fetch priority pages **concurrently** (`asyncio.gather`, capped at
   `max_pages-1`), chunking each.
5. **`store.finalize()`** — embed + prune before any retrieval; record `meta["embedded"]` and
   `meta["snippet_count"]`.
6. Return `(EvidenceStore, meta)`.

---

## 4. The agentic orchestration — `backend/agent.py`

### 4.1 Configuration
```python
load_dotenv(backend/local.env)          # real env vars take precedence
CHEAP_MODEL  = env CHEAP_MODEL  or "claude-haiku-4-5"
STRONG_MODEL = env STRONG_MODEL or "claude-sonnet-4-6"
_client = Anthropic(base_url=...) if ANTHROPIC_BASE_URL else Anthropic()
```

### 4.2 Token metering — `TokenMeter`
Accumulates `input_tokens`, `output_tokens`, `calls`, and a `by_step` list (step name + model
+ tokens) from each call's `msg.usage`. `summary()` returns the totals returned in every API
response and rendered by the UI's usage footer.

### 4.3 LLM helpers
- **`_complete(model, system, user, max_tokens, tools, meter, step)`** — one
  `messages.create`; meters usage; returns `(text, search_urls)`. From
  `web_search_tool_result` blocks it harvests the **real URLs** the search surfaced (the
  result *content* is encrypted; usable findings land in the text output).
- **`_complete_json(model, system, user, schema, …)`** — **forced structured output**: defines
  a single tool `emit_result` with `input_schema=schema` and `tool_choice={"type":"tool",
  "name":"emit_result"}`, then returns the tool-call `input` dict. Falls back to
  `_extract_json` on the model's text if (rarely) no tool call is produced. This is why
  structured steps can't silently yield a blank result.
- **`_extract_json` / `_strip_cite_tags`** — fallback JSON path: strips fences + `<cite>` tags,
  then string-aware brace-matches the first balanced `{…}`/`[…]`. Still the primary parser for
  the web-search signal step (plain JSON array).
- **`_format_snippets`** — compact numbered block: `[id] (page_kind · url)\ntext`.
- **`_retrieve_facets(store, queries, per_query, cap)`** — the RAG core: runs
  `semantic_search` for each facet query, **unions and dedupes** results (order preserved),
  capped. Pulls snippets relevant to *each* reasoning facet rather than dumping pages.
- **`WEB_SEARCH_TOOL`** — `web_search_20250305`, `max_uses=4`.

### 4.4 Mode 1 — `analyze_sender(url)`
1. `crawl_company_site(url, max_pages=7)`; bail with structured error if homepage unfetchable.
2. `_retrieve_facets(store, SENDER_FACET_QUERIES, per_query=4, cap=24)` — 7 facet queries cover
   product/value-prop, industries, company size, buyer personas, pains, triggers, customers.
3. One **strong-model** `_complete_json` with `SENDER_SCHEMA` → `{one_liner,
   value_proposition (+evidence ids), category, differentiators (+evidence), icp{industries,
   size_bands, buyer_personas, common_triggers, pain_points, icp_evidence}, confidence,
   notes}`.
4. Build `evidence = {id: snippet.to_public()}`; return `{ok, profile, evidence, meta,
   snippet_count, usage}`.

### 4.5 Mode 2 — `evaluate_target(sender_profile, target_url, persona_role, persona_seniority)`

A six-step agent graph (each step metered):

**Step 1 — Signal mining (`research_target_signals`, cheap model + web_search).**
Runs 2–4 searches; final message is a JSON array of `{finding, url, title, date_hint}`. Parsed
with `_extract_json(prefer="array")` and **validated** (http URL + finding required; cite tags
stripped); invalid rows dropped.

**Step 2 — Facet retrieval (ICP- and persona-driven).**
Builds queries dynamically from the sender ICP and the persona: a "what the company does"
query, plus `pain:`, `trigger:`, `industry:` queries from the ICP, plus a persona-priorities
query and a growth/enterprise query. `_retrieve_facets(per_query=3, cap=18)` selects the
snippets relevant to *this* fit decision. Then the combined **evidence map** is built: page
snippets keyed by id + each signal keyed by its URL.

**Step 3 — Fit scoring (strong model, `FIT_SCHEMA`).**
Scores **five** dimensions (Industry, Company size, **Buyer/persona fit**, Pain match,
Triggers), each with rationale + evidence refs, plus `fit_score`, `fit_band`, `summary`, and
`best_angle_hooks`. The persona is passed in so persona fit is real.

**Step 4 — Messaging strategy (cheap model, `STRATEGY_SCHEMA`).**
Produces `likely_priorities`, a `pain_led_angle` and `trigger_led_angle`, and — critically —
`claims_allowed` (each claim tied to an evidence id/url, only if a snippet/signal supports it)
and `claims_not_allowed` (tempting-but-unsupported claims to avoid). This **gates** drafting.

**Step 5 — Email drafting (strong model, `EMAILS_SCHEMA`).**
Writes two emails (pain-led, trigger-led) constrained to the allowed-claims whitelist, with a
mandatory per-email `claims` array. Hard rules in the prompt: 80–130-word body, ≤7-word
subject, one soft CTA, no placeholders, tone tuned to seniority.

**Step 6 — Verify & refine (`_verify_and_refine`).** See §4.6.

Returns `{ok, target_name, meta, signals, fit, strategy, emails, evidence, claim_map,
verification, snippet_count, usage}`.

### 4.6 Verification, constraints & corrective redraft
- **`_annotate_claim_status(emails, evidence, meter)`** — claims whose evidence id can't be
  resolved are marked `unsupported` with **no** model call; the rest are batched into one
  cheap-model verifier call (`VERIFY_SCHEMA`, `VERIFY_SYSTEM`) that judges each `(claim,
  cited snippet)` pair as `supported` / `partial` / `unsupported` **using only the snippet
  text**. Each claim gets a `status` (+ optional `verify_reason`).
- **`_email_constraint_issues(em)`** — deterministic checks: body word count (flag <70 or
  >140, aim 80–130), subject >8 words, and a `PLACEHOLDER_RE = \[[^\]\n]{1,40}\]` test for
  bracket tokens.
- **`_collect_problems`** — per-email list of unsupported claims + constraint violations.
- **`_verify_and_refine(..., max_rounds=1)`** — annotate → collect problems → if any, do **one**
  corrective redraft (strong model, `EMAIL_REVISION_PROMPT` with targeted feedback) → re-verify
  → re-collect. Returns the emails plus a `verification` summary (`rounds`, `remaining_issues`,
  `claims_total`, `claims_supported`).
- **`_build_claim_map`** — flattens every claim across both emails, resolves its citation to
  `{url, snippet, title}`, attaches the verified `status`, and sets `resolved = (source exists
  AND status in {supported, partial})`.

---

## 5. The API — `backend/server.py`

FastAPI app with permissive CORS and `db.init_db()` on startup.

| Method | Endpoint | Handler | Notes |
|---|---|---|---|
| GET | `/api/health` | `health` | Liveness |
| POST | `/api/sender/analyze` | `sender_analyze` | Runs Mode 1, **persists** sender, returns result + `id` |
| GET | `/api/senders` | `senders` | List saved senders |
| GET | `/api/sender/{sid}` | `sender_get` | Fetch one (404 if missing) |
| POST | `/api/target/evaluate` | `target_evaluate` | Loads sender, runs Mode 2, persists evaluation |
| GET | `/api/evaluations` | `evaluations` | List (optional `?sender_id=`) |
| GET | `/api/evaluation/{eid}` | `evaluation_get` | Fetch one |

**Error handling.** Pipeline "soft" failures (e.g. unreachable site) return `{ok:False,
error}` → HTTP **422**; unexpected exceptions are `traceback`-logged and returned as **500**
with the exception type. **Static serving:** if `frontend/dist` exists, `/assets` is mounted
and a catch-all serves `index.html` for SPA routing — one origin for API + app.

---

## 6. Persistence — `backend/db.py`

Two tables, each storing the **full JSON result** in a `data` column plus promoted columns for
cheap listing/filtering:
- **`senders`**: `id (snd_…), url, domain, company_name, one_liner, data, created_at`.
- **`evaluations`**: `id (evl_…), sender_id, target_url, target_name, persona_role,
  persona_seniority, fit_score, data, created_at`.

`save_*` generate prefixed UUID ids and dump the result JSON; getters rehydrate `data` and
re-attach `id`/`created_at`. `DB_PATH` is env-overridable for a mounted volume. Storing
whole-result JSON keeps the schema stable as the LLM output shape evolves (e.g. adding
`strategy`/`verification`/`usage` required no migration).

---

## 7. Frontend — `frontend/src/`

A two-tab SPA; all server interaction goes through `api.js` (base-URL resolution:
`VITE_API_BASE` → preview port token → same-origin in PROD → `localhost:8000` in dev).

### `App.jsx`
- **Mode 1 (`SenderMode` → `SenderResult`)** — URL input, saved-sender chips, a staged
  `AgentProgress` indicator, then value prop + differentiators + structured ICP with clickable
  **evidence pills**, plus a **`UsageFooter`** (tokens in/out, model calls, snippets).
- **Mode 2 (`TargetMode` → `TargetResult`)** — pick a saved sender ICP, target URL + persona;
  results render: a **ScoreRing** + dimension **Bars** (now 5 dims); a **live signals** list;
  a **`StrategyPanel`** (pain/trigger angles, allowed-claims and off-limits lists); **two email
  cards** (color-coded, per-email claims, copy button); the **`ClaimMap`** showing each claim's
  verified **status** (✓ verified / ~ partial / ⚠ unsupported) and `N/M grounded`; and a
  `UsageFooter`.
- **`EvidenceDrawer`** — slide-over resolving any clicked evidence id/URL to its title, snippet
  text, and source link.

Progress steps are **time-driven** on the client (an interval advances the stage) — a
deliberate simplicity trade-off versus streaming real per-step progress from the server.

`components.jsx` holds presentational pieces: `Logo`, `ScoreRing` (animated SVG gauge), `Bar`,
`EvidencePill`, `Section`, `CopyButton`, `Spinner`.

---

## 8. Configuration & deployment

- **Config/secrets** live in `backend/local.env` (gitignored): `ANTHROPIC_API_KEY`, optional
  `CHEAP_MODEL`/`STRONG_MODEL`, optional `ANTHROPIC_BASE_URL`. Real env vars override the file.
- **Optional semantic retrieval** — `pip install numpy==2.4.6 fastembed==0.8.0` (these are
  commented in `requirements.txt`, not installed by the default deploy). No embeddings key
  needed; `fastembed` downloads the ~130 MB model once on first run. Without them, retrieval
  runs in keyword mode.
- **Local run**: backend `uvicorn server:app` on :8000; frontend `npm run dev` (or
  `npm run build` to let the backend serve the SPA).
- **Deploy** (`nixpacks.toml`): install deps → `npm run build` → start uvicorn on `$PORT`,
  serving API + built SPA from one origin. `DB_PATH` can point at a persistent volume.

---

## 9. End-to-end data flow (worked example)

**Mode 1** — analyze `artisan.co`:
```
POST /api/sender/analyze {url:"artisan.co"}
  → crawl: homepage + about/product/pricing/customers… (parallel)
  → finalize: embed all chunks + prune near-duplicates (no-op in keyword mode)
  → _retrieve_facets(7 ICP queries) → ~24 snippets  (semantic or keyword)
  → STRONG _complete_json(SENDER_SCHEMA): value prop + ICP citing ids
  → evidence map → db.save_sender → {profile, evidence, usage, id:"snd_…"}
```

**Mode 2** — evaluate `gusto.com` for "Head of Sales Development" (VP):
```
POST /api/target/evaluate {sender_id, target_url, persona_role, persona_seniority}
  → db.get_sender → ICP
  → crawl(gusto.com) + finalize
  → CHEAP + web_search: signals[] (validated)
  → _retrieve_facets(ICP+persona queries) → ~18 snippets → evidence map (+signals)
  → STRONG fit{5 dims, hooks}
  → CHEAP strategy{angles, claims_allowed, claims_not_allowed}
  → STRONG emails[2] constrained to allowed claims
  → CHEAP verify each claim ↔ snippet → status; constraint check
  → STRONG one corrective redraft if needed
  → claim_map (per-claim status) → db.save_evaluation
  → {fit, strategy, emails, signals, evidence, claim_map, verification, usage, id:"evl_…"}
```
The frontend renders the fit gauge + bars, signals, strategy panel, two email cards, the claim
map with verification status, and the token-usage footer; clicking any pill opens the evidence
drawer with the underlying snippet and URL.
