# OutboundIQ — Interview Prep (Q&A)

Anticipated interview questions about OutboundIQ, grouped by theme, with answers grounded in
the actual code. Read with [`DESIGN.md`](./DESIGN.md) and
[`IMPLEMENTATION.md`](./IMPLEMENTATION.md).

> **30-second pitch.** OutboundIQ turns public company info into outbound strategy. Mode 1
> reads a sender's website and infers a value prop + structured ICP. Mode 2 researches a
> target (its site + live web search), scores ICP fit across five dimensions, plans a
> messaging strategy, drafts two evidence-backed cold emails with different angles, then
> **verifies every claim against its cited snippet and redrafts if anything is unsupported**.
> Output includes a claim map tying each claim to a citation with a verified status. It's
> **RAG-grounded, not context-stuffed** (a pluggable retriever — local embeddings when
> available, keyword ranking in production), uses a **cheap/strong model split**, and **meters
> tokens per step** so the optimization is measurable.

---

## A. Architecture & design choices

**Q1. Walk me through the architecture.**
Three backend layers with strict separation of concerns: `retrieval.py` (fetch/extract/chunk/
**embed**/dedupe/semantic-retrieve — no LLM knowledge), `agent.py` (orchestrates the agent
graph + token metering — no HTTP/SQL knowledge), and `server.py` + `db.py` (transport +
persistence). A React/Vite SPA talks to FastAPI over JSON; in production the backend also
serves the built SPA from one origin. Each layer is independently swappable — e.g. change the
embedding model or move SQLite to Postgres without touching the others.

**Q2. What makes this "agentic" rather than just prompt chaining?**
Three properties beyond chaining: (1) **tool use** — a model with Claude's server-side
`web_search` autonomously runs searches and returns structured signals; (2) a **plan-before-act
gate** — a strategist pre-computes the allowed-claims whitelist that constrains the drafter;
(3) a **self-correction loop** — a verifier judges each claim's entailment against its evidence
and triggers a corrective redraft. The pipeline also adapts: ICP + persona drive which snippets
get retrieved, and fit hooks feed strategy and drafting.

**Q3. Why split into many LLM calls instead of one big prompt?**
Single responsibility per call → tighter prompts (fewer tokens, isolated failures), different
inputs/tools per step, and the ability to route each step to the right model tier. It also
enables the verify→redraft loop and lets the sender ICP be computed once and reused across many
targets. Cost is more round-trips/latency — acceptable for an interactive tool, masked by a
staged progress UI.

**Q4. Why FastAPI + React + SQLite + local embeddings?**
FastAPI gives async (concurrent fetches) + Pydantic validation + static serving. React/Vite/
Tailwind is a fast SPA stack. SQLite is zero-config persistence enabling ICP reuse. Local
embeddings (`fastembed`/bge-small) give semantic retrieval with no second API key and no
per-token cost. All have clean upgrade paths.

---

## B. Retrieval — semantic RAG, not context stuffing

**Q5. "Based on retrieved snippets, not full-context stuffing" — how?**
Pages are fetched, boilerplate-stripped (`trafilatura`), chunked (~700 chars), and **embedded
locally**. For each reasoning step I run **facet retrieval** — a semantic query per ICP
dimension — and union the top hits into a bounded set (~24 sender / ~18 target snippets). The
model gets a compact numbered list and must cite snippet ids, which I resolve to `{url,
snippet}` for the claim map. Full page text never enters the prompt.

**Q6. Embeddings vs keyword ranking — which do you actually use?**
Both, behind one interface. Semantic retrieval (embeddings) surfaces the right evidence even when
wording differs from the query, and per-facet queries guarantee coverage of every ICP dimension —
that's the preferred path locally. But embeddings are an *optional* dependency, so the keyword
ranker (`search()`) is the production default and the fallback whenever the embedding model can't
load. `semantic_search()` picks the backend automatically; callers never branch. See Q7 for why
production is keyword-only.

**Q7. Why are embeddings *optional*, and why does production run keyword-only?**
This is a deliberate **graceful-degradation** decision driven by a real deployment constraint.
Local embeddings pull in native wheels — `numpy`, and `onnxruntime` via `fastembed` — which need
the C++ runtime (`libstdc++.so.6`) at import time. On Railway/Nixpacks that runtime isn't on the
linker path, so the app crashed at boot (`ImportError: libstdc++.so.6`), and even once fixed the
~130 MB model download on an ephemeral filesystem + onnxruntime memory risk OOMing a small
instance. Since retrieval was already written behind one interface with a keyword fallback, I
made the native deps **optional**: the default deploy is pure-Python (keyword retrieval, always
boots), and `pip install numpy fastembed` re-enables semantic search locally with **zero code
change**. I'd rather ship a deploy that always boots than a semantically-richer one that crashes
on native libs. (Talking point: this is the classic "make the expensive/fragile dependency
optional behind a stable interface" pattern.)

**Q7b. How exactly does the fallback stay invisible to the rest of the code?**
`numpy` is imported defensively (`try: import numpy as np / except: np = None`).
`_get_embedder()` returns `None` if numpy is missing; `embed_texts()` returns `None` whenever the
embedder is unavailable, and **every** `np.*` call sits behind that `None` check. So
`finalize()` becomes a no-op and `semantic_search()` calls the keyword ranker — the call sites in
`agent.py` are identical in both modes. I verified this by importing the module with numpy/
fastembed forcibly blocked: it imports, `finalize()` no-ops, and `semantic_search()` returns
correct keyword hits.

**Q8. What's the near-duplicate pruning about?**
After embedding, `finalize()` greedily drops any chunk whose cosine similarity to an
already-kept chunk ≥ 0.93. Shared nav/footer/CTA boilerplate survives per-page hashing and
would waste tokens and over-count the same "evidence." Pruning keeps the snippet set diverse
and the claim map honest. 0.93 is conservative to avoid dropping genuinely distinct text.

**Q9. How do snippet ids / citations work end to end?**
`_mk_id` = `s_ + sha1(url + first 80 chars)[:10]` — stable and deduplicating. The prompt shows
`[s_abc123] (product · https://…) text`. Models return ids in `*_evidence`/`claims` fields. I
build `evidence = {id: snippet.to_public()}`, and signals are keyed by URL so a URL is itself a
citation id. `_build_claim_map` resolves each claim and attaches a verified status; the UI shows
`N/M grounded` and per-claim ✓/~/⚠.

---

## C. Token & quality optimization

**Q10. "Optimize for token usage and quality" — what did you do, and can you prove it?**
Levers: semantic retrieval with **hard snippet caps**, **near-dup pruning**, a **cheap/strong
model split**, the **strategist pre-computing allowed claims** (shrinks the drafter's job),
`max_tokens` budgets per call, boilerplate stripping, and ICP reuse. Crucially I can *prove*
it: a `TokenMeter` records input/output tokens per step from each call's `usage`, the API
returns a `usage` summary, and the UI shows a token footer — so the optimization is observable
and regressions are obvious.

**Q11. Explain the cheap/strong model routing.**
Cheap model (Haiku): signal mining, messaging strategy, claim verification — extraction/
judgment tasks and the bulk of token volume. Strong model (Sonnet): ICP synthesis, fit scoring,
drafting + redraft — synthesis and persuasive writing. So strong-model tokens are spent only
where they move quality. Both ids are env-driven (`CHEAP_MODEL`/`STRONG_MODEL`).

**Q12. How do you guarantee email quality?**
Structural prompt constraints (80–130-word body, ≤7-word subject, one soft CTA, seniority-tuned
tone, no placeholders, two structurally-different angles) **plus** machine enforcement: the
allowed-claims gate, entailment verification of every claim, deterministic constraint checks,
and one corrective redraft. Quality isn't just asserted — it's checked and corrected.

---

## D. The agent graph & web search

**Q13. Walk through Mode 2 step by step.**
(1) Crawl + embed + dedupe the target site. (2) **Signal mining** — cheap model + `web_search`
returns validated `{finding, url, title, date_hint}`. (3) **Facet retrieval** driven by the
sender ICP + persona. (4) **Fit scoring** — strong model, five dimensions incl. buyer/persona
fit, + hooks. (5) **Messaging strategy** — cheap model produces angles + allowed/off-limits
claims. (6) **Drafting** — strong model writes two emails constrained to allowed claims.
(7) **Verify & refine** — cheap verifier checks entailment, deterministic constraint check, one
corrective redraft if needed. (8) Build the claim map. Every step is metered.

**Q14. What does the messaging-strategy step add — isn't it redundant with drafting?**
It's a deliberate **plan-before-act** separation. The strategist pre-computes the exact set of
supportable claims (each tied to evidence) and an off-limits list of tempting-but-unsupported
claims. The drafter may only assert whitelisted claims. This attacks hallucination at the
source rather than hoping the drafter stays grounded, separates cheap "strategy" from expensive
"prose," and makes the verify→redraft loop converge faster.

**Q15. How does claim verification work?**
`_annotate_claim_status` pairs each claim with its cited snippet. Claims whose evidence id can't
be resolved are marked `unsupported` with **no** model call. The rest go in one batched
cheap-model verifier call that judges each pair `supported`/`partial`/`unsupported` **using only
the snippet text** (no outside knowledge). Statuses flow into the claim map and the UI.

**Q16. What's the corrective redraft loop, and why cap it at one round?**
`_verify_and_refine` collects problems (unsupported claims + constraint violations); if any
exist, the strong model redrafts against targeted feedback (`EMAIL_REVISION_PROMPT`), then I
re-verify. It's capped at **one** round (`max_rounds=1`) to bound token cost and latency;
remaining issues are reported transparently in the `verification` summary rather than looped
forever. In practice one round resolves almost everything because the allowed-claims gate keeps
drafts close to correct.

**Q17. How do you stop the model fabricating sources during web search?**
Three defenses: the system prompt says only cite pages the search actually returned and never
fabricate URLs; I capture the real URLs from `web_search_tool_result` blocks; and post-hoc I
drop any signal whose URL isn't `http…` or that lacks a finding, stripping `<cite>` tags. Bad
rows are dropped, not trusted.

**Q18. Web-search results come back encrypted — how do you read them?**
The result *content* blocks are opaque, but the model's usable findings land in its **text
output**, and the result blocks still expose real `url`/`title`. So `_complete` reads text for
the findings and separately harvests URLs for citation/validation.

**Q19. What is the claim map / evidence panel?**
The deliverable that makes the emails auditable. Every factual claim about the target maps to a
citation; `_build_claim_map` flattens claims across both emails, resolves each to its source,
attaches the **verified status**, and sets `resolved` only when the source exists *and* the
claim verified as supported/partial. The UI renders it with a `grounded N/M` badge and per-claim
✓ verified / ~ partial / ⚠ unsupported.

---

## E. Robustness & edge cases

**Q20. Why forced tool-use for JSON instead of parsing free text?**
`_complete_json` defines an `emit_result` tool whose `input_schema` is the target shape and
forces it via `tool_choice`. The model is constrained to the contract, so a structured step
**can't silently produce a blank result**. I keep the brace-matching `_extract_json` as a
fallback (and as the primary parser for the web-search step, whose final message is a plain JSON
array).

**Q21. What if numpy/the embedding model isn't available at runtime?**
That's the *normal* production case (numpy is excluded from the default deploy). `embed_texts`
returns `None`, `finalize()` becomes a no-op (`embedded=False`, no pruning), and
`semantic_search` transparently falls back to the keyword ranker. The app degrades gracefully
rather than failing — and `meta["embedded"]` records which mode ran.

**Q22. What if a site can't be fetched or is JS-heavy?**
`fetch_html` swallows errors to `None`, guards on status ≥400 / non-HTML, and
`crawl_company_site` retries `http://` and `https://www.` variants. If the homepage truly
fails, the pipeline returns `{ok:False, error}` → 422 → UI banner. JS-rendered sites can yield
thin text (no headless browser); `favor_recall=True` mitigates it and a Playwright fallback is
the planned fix.

**Q23. What if no external signals are found?**
The signals list is `[]`. Fit, strategy, and drafting still run on the target's own-site
snippets; the trigger-led email leans on whatever exists, and the allowed-claims gate +
verifier ensure it only asserts what's supported — an honest pain-led pitch beats a fabricated
trigger.

**Q24. How does API error handling work?**
Soft pipeline failures → `{ok:False, error}` → HTTP 422 with a readable message. Unexpected
exceptions are caught, `traceback`-logged, and returned as 500 with the exception type. The
frontend's `req()` throws on non-2xx using `detail`, feeding the inline error banners.

---

## F. Data, persistence & scaling

**Q25. Why store the whole result JSON instead of normalized tables?**
The LLM output shape evolves — I added `strategy`, `verification`, and `usage` with **zero
migration** because they live in the `data` JSON blob. I promote just a few columns (domain,
fit_score, etc.) for fast list/filter views. It's a pragmatic document-store-in-SQLite pattern;
I'd normalize claims/signals out only if I needed to query across them.

**Q26. How would you scale beyond a local tool?**
Postgres (the `db.py` interface is tiny), auth + per-user scoping, a job queue with SSE/
WebSocket progress (replacing the time-driven client progress bar), cache embeddings/crawls per
domain, and an LLM-as-judge eval harness with a regression set to catch quality drift.

**Q27. Any caching?**
Within a run: content-hash dedup + near-dup pruning, and embeddings computed once in
`finalize()`. Across runs: the sender ICP is persisted and reused (the main amortization).
There's no cross-run crawl/embedding cache yet — a clear next optimization (cache vectors keyed
by domain + fetch time).

---

## G. Trade-offs, limitations & "what would you do differently"

**Q28. Biggest limitations?**
No headless rendering for JS-heavy sites; embeddings are CPU-bound and add a heavy dependency;
the corrective loop is a single round; signals aren't cross-verified; requests are synchronous
with a time-driven (not real) progress indicator; no automated quality eval. Each is a conscious
scope call with a low-risk upgrade path thanks to the layered design.

**Q29. If you had another week, what's first?**
(1) LLM-as-judge eval rubric + a regression set of sender/target/persona fixtures so I can
*measure* email quality changes. (2) Cross-run embedding/crawl cache. (3) SSE streaming of real
per-step progress + token usage. Those give the biggest robustness/observability gains.

**Q30. How would you evaluate email quality objectively?**
A rubric-based LLM-as-judge (specificity, evidence-grounding, angle differentiation, CTA
clarity, persona fit, no placeholders) over fixed fixtures, tracked as a regression suite. Plus
the hard automated check I *already* have — the `verification` summary reports
`claims_supported/claims_total`, and the claim map's `resolved` flag is machine-checkable today.

**Q31. Why two emails, and why those two angles?**
The brief asks for "meaningfully different angles." Pain-led and trigger-led are different
*strategies*, not just tone: one opens on a durable problem the persona owns, the other on a
time-sensitive event. The strategist seeds both explicitly, giving a salesperson a real A/B
choice driven by which evidence is strongest.

**Q32. Security considerations?**
It fetches arbitrary user-supplied URLs (SSRF surface) — for production I'd add allowlisting/
egress controls and block internal IP ranges. Secrets live in gitignored `local.env`; the API
key never reaches the frontend (all model calls are server-side). CORS is open (`*`) for dev and
would be locked to the known origin in prod.

---

## H. Quick-reference cheat sheet

| Topic | One-liner |
|---|---|
| Grounding | chunk → (optional embed → dedupe) → facet retrieval (semantic *or* keyword) → cite ids → verify entailment → claim map |
| Token opt | semantic caps, near-dup prune, cheap/strong split, strategist gate, `max_tokens`, ICP reuse, **per-step metering** |
| Agentic | crawl → web_search signals → fit → strategy(gate) → draft → verify → redraft → claim map |
| Models | Haiku (signals/strategy/verify) vs Sonnet (ICP synth/fit/draft/redraft), env-driven |
| Structured output | forced tool-use (`emit_result` + schema), `_extract_json` fallback |
| Strategy gate | allowed-claims whitelist + off-limits list pre-computed before drafting |
| Verification | cheap verifier judges each claim ↔ snippet (supported/partial/unsupported); ≤1 redraft |
| Two angles | pain-led (owned problem) vs trigger-led (recent event) |
| Embeddings | **optional** local `fastembed`/bge-small (lazy, cosine dedup @0.93); keyword retrieval is the production default + auto-fallback |
| Persistence | SQLite, full-JSON `data` column + promoted columns, env `DB_PATH` |
| Deploy | nixpacks: build SPA + run uvicorn, one origin serves API + app |
| Top next steps | LLM-judge eval harness · cross-run embedding cache · SSE progress |
