# OutboundIQ — Interview Prep (Q&A)

Anticipated interview questions about OutboundIQ, grouped by theme, with answers grounded in
the actual code. Read with [`DESIGN.md`](./DESIGN.md) and
[`IMPLEMENTATION.md`](./IMPLEMENTATION.md).

> **30-second pitch.** OutboundIQ turns public company info into outbound strategy. Mode 1
> reads a sender's website and infers a value prop + structured ICP. Mode 2 researches a
> target company (its site + live web search), scores ICP fit, and drafts two
> evidence-backed cold emails with different angles plus a claim map tying every factual
> claim to a citation. The whole thing is **snippet-grounded, not context-stuffed**, uses a
> **cheap/strong model split** to optimize tokens, and runs an **agentic pipeline** that
> plans → retrieves → finds signals → drafts.

---

## A. Architecture & design choices

**Q1. Walk me through the architecture.**
Three backend layers with strict separation of concerns: `retrieval.py` (fetch/extract/
chunk/rank evidence — no LLM knowledge), `agent.py` (orchestrates LLM calls over evidence —
no HTTP/SQL knowledge), and `server.py` + `db.py` (transport + persistence). A React/Vite
SPA talks to FastAPI over JSON; in production the backend also serves the built SPA from one
origin. Each layer is independently testable and swappable — e.g. I can replace the keyword
ranker with embeddings, or SQLite with Postgres, without touching the others.

**Q2. What makes this "agentic" rather than just prompt chaining?**
It *plans a sequence*, *decides what evidence to retrieve*, *uses a tool to gather live
signals*, and *composes intermediate artifacts* into a final deliverable. The clearest
agentic step is signal mining: a model with Claude's `web_search` tool autonomously runs 2–4
searches and returns structured findings. The pipeline also adapts — fit hooks from the
scoring step feed the drafting step. It's not one mega-prompt; each step has a single
responsibility, its own inputs, and sometimes its own tools.

**Q3. Why split into multiple LLM calls instead of one big prompt?**
Single responsibility per call means tighter, smaller prompts (fewer tokens, clearer failure
isolation), different inputs/tools per step, and reusability — the sender ICP is computed
once and reused across many target evaluations. The cost is more round-trips/latency, which
is acceptable for an interactive analysis tool and masked by a staged progress UI.

**Q4. Why FastAPI + React + SQLite specifically?**
FastAPI gives async (needed for concurrent page fetches), typed request validation via
Pydantic, and trivial static-file serving for the SPA. React/Vite/Tailwind is a fast,
familiar SPA stack. SQLite is zero-config and file-based — perfect for a local-first tool
that still needs persistence (ICP reuse, saved evaluations). All three have clean upgrade
paths.

---

## B. The core requirement — retrieval, not context stuffing

**Q5. The brief says "based on retrieved snippets, not full-context stuffing." How did you
implement that?**
Pages are fetched, boilerplate-stripped (`trafilatura`), and chunked into ~480-char snippets
stored in an `EvidenceStore` with content-hash ids. For each reasoning step I *select* a
bounded subset — `_select_sender_snippets` caps at 26 (Mode 1) / 22 (Mode 2) using per-page-
kind quotas — and feed the model a compact numbered list. The model must **cite snippet ids**
for every claim, and I resolve those ids back to `{url, snippet}` for the evidence panel. So
the full page text never enters the prompt; only ranked, relevant, citable snippets do.

**Q6. How does snippet selection actually work — is it semantic?**
Two mechanisms. `EvidenceStore.search()` is a keyword ranker (term-frequency over text+title,
with a page-kind boost). For the sender/target synthesis I use `_select_sender_snippets`,
which guarantees *coverage* via per-kind quotas (home/product/customers/about/pricing/blog)
rather than pure relevance — because for ICP inference you want a representative spread, not
just the top keyword hits. It's lexical, not semantic, which is the main known limitation.

**Q7. Why keyword ranking and not embeddings?**
Deliberate v1 trade-off: keyword ranking is zero-dependency, instant, deterministic, and
needs no vector store or embedding calls (saving tokens/latency/cost). The `search()`
interface is designed as a clean seam — I'd drop in embedding retrieval next, keeping keyword
as a cheap pre-filter. For a focused take-home with bounded site sizes, lexical ranking over
curated page kinds is good enough and easy to reason about.

**Q8. How do snippet ids / citations work end to end?**
`_mk_id` = `s_ + sha1(url + first 80 chars)[:10]` — stable and deduplicating. The prompt
shows `[s_abc123] (product · https://…) text`. The model returns ids in `evidence`/`claims`
fields. I build `evidence = {id: snippet.to_public()}`, and for signals I key by URL so a URL
is itself a citation id. `_build_claim_map` resolves each claim's citation and sets a
`resolved` flag; the UI shows `N/M grounded` and flags unresolved claims with ⚠.

---

## C. Token & quality optimization

**Q9. The brief says "optimize for token usage and quality." What did you do?**
Several layers: (1) **snippet caps + per-kind quotas** bound every call's input; (2)
**boilerplate stripping** removes nav/footer noise before chunking; (3) **content-hash
dedup** avoids feeding duplicate text; (4) a **cheap model** (Haiku) handles signal mining /
extraction-style work while the **strong model** (Sonnet) is reserved for synthesis, fit
scoring, and writing; (5) **`max_tokens` budgets** (1500–1800) cap output; (6) the **sender
ICP is persisted and reused**, amortizing Mode 1 across many targets; (7) a **compact numbered
snippet format** minimizes scaffolding tokens while keeping citability.

**Q10. Explain the cheap/strong model routing decision.**
Signal mining is mostly "run searches and reformat results into JSON" — cheap-model work, and
the bulk of token volume. Synthesis and persuasive email writing reward the stronger model.
So I spend strong-model tokens only where they move output quality. Both ids are env-driven
(`CHEAP_MODEL`/`STRONG_MODEL`), so the split is reconfigurable per environment without code
changes.

**Q11. How do you guarantee the emails are high quality?**
Structural constraints in the prompts: ≤120-word bodies, ≤7-word subjects, one soft CTA, tone
tuned to seniority (exec = strategic/brief; IC = concrete/operational), a hard ban on
placeholder tokens like `[First Name]`, and two *meaningfully different* angles defined
structurally — pain-led (a problem the role owns) vs trigger-led (a recent event). Plus the
mandatory claim mapping forces specificity: any sentence about the target without supporting
evidence must be removed.

---

## D. Agentic pipeline & web search

**Q12. Walk through Mode 2 step by step.**
(1) Crawl the target's own site into snippets. (2) **Signal mining** — cheap model +
`web_search` runs queries (funding, hiring, launches, news) and returns validated
`{finding, url, title, date_hint}`. (3) **Fit scoring** — strong model scores the target vs
the sender ICP across Industry, Company size, Pain match, Triggers, each with rationale +
evidence, plus outreach hooks. (4) **Drafting** — strong model writes the two emails with
mandatory claims. (5) **Assembly** — combine page snippets + signals into one evidence map,
build the claim map, persist, return.

**Q13. How do you stop the model from fabricating sources during web search?**
Three defenses: (a) the system prompt explicitly says only cite pages the search actually
returned and never fabricate a URL; (b) I capture the real URLs from the
`web_search_tool_result` blocks for validation; (c) post-hoc filtering drops any signal whose
URL doesn't start with `http` or that lacks a finding, and strips `<cite>` tags. Bad rows are
dropped, not trusted.

**Q14. Web search results come back encrypted — how do you read them?**
The search-result *content* blocks are encrypted/opaque, but the model's usable findings land
in its **text output**, and the result blocks still expose real `url`/`title`. So `_complete`
reads the text for the findings and separately harvests the URLs from
`web_search_tool_result` blocks for citation/validation.

**Q15. What is the "claim map / evidence panel"?**
It's the deliverable that makes the emails trustworthy. Every factual claim about the target
in either email is mapped to a citation in the email's `claims` array.
`_build_claim_map` flattens these across both emails, resolves each citation to its source
(url/title/snippet), and marks `resolved`. The UI renders it as a table with a
`grounded N/M` badge and flags any unsupported claim — so a user can audit exactly where each
statement came from.

---

## E. Robustness & edge cases

**Q16. The model sometimes wraps JSON in prose or code fences. How do you handle that?**
`_extract_json` strips code fences and `<cite>` tags, then does **string-aware brace
matching** — it scans for the first balanced `{…}` or `[…]`, correctly skipping braces inside
quoted strings and handling escapes — before falling back to `json.loads`. So I get valid JSON
even when the model adds commentary. (The robust upgrade is Anthropic's structured-output /
tool schema, which I note as future work.)

**Q17. What happens if a site can't be fetched or is JS-heavy?**
`fetch_html` swallows errors to `None` (a dead page never crashes the run), guards on status
≥400 and non-HTML content, and `crawl_company_site` retries `http://` and `https://www.`
variants. If the homepage truly can't be fetched, the pipeline returns `{ok:False, error}`
which the API maps to a 422 and the UI shows as a banner. JS-rendered sites can yield thin
text since there's no headless browser — `favor_recall=True` mitigates it, and a Playwright
fallback is the planned fix.

**Q18. What if no external signals are found?**
The signals list is simply empty (`[]`). Fit scoring and drafting still run on the target's
own-site snippets; the trigger-led email leans on whatever recent signal exists, and if none,
the model is constrained to only claim what's supported — better an honest pain-led pitch than
a fabricated trigger.

**Q19. How does error handling work at the API layer?**
Soft pipeline failures return `{ok:False, error}` → HTTP 422 with a readable message.
Unexpected exceptions are caught, `traceback`-logged server-side, and returned as 500 with the
exception type. The frontend's `req()` wrapper throws on non-2xx using the `detail`, which
feeds the inline error banners.

---

## F. Data, persistence & scaling

**Q20. Why store the whole result JSON in a `data` column instead of normalized tables?**
The LLM output shape evolves; storing the full JSON keeps the schema stable while I promote
just a few columns (domain, company_name, fit_score, etc.) for fast list/filter views. It's a
pragmatic document-store-in-SQLite pattern. If I needed rich querying over claims or signals,
I'd normalize those out.

**Q21. How would you scale this beyond a local tool?**
Swap SQLite for Postgres (the `db.py` interface is tiny), add auth + per-user scoping, move
the pipeline to a job queue with SSE/WebSocket progress streaming (replacing the time-driven
client progress bar), add caching of crawled snippets by domain, and add an LLM-as-judge eval
harness with a regression set of sender/target pairs to catch quality drift.

**Q22. Is there any caching? The same site might be analyzed repeatedly.**
Within a run, `EvidenceStore` dedups by content hash. Across runs, the sender ICP is persisted
and reused (the main amortization). There's no cross-run crawl cache yet — that's a clear next
optimization (cache snippets keyed by domain + fetch time).

---

## G. Trade-offs, limitations & "what would you do differently"

**Q23. What are the biggest limitations?**
Lexical (not semantic) retrieval; no headless rendering for JS-heavy sites; best-effort JSON
parsing instead of enforced schemas; signals aren't cross-verified against a second source;
synchronous blocking requests with a time-driven (not real) progress indicator; no automated
quality eval. Each is a conscious v1 scope decision with a low-risk upgrade path thanks to the
layered design.

**Q24. If you had another week, what's first?**
(1) Embedding retrieval behind the existing `search()` seam. (2) Anthropic structured-output
to kill JSON-parsing fragility. (3) An LLM-as-judge eval rubric + regression set so I can
measure email quality changes. Those three give the biggest quality/robustness gains for the
effort.

**Q25. How would you evaluate email quality objectively?**
A rubric-based LLM-as-judge (specificity, evidence-grounding, angle differentiation, CTA
clarity, persona fit, no placeholders), scored over a fixed set of sender/target/persona
fixtures, tracked as a regression suite. Plus a hard automated check that 100% of email claims
resolve in the claim map — that's already machine-checkable today via the `resolved` flag.

**Q26. Why two emails, and why those two angles?**
The brief asks for "meaningfully different angles." Pain-led and trigger-led are genuinely
different *strategies*, not just tone: one opens on a durable problem the persona owns, the
other on a time-sensitive event (funding/hiring/launch). That gives a salesperson a real A/B
choice driven by what evidence is strongest for a given account.

**Q27. Security considerations?**
It fetches arbitrary user-supplied URLs (SSRF surface) — for production I'd add allowlisting/
egress controls and block internal IP ranges. Secrets live in gitignored `local.env`. CORS is
currently open (`*`) for dev convenience and would be locked to the known origin in prod. The
API key never reaches the frontend; all model calls are server-side.

---

## H. Quick-reference cheat sheet

| Topic | One-liner |
|---|---|
| Grounding | Chunk → store → rank → feed bounded numbered snippets → cite ids → resolve to claim map |
| Token opt | Snippet caps + per-kind quotas, boilerplate strip, dedup, cheap/strong split, `max_tokens`, ICP reuse |
| Agentic | plan → crawl → web_search signals → score fit → draft → assemble claim map |
| Models | Haiku (signals/extraction) vs Sonnet (synthesis/scoring/writing), env-driven |
| Two angles | pain-led (owned problem) vs trigger-led (recent event) |
| Claim map | every target claim → citation, with `resolved` flag and `N/M grounded` badge |
| JSON safety | fence/cite strip + string-aware brace matching + `json.loads` fallback |
| Robustness | error→None fetch, http/www fallback, soft errors→422, dropped invalid signals |
| Persistence | SQLite, full-JSON `data` column + promoted columns, env `DB_PATH` |
| Deploy | nixpacks: build SPA + run uvicorn, one origin serves API + app |
| Top next steps | embeddings · structured output · LLM-judge eval harness |
