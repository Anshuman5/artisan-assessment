# OutboundIQ

Turn public company information into outbound strategy. OutboundIQ is an agentic web app that reads a company's public web pages and live search results to infer who they sell to, then drafts evidence-backed outbound emails for a specific target account and persona.

Every claim in the output is grounded in a retrieved snippet — the app plans, fetches evidence, mines signals, and drafts, rather than stuffing full pages into the model.

## Features

### Mode 1 — ICP & Value Proposition
Given a **sender** company's website (e.g. `artisan.co`), the agent:
- Fetches and reads public pages (about, product, pricing, customers, blog, careers)
- Infers a concise **value proposition**
- Produces a structured **ICP**: target industries, size bands, common triggers, and likely buyer personas
- Backs every inference with traceable evidence snippets

### Mode 2 — Target Evaluation & Outbound Drafting
Given a **target** company's website plus a recipient persona (role + seniority), the agent:
- Researches the account using live web pages and search
- Scores how well the target fits the saved sender ICP
- Drafts **two outbound emails** with meaningfully different angles (pain-led vs trigger-led)
- Outputs a **claim map** listing every factual claim used, with supporting URL + snippet

## Architecture

```
outbound-iq/
├── backend/              # FastAPI + agentic pipeline (Python)
│   ├── retrieval.py      # Live fetch + chunking + local embeddings, semantic
│   │                     #   retrieval, near-duplicate dedup, evidence store
│   ├── agent.py          # Agent graph: research, signal mining, ICP fit,
│   │                     #   messaging strategy, drafting, claim verification
│   ├── db.py             # SQLite persistence (senders + evaluations)
│   └── server.py         # API endpoints + serves the built frontend
└── frontend/             # Vite + React + Tailwind SPA
    └── src/              # App.jsx, components.jsx, api.js, styles
```

**Mode 2 agent graph**

```
crawl ─▶ embed + dedupe ─▶ facet retrieval ─▶ signal extraction (web search)
      ─▶ ICP fit scoring ─▶ messaging strategy (allowed-claims gate)
      ─▶ email drafting ─▶ claim verification (entailment) ─▶ constraint check
      ─▶ corrective redraft (≤1) ─▶ claim map
```

**Design highlights**
- **Snippet-grounded RAG, not full-context stuffing:** pages are chunked, embedded locally (BAAI `bge-small` via `fastembed` — no extra API key), and only the snippets semantically closest to each reasoning facet (industries, personas, pains, triggers, persona-fit) are fed to the model. Near-duplicate boilerplate is pruned by cosine similarity.
- **Explicit claim verification:** every factual claim about the target is checked for entailment against its cited snippet by a verifier agent. Unsupported claims trigger one corrective redraft and are flagged in the claim map — preventing hallucinated facts from shipping.
- **Messaging strategist gates the drafter:** before drafting, a strategist produces the angles plus an *allowed-claims* whitelist (and an off-limits list); the drafter may only assert approved claims.
- **Constraint enforcement:** emails are validated for length (80–130 words), subject length, and placeholder tokens, with one corrective pass on violation.
- **Balanced model routing:** a cheap model handles signal-mining, strategy, and verification; a stronger model handles ICP synthesis and drafting.
- **Token accounting:** every run reports input/output tokens per step so the token-optimization is measurable (shown in the UI).
- **Structured outputs:** model steps use tool-use/JSON-schema forcing, so malformed output can't silently produce a blank result.
- **Persistence:** sender ICP profiles and target evaluations are saved to SQLite, so a sender's ICP can be reused across many target accounts.

## Getting started

### Prerequisites
- Python 3.10+
- Node.js 18+
- An Anthropic API key (the agent uses Claude models)
- No embeddings key needed — retrieval uses a local model (`fastembed`, ~130 MB, downloaded once on first run)

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Set your API key in a local env file (loaded automatically on startup):
cp local.env.example local.env
# then edit local.env and set ANTHROPIC_API_KEY=sk-ant-...

uvicorn server:app --host 0.0.0.0 --port 8000
```

The backend reads `backend/local.env` on startup (via `python-dotenv`), so you
don't need to `export` anything in your shell. `local.env` is gitignored — keep
your real key there. Any real environment variables (e.g. those set on Railway)
take precedence over `local.env`.

The API runs at `http://localhost:8000`. A `data.db` SQLite file is created automatically on first run.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev      # dev server with hot reload
```

For a production build (served by the backend):

```bash
npm run build    # outputs to frontend/dist
```

When built, the backend serves the frontend at the root path, so visiting `http://localhost:8000` gives you the full app.

## API reference

| Method | Endpoint                  | Description                                  |
|--------|---------------------------|----------------------------------------------|
| POST   | `/api/sender/analyze`     | Analyze a sender site → value prop + ICP     |
| GET    | `/api/senders`            | List saved sender profiles                   |
| GET    | `/api/sender/{sid}`       | Get a saved sender profile                   |
| POST   | `/api/target/evaluate`    | Evaluate a target + persona → fit + emails   |
| GET    | `/api/evaluations`        | List saved target evaluations                |
| GET    | `/api/evaluation/{eid}`   | Get a saved evaluation                       |
| GET    | `/api/health`             | Health check                                 |

## Notes
- Secrets live in `backend/local.env` (gitignored); commit only `local.env.example`.
- The SQLite database (`data.db`), `node_modules/`, build output (`dist/`), and `.venv/` are gitignored.
- Email drafts avoid placeholder tokens — claims are populated from real retrieved evidence.
