# OutboundIQ — Implementation Document

This is the **how**: a file-by-file, function-by-function walkthrough of how OutboundIQ
works in code. Pair it with [`DESIGN.md`](./DESIGN.md) (the *why*) and
[`INTERVIEW.md`](./INTERVIEW.md) (Q&A).

---

## 1. Tech stack

| Layer | Choice | Version | Why |
|---|---|---|---|
| Backend framework | **FastAPI** | 0.115 | Async, typed, auto-validation via Pydantic, trivial static-file serving |
| ASGI server | **uvicorn[standard]** | 0.30 | Standard FastAPI runtime |
| HTTP client | **httpx** (async) | 0.27 | Concurrent page fetches with `asyncio.gather` |
| HTML→text | **trafilatura** | 1.12 | Boilerplate-stripped main-content extraction |
| Domain parsing | **tldextract** | 5.3 | Reliable root-domain extraction for same-site link filtering |
| LLM SDK | **anthropic** | 0.39 | Claude models + server-side `web_search` tool |
| Validation | **pydantic** | 2.9 | Request body schemas |
| Config | **python-dotenv** | 1.0 | Loads `backend/local.env` so keys aren't shell-exported |
| Persistence | **SQLite** (stdlib `sqlite3`) | — | Zero-config, file-based |
| Frontend | **React 18 + Vite 5 + Tailwind 3** | — | Fast SPA, hot reload, utility styling |

Deployment: **nixpacks** config builds the React bundle and runs uvicorn, which serves both
the API and the static SPA from one origin (Railway-ready).

---

## 2. Repository layout

```
outbound-iq/
├── backend/
│   ├── retrieval.py     # Fetch + extract + chunk + EvidenceStore + ranking
│   ├── agent.py         # Agentic orchestration: Mode 1 & Mode 2 + LLM helpers
│   ├── db.py            # SQLite persistence (senders, evaluations)
│   ├── server.py        # FastAPI app: API routes + serves built SPA
│   ├── requirements.txt
│   └── local.env(.example)  # ANTHROPIC_API_KEY + optional model/base overrides
├── frontend/
│   └── src/
│       ├── App.jsx        # Two modes, results, claim map, evidence drawer
│       ├── components.jsx # Logo, ScoreRing, Bar, EvidencePill, etc.
│       ├── api.js         # Fetch wrapper + base-URL resolution
│       └── index.css / main.jsx
├── nixpacks.toml          # Railway build/run config
└── README.md
```

---

## 3. The retrieval layer — `backend/retrieval.py`

The grounding engine. Knows nothing about LLMs.

### 3.1 `Snippet` (dataclass)
A compact, citable unit: `id, url, title, text, source_type ("page"|"search"),
page_kind ("home"|"product"|…)`. `to_public()` renames `text→snippet` for the API/UI.

### 3.2 IDs and cleaning
- `_mk_id(url, text)` → `"s_" + sha1(url + first-80-chars-of-text)[:10]`. Content-hash ids
  give **stable, deduplicated** snippet ids across a run.
- `_clean_text` collapses whitespace.

### 3.3 Chunking — `_chunk(text, max_chars=480)`
Splits extracted text on sentence/paragraph boundaries, then **packs** parts greedily into
~480-char chunks. This balances precision (small enough to cite tightly) against scaffolding
overhead (not so small that prompts fill with ids).

### 3.4 Fetching — `fetch_html` + `crawl_company_site`
- `fetch_html` does an async GET (15s timeout, follows redirects), guards on status ≥ 400
  and non-HTML content types, and swallows exceptions to `None` (a dead page never crashes
  the run).
- `crawl_company_site(url, max_pages)`:
  1. Normalizes the URL, derives the root domain and a name guess.
  2. Fetches the homepage; if that fails, retries `http://` and `https://www.` variants.
  3. Extracts main text (`extract_main_text`, trafilatura + regex title fallback) and
     chunks the homepage into snippets.
  4. `discover_links` scans the homepage HTML for **same-domain** links and classifies each
     into a `page_kind` via `PRIORITY_PATH_HINTS` (e.g. path contains `pricing`→pricing).
  5. Fetches the discovered priority pages **concurrently** (`asyncio.gather`), capped at
     `max_pages-1`, chunking each into the store.
  6. Returns `(EvidenceStore, meta)` where `meta` records domain, name, and pages fetched.

### 3.5 `EvidenceStore`
In-memory dict of `id→Snippet`.
- `add()` skips text shorter than 40 chars and dedupes by content-hash id.
- `search(query_terms, limit, prefer_kinds)` is a **lightweight keyword ranker**: counts
  term occurrences in `text+title`, boosts preferred page kinds by +2, sorts, returns the
  top `limit`. Deterministic, instant, zero-dependency — and a clean seam to later swap for
  embeddings.

---

## 4. The agentic orchestration — `backend/agent.py`

### 4.1 Configuration
```python
load_dotenv(backend/local.env)          # real env vars take precedence
CHEAP_MODEL  = env CHEAP_MODEL  or "claude-haiku-4-5"
STRONG_MODEL = env STRONG_MODEL or "claude-sonnet-4-6"
_client = Anthropic(base_url=...) if ANTHROPIC_BASE_URL else Anthropic()
```
Model ids and base URL are env-driven so the same code runs against a real Anthropic key or
a proxy, and the cheap/strong split is reconfigurable without code changes.

### 4.2 LLM helpers
- **`_complete(model, system, user, max_tokens, tools)`** — single `messages.create` call.
  Walks the response content blocks: collects `text` blocks, and from
  `web_search_tool_result` blocks captures the **real URLs** the search surfaced. Returns
  `(text, search_urls)`. (Search-result *content* comes back encrypted; the usable findings
  land in the model's text output, so we read text and keep the URLs for validation.)
- **`_strip_cite_tags`** — removes `<cite index=…>…</cite>` wrappers that web-search adds.
- **`_extract_json(text, prefer)`** — robust JSON extraction: strips code fences, then
  **brace-matches** the first balanced `{…}` or `[…]` (string-aware, handles escapes),
  falling back to `json.loads`. This tolerates the model wrapping JSON in prose.
- **`_format_snippets(snips)`** — renders the compact numbered prompt block:
  `[id] (page_kind · url)\ntext`.
- **`WEB_SEARCH_TOOL`** — `web_search_20250305`, `max_uses=4`.

### 4.3 Mode 1 — `analyze_sender(url)`
1. `crawl_company_site(url, max_pages=7)`; bail with a structured error if the homepage
   can't be fetched.
2. `_select_sender_snippets(store, cap=26)` — selects a **balanced** snippet set using a
   priority order and per-kind quotas (`home:6, product:6, customers:5, about:4,
   pricing:3, blog:2`). This is the token-bounding step.
3. One **strong-model** call with `SENDER_SYSTEM` + `SENDER_PROMPT`, which demands strict
   JSON: `one_liner, value_proposition (+evidence ids), category, differentiators (+evidence),
   icp{industries, size_bands, buyer_personas, common_triggers, pain_points, icp_evidence},
   confidence, notes`.
4. Parse JSON, collect all cited ids, build `evidence = {id: snippet.to_public()}`, and
   return `{ok, profile, evidence, meta, snippet_count}`.

`SENDER_SYSTEM` enforces the core rule: infer **strictly from snippets**, cite every claim,
don't invent, and reflect thin evidence in `confidence`.

### 4.4 Mode 2 — `evaluate_target(sender_profile, target_url, persona_role, persona_seniority)`

Three LLM steps over the target's evidence:

**Step 1 — Signal mining (`research_target_signals`, cheap model + web_search).**
- Prompts the model to run 2–4 searches and return **only** a JSON array of
  `{finding, url, title, date_hint}`, preferring the last 18 months and never fabricating
  URLs.
- The response is parsed (`prefer="array"`) and **validated**: each row must have a finding
  and an `http…` URL; cite tags are stripped. Invalid rows are dropped.

**Step 2 — Fit scoring (strong model, `FIT_SYSTEM`/`FIT_PROMPT`).**
- Inputs: sender value prop + ICP JSON, target snippets (`_select_sender_snippets(cap=22)`),
  and the signals block.
- Output JSON: `fit_score (0–100)`, `fit_band`, `summary`, four `dimension_scores`
  (Industry, Company size, Pain match, Triggers) each with rationale + evidence refs, and
  `best_angle_hooks`. The prompt explicitly demands honesty (poor fit → low score).

**Step 3 — Email drafting (strong model, `EMAIL_SYSTEM`/`EMAIL_PROMPT`).**
- Inputs: sender one-liner/value-prop/differentiators, persona role+seniority, fit summary
  + hooks, target snippets, and signals.
- Produces **two emails**: `pain-led` (opens on an evidenced pain the role owns) and
  `trigger-led` (opens on a recent signal). Hard rules: ≤120-word bodies, ≤7-word subjects,
  one soft CTA, tone tuned to seniority, **no placeholder tokens**, and a **mandatory
  `claims` array** mapping every factual claim about the target to a snippet id or signal
  URL.

**Assembly.**
- Build a combined `evidence` map: page snippets keyed by id, plus each signal keyed by its
  URL (so URLs act as citation ids too).
- `_build_claim_map(emails, evidence)` flattens every claim across both emails, resolves its
  citation to `{url, snippet, title}`, and sets a `resolved` boolean.
- Return `{ok, target_name, meta, signals, fit, emails, evidence, claim_map, snippet_count}`.

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

**Error handling.** Pipeline "soft" failures (e.g. unreachable site) return
`{ok: False, error}` and are surfaced as HTTP **422**; unexpected exceptions are logged
(`traceback`) and returned as **500** with the exception type — so the frontend can show a
meaningful message.

**Static serving.** If `frontend/dist` exists, `/assets` is mounted and a catch-all route
serves `index.html` for SPA routing — one origin for API + app in production.

---

## 6. Persistence — `backend/db.py`

Two tables, both storing the **full JSON result** in a `data` column plus a few promoted
columns for cheap listing/filtering:

- **`senders`**: `id (snd_…), url, domain, company_name, one_liner, data, created_at`.
- **`evaluations`**: `id (evl_…), sender_id, target_url, target_name, persona_role,
  persona_seniority, fit_score, data, created_at`.

`save_sender`/`save_evaluation` generate prefixed UUID ids and dump the result JSON;
getters rehydrate `data` and re-attach `id`/`created_at`. `DB_PATH` is env-overridable for a
mounted volume. Storing whole-result JSON keeps the schema stable as the LLM output shape
evolves, while promoted columns keep list views fast.

---

## 7. Frontend — `frontend/src/`

A two-tab SPA; all server interaction goes through `api.js`.

### 7.1 `api.js`
Resolves the API base across environments: explicit `VITE_API_BASE` → a preview-deploy port
token → same-origin in PROD → `http://localhost:8000` in dev. `req()` is a thin fetch wrapper
that parses JSON and throws `detail` on non-2xx (feeding the UI error banners).

### 7.2 `App.jsx`
- **Mode 1 (`SenderMode`)** — URL input, "Analyze company", saved-sender chips. Shows a
  staged `AgentProgress` indicator (Fetching → Extracting → Inferring → Synthesizing) while
  the request runs. `SenderResult` renders the value prop, differentiators, and structured
  ICP, each with clickable **evidence pills**.
- **Mode 2 (`TargetMode`)** — pick a saved sender ICP, enter target URL + persona
  (role + seniority). `TargetResult` renders: a **ScoreRing** + dimension **Bars** for fit,
  a **live signals** list with source links, **two email cards** (color-coded by angle,
  with per-email claims and a copy button), and the **ClaimMap** table showing
  `resolved/total grounded` and flagging any ⚠ unsupported claim.
- **`EvidenceDrawer`** — slide-over that resolves any clicked evidence id/URL to its title,
  snippet text, and source link.

The progress steps are **time-driven** on the client (an interval advances the stage) — a
deliberate simplicity trade-off versus streaming real progress from the server.

### 7.3 `components.jsx`
Presentational pieces: `Logo`, `ScoreRing` (animated SVG gauge, color by band), `Bar`
(dimension score), `EvidencePill` (renders id or `↗ source` for URLs), `Section`,
`CopyButton`, `Spinner`.

---

## 8. Configuration & deployment

- **Secrets/config** live in `backend/local.env` (gitignored): `ANTHROPIC_API_KEY`, optional
  `CHEAP_MODEL`/`STRONG_MODEL`, optional `ANTHROPIC_BASE_URL`. Real env vars override the
  file.
- **Local run**: backend `uvicorn server:app` on :8000; frontend `npm run dev` (or
  `npm run build` to let the backend serve the SPA).
- **Deploy** (`nixpacks.toml`): install Python + Node deps → `npm run build` → start uvicorn
  on `$PORT`, serving API + built SPA from one origin. `DB_PATH` can point at a persistent
  volume.

---

## 9. End-to-end data flow (worked example)

**Mode 1** — analyze `artisan.co`:
```
POST /api/sender/analyze {url:"artisan.co"}
  → crawl_company_site: homepage + about/product/pricing/customers… (parallel)
  → _select_sender_snippets: ~26 balanced snippets
  → STRONG_MODEL: JSON {value_prop, differentiators, icp, confidence} citing ids
  → resolve ids → evidence map
  → db.save_sender → {profile, evidence, meta, id:"snd_…"}
```

**Mode 2** — evaluate `gusto.com` for "Head of Sales Development" (VP):
```
POST /api/target/evaluate {sender_id, target_url, persona_role, persona_seniority}
  → db.get_sender → sender profile/ICP
  → crawl_company_site(gusto.com)
  → CHEAP_MODEL + web_search: signals[] {finding,url,title,date_hint} (validated)
  → STRONG_MODEL: fit {score, band, dimension_scores, hooks}
  → STRONG_MODEL: emails[2] {angle, subject, body, claims[]}
  → _build_claim_map: resolve every claim → source (+resolved flag)
  → db.save_evaluation → full result {fit, emails, signals, evidence, claim_map, id:"evl_…"}
```
The frontend renders fit gauge + bars, signals, two email cards, and the claim map; clicking
any pill opens the evidence drawer with the underlying snippet and URL.
