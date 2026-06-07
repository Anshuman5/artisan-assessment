"""
Agentic orchestration for OutboundIQ.

Two pipelines:
  Mode 1: analyze_sender(url)  -> value prop + structured ICP
  Mode 2: evaluate_target(sender_profile, target_url, persona) -> fit + 2 emails + claim map

Token strategy:
  - We never stuff full page text into the model. We crawl, chunk, and store snippets,
    then retrieve only the top-ranked snippets relevant to each reasoning step.
  - Each LLM call gets a compact, numbered snippet list. The model must cite snippet ids,
    which we resolve back to {url, snippet} for the claim map. This keeps grounding tight.
  - Cheap model (haiku) for extraction/signal-finding; strong model (sonnet) for synthesis/drafting.
"""
from __future__ import annotations

import json
import re

from anthropic import Anthropic

from retrieval import (
    EvidenceStore,
    crawl_company_site,
    company_name_guess,
)

CHEAP_MODEL = "claude_haiku_4_5"
STRONG_MODEL = "claude_sonnet_4_6"

_client = Anthropic()


# ---------------------------------------------------------------------------
# Low-level LLM helpers
# ---------------------------------------------------------------------------

def _complete(model: str, system: str, user: str, max_tokens: int = 1500,
              tools: list | None = None) -> tuple[str, list]:
    """Return (text, web_search_meta) for a single completion.

    Web search results come back as encrypted blocks (not readable). The actual
    findings + URLs land in the model's TEXT output. We capture the URLs that the
    search surfaced (from result blocks) so we can validate citations later.
    """
    kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    if tools:
        kwargs["tools"] = tools
    msg = _client.messages.create(**kwargs)
    text_parts, search_urls = [], []
    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "web_search_tool_result":
            items = block.content if isinstance(block.content, list) else []
            for it in items:
                url = getattr(it, "url", None)
                if url:
                    search_urls.append({"url": url, "title": getattr(it, "title", "") or url})
    return "\n".join(text_parts).strip(), search_urls


def _strip_cite_tags(text: str) -> str:
    """Web search responses wrap quoted facts in <cite index=...>...</cite>. Unwrap them."""
    text = re.sub(r'<cite[^>]*>', '', text)
    text = text.replace('</cite>', '')
    return text


def _extract_json(text: str, prefer: str = "auto") -> dict | list | None:
    """Best-effort JSON extraction. prefer='array' | 'object' | 'auto'."""
    text = _strip_cite_tags(text.strip())
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    pairs = [("[", "]"), ("{", "}")] if prefer == "array" else [("{", "}"), ("[", "]")]

    def _scan(opener, closer):
        start = text.find(opener)
        if start == -1:
            return None
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        return None
        return None

    for opener, closer in pairs:
        res = _scan(opener, closer)
        if res is not None:
            return res
    try:
        return json.loads(text)
    except Exception:
        return None


def _format_snippets(snips) -> str:
    """Numbered, compact snippet list for the prompt. Uses snippet ids for citation."""
    lines = []
    for s in snips:
        lines.append(f"[{s.id}] ({s.page_kind} · {s.url})\n{s.text}")
    return "\n\n".join(lines)


WEB_SEARCH_TOOL = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]


# ---------------------------------------------------------------------------
# MODE 1 — Sender: value proposition + ICP
# ---------------------------------------------------------------------------

SENDER_SYSTEM = """You are a B2B go-to-market analyst. You infer a company's value proposition and Ideal Customer Profile (ICP) STRICTLY from the evidence snippets provided. Every factual claim must be grounded in a snippet id. Do not invent facts, customers, metrics, or integrations that are not in the snippets. If evidence is thin, say so in the confidence field. Output only valid JSON."""

SENDER_PROMPT = """Company: {name} ({domain})

EVIDENCE SNIPPETS (cite these ids):
{snippets}

Produce a JSON object with this exact shape:
{{
  "company_name": string,
  "one_liner": string,                      // <= 18 words, what they do
  "value_proposition": string,              // 2-3 sentences, outcome-focused, grounded
  "value_prop_evidence": [snippet_id, ...], // ids supporting the value prop
  "category": string,                       // product category / market
  "differentiators": [ {{"point": string, "evidence": [snippet_id,...]}} ],  // up to 4
  "icp": {{
     "target_industries": [string, ...],
     "company_size_bands": [string, ...],   // e.g. "11-50", "201-1000", "Enterprise (1000+)"
     "buyer_personas": [ {{"role": string, "why": string}} ],  // who signs / champions
     "common_triggers": [string, ...],      // buying triggers / events that create need
     "pain_points": [string, ...],          // problems the product solves
     "icp_evidence": [snippet_id, ...]
  }},
  "confidence": "high" | "medium" | "low",
  "notes": string                           // gaps / assumptions
}}

Rules:
- Base industries, size, personas, triggers ONLY on signals in the snippets (customers mentioned, language used, pricing tiers, who they sell to). If you infer, keep it conservative and reflect it in confidence.
- Keep arrays tight (no filler). Return ONLY the JSON."""


async def analyze_sender(url: str) -> dict:
    store, meta = await crawl_company_site(url, max_pages=7)
    if meta.get("error"):
        return {"ok": False, "error": meta["error"], "meta": meta}

    name = meta.get("company_name") or company_name_guess(url)

    # Retrieve a balanced, deduped snippet set across page kinds (token-bounded).
    selected = _select_sender_snippets(store)
    snippet_block = _format_snippets(selected)

    text, _ = _complete(
        STRONG_MODEL,
        SENDER_SYSTEM,
        SENDER_PROMPT.format(name=name, domain=meta["domain"], snippets=snippet_block),
        max_tokens=1800,
    )
    data = _extract_json(text) or {}

    # Attach the evidence map so the UI can render citations.
    used_ids = set()
    used_ids.update(data.get("value_prop_evidence", []) or [])
    used_ids.update(data.get("icp", {}).get("icp_evidence", []) or [])
    for d in data.get("differentiators", []) or []:
        used_ids.update(d.get("evidence", []) or [])

    evidence = {s.id: s.to_public() for s in selected}
    return {
        "ok": True,
        "profile": data,
        "evidence": evidence,
        "meta": meta,
        "snippet_count": len(store.snippets),
    }


def _select_sender_snippets(store: EvidenceStore, cap: int = 26) -> list:
    """Pick a token-bounded, representative snippet set across page kinds."""
    by_kind: dict[str, list] = {}
    for s in store.all():
        by_kind.setdefault(s.page_kind, []).append(s)
    # priority order for ICP inference
    order = ["home", "product", "customers", "about", "pricing", "blog", "careers", "other", "contact"]
    quota = {"home": 6, "product": 6, "customers": 5, "about": 4, "pricing": 3, "blog": 2}
    selected = []
    for kind in order:
        items = by_kind.get(kind, [])
        selected.extend(items[: quota.get(kind, 1)])
        if len(selected) >= cap:
            break
    return selected[:cap]


# ---------------------------------------------------------------------------
# MODE 2 — Target: research, fit, emails, claim map
# ---------------------------------------------------------------------------

SIGNALS_SYSTEM = """You are a sales researcher. Use the web_search tool to find recent, specific, citable signals about the target company that matter for outbound: funding, growth/hiring, product launches, leadership changes, expansion, partnerships, pain indicators, or strategic initiatives. Prefer the last 18 months. Never fabricate a URL — only cite pages your search actually returned.

After you finish searching, your FINAL message must be ONLY a JSON array (no prose, no markdown, no code fence) of this exact shape:
[ {"finding": "<one specific sentence>", "url": "<real source url>", "title": "<source title>", "date_hint": "<e.g. Nov 2025>"} ]
Return 3-6 items. If you find nothing credible, return []."""

SIGNALS_PROMPT = """Target company: {name} (domain: {domain}).
Search the web for the most relevant recent signals for a B2B seller approaching this company, then return the JSON array described in your instructions. Run 2-4 searches (e.g. "{name} funding", "{name} hiring", "{name} product launch news", "{name} {domain} announcement")."""


def research_target_signals(name: str, domain: str) -> tuple[list[dict], list[dict]]:
    """Returns (signals, search_urls). Uses web_search tool."""
    text, search_urls = _complete(
        CHEAP_MODEL,
        SIGNALS_SYSTEM,
        SIGNALS_PROMPT.format(name=name, domain=domain),
        max_tokens=1500,
        tools=WEB_SEARCH_TOOL,
    )
    signals = _extract_json(text, prefer="array")
    if not isinstance(signals, list):
        signals = []
    cleaned = []
    for s in signals:
        if isinstance(s, dict) and s.get("url", "").startswith("http") and s.get("finding"):
            cleaned.append({
                "finding": _strip_cite_tags(str(s.get("finding", ""))).strip(),
                "url": s["url"].strip(),
                "title": str(s.get("title", "")).strip() or s["url"],
                "date_hint": str(s.get("date_hint", "")).strip(),
            })
    return cleaned, search_urls


FIT_SYSTEM = """You are a B2B qualification analyst. You score how well a TARGET company fits a SENDER's ICP, using ONLY the evidence snippets and signals provided. Cite snippet ids / signal urls for each rationale. Be honest: a poor fit must score low. Output only valid JSON."""

FIT_PROMPT = """SENDER value proposition: {value_prop}
SENDER ICP:
{icp_json}

TARGET company: {target_name} ({target_domain})

TARGET website evidence snippets (cite ids):
{snippets}

TARGET external signals (cite by url):
{signals}

Evaluate fit. Return ONLY JSON:
{{
  "fit_score": integer 0-100,
  "fit_band": "Strong" | "Moderate" | "Weak",
  "summary": string,                  // 2 sentences
  "dimension_scores": [
     {{"dimension": "Industry", "score": 0-100, "rationale": string, "evidence": [id_or_url,...]}},
     {{"dimension": "Company size", "score": 0-100, "rationale": string, "evidence": [...]}},
     {{"dimension": "Pain match", "score": 0-100, "rationale": string, "evidence": [...]}},
     {{"dimension": "Triggers", "score": 0-100, "rationale": string, "evidence": [...]}}
  ],
  "best_angle_hooks": [string, ...]   // 2-3 concrete hooks for outreach grounded in evidence
}}"""


EMAIL_SYSTEM = """You are an elite B2B SDR copywriter. You write short, specific, human cold emails that earn replies. You ground EVERY factual claim about the recipient's company in a provided evidence id (from page snippets) or signal url — no generic flattery, no invented facts. You write to a specific persona.

HARD RULES:
- NEVER use bracketed placeholders like [First Name], [Company], [Name]. Open with a simple "Hi there," or address the role/company directly. Use the real target company name.
- The "claims" array is MANDATORY and must list every sentence in the body that asserts a fact about the TARGET company (what they do, their market, their customers, a recent event). For each, give the supporting evidence id or signal url. If a sentence about the target has no supporting evidence, REMOVE that sentence — do not write unsupported claims.
- Claims about the SENDER's own product/metrics do not need target evidence and should NOT appear in the claims array.
Output only valid JSON."""

EMAIL_PROMPT = """You are writing on behalf of SENDER to a recipient at the TARGET company.

SENDER one-liner: {one_liner}
SENDER value proposition: {value_prop}
SENDER differentiators: {differentiators}

RECIPIENT persona: {persona_role} (seniority: {persona_seniority}) at {target_name}.

FIT summary: {fit_summary}
Suggested hooks: {hooks}

TARGET evidence snippets (cite ids in claims):
{snippets}

TARGET signals (cite urls in claims):
{signals}

Write TWO cold emails with MEANINGFULLY DIFFERENT angles:
  - Email A = PAIN-LED: open on a specific, evidenced pain/problem the recipient's role likely owns; connect to the sender's outcome.
  - Email B = TRIGGER-LED: open on a specific recent trigger/signal (funding, hiring, launch, expansion); connect to timing-driven value.

Constraints:
  - <= 120 words body each. One clear CTA (a soft ask, e.g. a question or 15-min call).
  - Subject line <= 7 words, specific, no clickbait.
  - Tailor tone/altitude to seniority (exec = strategic & brief; manager/IC = concrete & operational).
  - Every factual claim about the target MUST map to an evidence id or signal url.
  - No placeholders like [Company] or [First Name]. Use the real target name; greet with "Hi there,".
  - Populate the "claims" array for EVERY factual statement about {target_name}, each mapped to a real evidence id or signal url from the lists above.

Return ONLY JSON:
{{
  "emails": [
    {{
      "angle": "pain-led",
      "subject": string,
      "body": string,
      "claims": [ {{"claim": string, "evidence": id_or_url}} ]   // every factual claim about target
    }},
    {{
      "angle": "trigger-led",
      "subject": string,
      "body": string,
      "claims": [ {{"claim": string, "evidence": id_or_url}} ]
    }}
  ]
}}"""


async def evaluate_target(sender_profile: dict, target_url: str,
                          persona_role: str, persona_seniority: str) -> dict:
    store, meta = await crawl_company_site(target_url, max_pages=6)
    if meta.get("error"):
        return {"ok": False, "error": meta["error"], "meta": meta}

    target_name = meta.get("company_name") or company_name_guess(target_url)
    domain = meta["domain"]

    # 1) External signals via web search (cheap model + tool).
    signals, _raw = research_target_signals(target_name, domain)

    # Build a combined evidence map: page snippets (ids) + signals (urls as ids).
    selected = _select_sender_snippets(store, cap=22)
    snippet_block = _format_snippets(selected)
    signals_block = "\n".join(
        f"[{s['url']}] {s.get('finding','')} ({s.get('title','')}, {s.get('date_hint','')})"
        for s in signals
    ) or "(no external signals found)"

    icp = sender_profile.get("icp", {})

    # 2) Fit evaluation (strong model).
    fit_text, _ = _complete(
        STRONG_MODEL, FIT_SYSTEM,
        FIT_PROMPT.format(
            value_prop=sender_profile.get("value_proposition", ""),
            icp_json=json.dumps(icp, ensure_ascii=False),
            target_name=target_name, target_domain=domain,
            snippets=snippet_block, signals=signals_block,
        ),
        max_tokens=1600,
    )
    fit = _extract_json(fit_text)
    if not isinstance(fit, dict):
        fit = {}

    # 3) Email drafting (strong model).
    email_text, _ = _complete(
        STRONG_MODEL, EMAIL_SYSTEM,
        EMAIL_PROMPT.format(
            one_liner=sender_profile.get("one_liner", ""),
            value_prop=sender_profile.get("value_proposition", ""),
            differentiators=json.dumps([d.get("point") for d in sender_profile.get("differentiators", [])]),
            persona_role=persona_role, persona_seniority=persona_seniority,
            target_name=target_name,
            fit_summary=fit.get("summary", ""),
            hooks=json.dumps(fit.get("best_angle_hooks", [])),
            snippets=snippet_block, signals=signals_block,
        ),
        max_tokens=1800,
    )
    emails_obj = _extract_json(email_text)
    if isinstance(emails_obj, dict):
        emails = emails_obj.get("emails", [])
    elif isinstance(emails_obj, list):
        emails = emails_obj
    else:
        emails = []
    # ensure each email has a claims list
    for em in emails:
        if isinstance(em, dict) and not isinstance(em.get("claims"), list):
            em["claims"] = []

    # Build the claim map / evidence panel: resolve every cited id/url to source.
    evidence = {s.id: s.to_public() for s in selected}
    for s in signals:
        evidence[s["url"]] = {
            "id": s["url"], "url": s["url"], "title": s.get("title", s["url"]),
            "snippet": s.get("finding", ""), "source_type": "search", "page_kind": "signal",
        }

    claim_map = _build_claim_map(emails, evidence)

    return {
        "ok": True,
        "target_name": target_name,
        "meta": meta,
        "signals": signals,
        "fit": fit,
        "emails": emails,
        "evidence": evidence,
        "claim_map": claim_map,
        "snippet_count": len(store.snippets),
    }


def _build_claim_map(emails: list, evidence: dict) -> list:
    """Flatten all claims across emails with resolved citation source."""
    rows = []
    for em in emails:
        for c in em.get("claims", []) or []:
            ev_id = c.get("evidence")
            src = evidence.get(ev_id)
            rows.append({
                "angle": em.get("angle"),
                "claim": c.get("claim"),
                "evidence_id": ev_id,
                "url": (src or {}).get("url"),
                "snippet": (src or {}).get("snippet"),
                "title": (src or {}).get("title"),
                "resolved": src is not None,
            })
    return rows
