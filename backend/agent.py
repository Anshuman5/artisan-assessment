"""
Agentic orchestration for OutboundIQ.

Two pipelines:
  Mode 1: analyze_sender(url)  -> value prop + structured ICP
  Mode 2: evaluate_target(sender_profile, target_url, persona) -> fit + 2 emails + claim map

Agent graph (Mode 2):
  crawl -> embed/retrieve -> signal extraction (web search)
        -> ICP fit scoring -> messaging strategy -> email drafting
        -> claim verification (entailment) -> constraint check -> corrective redraft
        -> claim map

Token strategy:
  - We never stuff full page text into the model. We crawl, chunk, embed, and
    retrieve only the top snippets relevant to each reasoning facet (industries,
    personas, pains, triggers, persona-fit) via local embeddings (no API key).
  - Each LLM call gets a compact, numbered snippet list and must cite snippet ids,
    which we resolve back to {url, snippet} for the claim map.
  - Cheap model (haiku) for extraction/signal-mining/verification; strong model
    (sonnet) for synthesis and drafting. Every call is metered for token usage.
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

import os
from pathlib import Path

from dotenv import load_dotenv

# Load local environment variables (ANTHROPIC_API_KEY, model overrides, etc.)
# from backend/local.env so the key doesn't have to be exported in the shell.
# Real environment variables (e.g. set on Railway) take precedence.
load_dotenv(Path(__file__).parent / "local.env")

# Model IDs are environment-driven so the same code runs in the sandbox
# (alias names routed through the website proxy) and on a real Anthropic key
# (e.g. on Railway, where you set real Anthropic model IDs as env vars).
CHEAP_MODEL = os.environ.get("CHEAP_MODEL", "claude-haiku-4-5")
STRONG_MODEL = os.environ.get("STRONG_MODEL", "claude-sonnet-4-6")

# Optional custom base URL (sandbox proxy). On Railway leave unset to hit
# Anthropic directly with ANTHROPIC_API_KEY.
_base_url = os.environ.get("ANTHROPIC_BASE_URL")
_client = Anthropic(base_url=_base_url) if _base_url else Anthropic()


# ---------------------------------------------------------------------------
# Token metering
# ---------------------------------------------------------------------------

class TokenMeter:
    """Accumulates token usage across all model calls in a single run."""

    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.by_step: list[dict] = []

    def add(self, usage, step: str, model: str):
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        self.input_tokens += it
        self.output_tokens += ot
        self.calls += 1
        self.by_step.append({"step": step, "model": model,
                             "input_tokens": it, "output_tokens": ot})

    def summary(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "calls": self.calls,
            "by_step": self.by_step,
        }


# ---------------------------------------------------------------------------
# Low-level LLM helpers
# ---------------------------------------------------------------------------

def _complete(model: str, system: str, user: str, max_tokens: int = 1500,
              tools: list | None = None, meter: TokenMeter | None = None,
              step: str = "complete") -> tuple[str, list]:
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
    if meter is not None:
        meter.add(msg.usage, step, model)
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


def _complete_json(model: str, system: str, user: str, schema: dict,
                   max_tokens: int = 1600, meter: TokenMeter | None = None,
                   step: str = "json") -> dict:
    """Force a structured JSON result via tool-use, so we never silently fail on
    malformed output. Falls back to best-effort text parsing if the model returns
    text instead of a tool call."""
    tool = {"name": "emit_result",
            "description": "Return the structured result for this task.",
            "input_schema": schema}
    msg = _client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool], tool_choice={"type": "tool", "name": "emit_result"},
    )
    if meter is not None:
        meter.add(msg.usage, step, model)
    for block in msg.content:
        if block.type == "tool_use" and block.name == "emit_result":
            return block.input if isinstance(block.input, dict) else {}
    # Fallback: parse any text the model emitted.
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _extract_json(text) or {}


def _strip_cite_tags(text: str) -> str:
    """Web search responses wrap quoted facts in <cite index=...>...</cite>. Unwrap them."""
    text = re.sub(r'<cite[^>]*>', '', text)
    text = text.replace('</cite>', '')
    return text


def _extract_json(text: str, prefer: str = "auto") -> dict | list | None:
    """Best-effort JSON extraction (fallback path). prefer='array' | 'object' | 'auto'."""
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


def _retrieve_facets(store: EvidenceStore, queries: list[str],
                     per_query: int = 4, cap: int = 22) -> list:
    """Embedding retrieval per facet query, unioned and deduped (order preserved).

    This is the core RAG step: instead of dumping pages (or a fixed page-kind
    quota) at the model, we pull only the snippets semantically closest to each
    reasoning facet (industries, personas, pains, triggers, persona-fit, ...)."""
    picked: dict[str, object] = {}
    for q in queries:
        for s in store.semantic_search(q, limit=per_query):
            if s.id not in picked:
                picked[s.id] = s
        if len(picked) >= cap:
            break
    return list(picked.values())[:cap]


WEB_SEARCH_TOOL = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]


# ---------------------------------------------------------------------------
# MODE 1 — Sender: value proposition + ICP
# ---------------------------------------------------------------------------

SENDER_FACET_QUERIES = [
    "what the company does, its product and core value proposition",
    "target industries, verticals and markets the company serves",
    "customer company size: enterprise, mid-market, or SMB",
    "buyer personas, roles and decision makers who purchase this product",
    "customer pain points and the problems this product solves",
    "buying triggers: growth, hiring, funding, scaling, expansion",
    "customers, case studies, testimonials and logos",
]

SENDER_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "one_liner": {"type": "string"},
        "value_proposition": {"type": "string"},
        "value_prop_evidence": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string"},
        "differentiators": {"type": "array", "items": {
            "type": "object", "properties": {
                "point": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            }, "required": ["point"]}},
        "icp": {"type": "object", "properties": {
            "target_industries": {"type": "array", "items": {"type": "string"}},
            "company_size_bands": {"type": "array", "items": {"type": "string"}},
            "buyer_personas": {"type": "array", "items": {
                "type": "object", "properties": {
                    "role": {"type": "string"}, "why": {"type": "string"}},
                "required": ["role"]}},
            "common_triggers": {"type": "array", "items": {"type": "string"}},
            "pain_points": {"type": "array", "items": {"type": "string"}},
            "icp_evidence": {"type": "array", "items": {"type": "string"}},
        }, "required": ["target_industries", "company_size_bands",
                        "buyer_personas", "common_triggers", "pain_points"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "notes": {"type": "string"},
    },
    "required": ["company_name", "one_liner", "value_proposition", "icp", "confidence"],
}

SENDER_SYSTEM = """You are a B2B go-to-market analyst. You infer a company's value proposition and Ideal Customer Profile (ICP) STRICTLY from the evidence snippets provided. Every factual claim must be grounded in a snippet id. Do not invent facts, customers, metrics, or integrations that are not in the snippets. If evidence is thin, reflect that in the confidence field."""

SENDER_PROMPT = """Company: {name} ({domain})

EVIDENCE SNIPPETS (cite these ids in the *_evidence fields):
{snippets}

Infer the value proposition and ICP. Rules:
- Base industries, size bands, personas, triggers, and pains ONLY on signals in the snippets (customers mentioned, language used, pricing tiers, who they sell to). If you must infer, keep it conservative and lower the confidence.
- Keep arrays tight (no filler). Cite snippet ids in every *_evidence field."""


async def analyze_sender(url: str) -> dict:
    meter = TokenMeter()
    store, meta = await crawl_company_site(url, max_pages=7)
    if meta.get("error"):
        return {"ok": False, "error": meta["error"], "meta": meta}

    name = meta.get("company_name") or company_name_guess(url)

    # Facet retrieval: pull the snippets most relevant to each ICP dimension.
    selected = _retrieve_facets(store, SENDER_FACET_QUERIES, per_query=4, cap=24)
    snippet_block = _format_snippets(selected)

    data = _complete_json(
        STRONG_MODEL, SENDER_SYSTEM,
        SENDER_PROMPT.format(name=name, domain=meta["domain"], snippets=snippet_block),
        schema=SENDER_SCHEMA, max_tokens=1800, meter=meter, step="sender_icp",
    )

    evidence = {s.id: s.to_public() for s in selected}
    return {
        "ok": True,
        "profile": data,
        "evidence": evidence,
        "meta": meta,
        "snippet_count": len(store.snippets),
        "usage": meter.summary(),
    }


# ---------------------------------------------------------------------------
# MODE 2 — Target: research, fit, strategy, emails, verification, claim map
# ---------------------------------------------------------------------------

SIGNALS_SYSTEM = """You are a sales researcher. Use the web_search tool to find recent, specific, citable signals about the target company that matter for outbound: funding, growth/hiring, product launches, leadership changes, expansion, partnerships, pain indicators, or strategic initiatives. Prefer the last 18 months. Never fabricate a URL — only cite pages your search actually returned.

After you finish searching, your FINAL message must be ONLY a JSON array (no prose, no markdown, no code fence) of this exact shape:
[ {"finding": "<one specific sentence>", "url": "<real source url>", "title": "<source title>", "date_hint": "<e.g. Nov 2025>"} ]
Return 3-6 items. If you find nothing credible, return []."""

SIGNALS_PROMPT = """Target company: {name} (domain: {domain}).
Search the web for the most relevant recent signals for a B2B seller approaching this company, then return the JSON array described in your instructions. Run 2-4 searches (e.g. "{name} funding", "{name} hiring", "{name} product launch news", "{name} {domain} announcement")."""


def research_target_signals(name: str, domain: str, meter: TokenMeter) -> tuple[list[dict], list[dict]]:
    """Returns (signals, search_urls). Uses web_search tool on the cheap model."""
    text, search_urls = _complete(
        CHEAP_MODEL, SIGNALS_SYSTEM,
        SIGNALS_PROMPT.format(name=name, domain=domain),
        max_tokens=1500, tools=WEB_SEARCH_TOOL, meter=meter, step="signals",
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


FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer"},
        "fit_band": {"type": "string", "enum": ["Strong", "Moderate", "Weak"]},
        "summary": {"type": "string"},
        "dimension_scores": {"type": "array", "items": {
            "type": "object", "properties": {
                "dimension": {"type": "string"},
                "score": {"type": "integer"},
                "rationale": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            }, "required": ["dimension", "score", "rationale"]}},
        "best_angle_hooks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fit_score", "fit_band", "summary", "dimension_scores"],
}

FIT_SYSTEM = """You are a B2B qualification analyst. You score how well a TARGET company fits a SENDER's ICP, using ONLY the evidence snippets and signals provided. Cite snippet ids / signal urls for each rationale. Be honest: a poor fit must score low."""

FIT_PROMPT = """SENDER value proposition: {value_prop}
SENDER ICP:
{icp_json}

TARGET company: {target_name} ({target_domain})
RECIPIENT persona: {persona_role} (seniority: {persona_seniority})

TARGET website evidence snippets (cite ids):
{snippets}

TARGET external signals (cite by url):
{signals}

Score fit on these five dimensions (0-100 each): "Industry", "Company size", "Buyer/persona fit" (does this persona match the ICP buyers?), "Pain match", "Triggers". For each, give a one-line rationale and cite the evidence ids / urls that support it. Then give an overall fit_score, fit_band, a 2-sentence summary, and 2-3 concrete outreach hooks grounded in evidence."""


STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "persona": {"type": "string"},
        "likely_priorities": {"type": "array", "items": {"type": "string"}},
        "pain_led_angle": {"type": "string"},
        "trigger_led_angle": {"type": "string"},
        "claims_allowed": {"type": "array", "items": {
            "type": "object", "properties": {
                "claim": {"type": "string"}, "evidence": {"type": "string"}},
            "required": ["claim", "evidence"]}},
        "claims_not_allowed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["likely_priorities", "pain_led_angle", "trigger_led_angle", "claims_allowed"],
}

STRATEGY_SYSTEM = """You are a B2B messaging strategist. Before any email is written, you decide the angle and — critically — the exact set of factual claims about the TARGET that are ALLOWED, each tied to a specific evidence id or signal url. A claim is allowed ONLY if a provided snippet/signal directly supports it. You also list claims that are tempting but NOT supported, so the drafter avoids them."""

STRATEGY_PROMPT = """SENDER value proposition: {value_prop}
SENDER ICP pains: {pains}
SENDER ICP triggers: {triggers}

TARGET: {target_name}. RECIPIENT persona: {persona_role} ({persona_seniority}).
FIT summary: {fit_summary}

TARGET evidence snippets (cite ids):
{snippets}

TARGET signals (cite urls):
{signals}

Produce the messaging strategy:
- likely_priorities for this persona,
- a pain_led_angle and a meaningfully different trigger_led_angle,
- claims_allowed: every factual claim about {target_name} the drafter MAY use, each with the supporting evidence id or signal url (only include claims a snippet/signal actually supports),
- claims_not_allowed: plausible-sounding claims about {target_name} that the evidence does NOT support (so the drafter avoids inventing them)."""


EMAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "emails": {"type": "array", "items": {
            "type": "object", "properties": {
                "angle": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "claims": {"type": "array", "items": {
                    "type": "object", "properties": {
                        "claim": {"type": "string"}, "evidence": {"type": "string"}},
                    "required": ["claim", "evidence"]}},
            }, "required": ["angle", "subject", "body", "claims"]}},
    },
    "required": ["emails"],
}

EMAIL_SYSTEM = """You are an elite B2B SDR copywriter. You write short, specific, human cold emails that earn replies. You ground EVERY factual claim about the recipient's company in a provided evidence id (snippet) or signal url — no generic flattery, no invented facts.

HARD RULES:
- Use ONLY claims from the provided "allowed claims" list. Never assert anything from the "not allowed" list.
- NEVER use bracketed placeholders like [First Name], [Company], [Name]. Greet with "Hi there," and use the real target company name.
- The "claims" array is MANDATORY: list every sentence in the body that asserts a fact about the TARGET company, each with its supporting evidence id / signal url. If a sentence about the target has no support, do not write it (or phrase it as a general hypothesis with NO factual claim about the target — and then do not list it as a claim).
- Claims about the SENDER's own product/metrics do not need target evidence and must NOT appear in the claims array.
- Body 80-130 words. Subject <= 7 words, specific, no clickbait. One soft CTA. Different subject lines and different opening logic for the two emails."""

EMAIL_PROMPT = """You are writing on behalf of SENDER to a recipient at the TARGET company.

SENDER one-liner: {one_liner}
SENDER value proposition: {value_prop}
SENDER differentiators: {differentiators}

RECIPIENT persona: {persona_role} (seniority: {persona_seniority}) at {target_name}.

MESSAGING STRATEGY:
- persona priorities: {priorities}
- pain-led angle: {pain_angle}
- trigger-led angle: {trigger_angle}

ALLOWED claims about {target_name} (use only these; cite the evidence id/url):
{claims_allowed}

NOT allowed (never assert these):
{claims_not_allowed}

TARGET evidence snippets (for reference, cite ids in claims):
{snippets}
TARGET signals (cite urls in claims):
{signals}

Write TWO cold emails with MEANINGFULLY DIFFERENT angles:
  - Email A = PAIN-LED: open on a specific, evidenced pain the recipient's role likely owns; connect to the sender's outcome.
  - Email B = TRIGGER-LED: open on a specific recent trigger/signal (funding, hiring, launch, expansion); connect to timing-driven value.
Tailor tone to seniority (exec = strategic & brief; manager/IC = concrete & operational).
Populate the "claims" array for EVERY factual statement about {target_name}, each mapped to a real evidence id or signal url from the allowed list."""

EMAIL_REVISION_PROMPT = """Revise the two emails below to fix the listed problems. Keep the same two angles (pain-led, trigger-led) and the allowed-claims discipline.

ALLOWED claims about {target_name} (cite the evidence id/url):
{claims_allowed}

CURRENT EMAILS (JSON):
{current}

PROBLEMS TO FIX:
{problems}

How to fix:
- "unsupported claim": remove that sentence, OR rewrite it as a GENERAL hypothesis that makes no factual claim about {target_name} (e.g. "Teams expanding their GTM motion often need more outbound coverage."). If rewritten as general, drop it from the claims array.
- "body too long/short": adjust to 80-130 words.
- "subject too long": tighten to <= 7 words.
- "bracket placeholder": remove it; greet with "Hi there," and use the real company name.
Return both revised emails with corrected "claims" arrays."""


async def evaluate_target(sender_profile: dict, target_url: str,
                          persona_role: str, persona_seniority: str) -> dict:
    meter = TokenMeter()
    store, meta = await crawl_company_site(target_url, max_pages=6)
    if meta.get("error"):
        return {"ok": False, "error": meta["error"], "meta": meta}

    target_name = meta.get("company_name") or company_name_guess(target_url)
    domain = meta["domain"]
    icp = sender_profile.get("icp", {})

    # 1) External signals via web search (cheap model + tool).
    signals, _raw = research_target_signals(target_name, domain, meter)

    # 2) Facet retrieval driven by the sender's ICP + the persona, so the snippets
    #    we feed downstream are the ones relevant to *this* fit decision.
    facet_queries = [f"{target_name}: what the company does, its product and industry"]
    facet_queries += [f"pain: {p}" for p in (icp.get("pain_points") or [])[:3]]
    facet_queries += [f"trigger: {t}" for t in (icp.get("common_triggers") or [])[:3]]
    facet_queries += [f"industry: {i}" for i in (icp.get("target_industries") or [])[:2]]
    facet_queries.append(f"{persona_role} {persona_seniority} responsibilities and priorities")
    facet_queries.append(f"{target_name} growth, hiring, expansion, enterprise customers")
    selected = _retrieve_facets(store, facet_queries, per_query=3, cap=18)
    snippet_block = _format_snippets(selected)
    signals_block = "\n".join(
        f"[{s['url']}] {s.get('finding','')} ({s.get('title','')}, {s.get('date_hint','')})"
        for s in signals
    ) or "(no external signals found)"

    # Evidence map: page snippets (ids) + signals (urls as ids).
    evidence = {s.id: s.to_public() for s in selected}
    for s in signals:
        evidence[s["url"]] = {
            "id": s["url"], "url": s["url"], "title": s.get("title", s["url"]),
            "snippet": s.get("finding", ""), "source_type": "search", "page_kind": "signal",
        }

    # 3) Fit evaluation (strong model), now including buyer/persona fit.
    fit = _complete_json(
        STRONG_MODEL, FIT_SYSTEM,
        FIT_PROMPT.format(
            value_prop=sender_profile.get("value_proposition", ""),
            icp_json=json.dumps(icp, ensure_ascii=False),
            target_name=target_name, target_domain=domain,
            persona_role=persona_role, persona_seniority=persona_seniority,
            snippets=snippet_block, signals=signals_block,
        ),
        schema=FIT_SCHEMA, max_tokens=1600, meter=meter, step="fit",
    )

    # 4) Messaging strategy (cheap model): angles + allowed/disallowed claims.
    strategy = _complete_json(
        CHEAP_MODEL, STRATEGY_SYSTEM,
        STRATEGY_PROMPT.format(
            value_prop=sender_profile.get("value_proposition", ""),
            pains=json.dumps(icp.get("pain_points", [])),
            triggers=json.dumps(icp.get("common_triggers", [])),
            target_name=target_name, persona_role=persona_role,
            persona_seniority=persona_seniority,
            fit_summary=fit.get("summary", ""),
            snippets=snippet_block, signals=signals_block,
        ),
        schema=STRATEGY_SCHEMA, max_tokens=1400, meter=meter, step="strategy",
    )
    allowed_block = json.dumps(strategy.get("claims_allowed", []), ensure_ascii=False, indent=0)
    not_allowed_block = json.dumps(strategy.get("claims_not_allowed", []), ensure_ascii=False)

    # 5) Email drafting (strong model), constrained to the approved claim set.
    drafted = _complete_json(
        STRONG_MODEL, EMAIL_SYSTEM,
        EMAIL_PROMPT.format(
            one_liner=sender_profile.get("one_liner", ""),
            value_prop=sender_profile.get("value_proposition", ""),
            differentiators=json.dumps([d.get("point") for d in sender_profile.get("differentiators", [])]),
            persona_role=persona_role, persona_seniority=persona_seniority,
            target_name=target_name,
            priorities=json.dumps(strategy.get("likely_priorities", [])),
            pain_angle=strategy.get("pain_led_angle", ""),
            trigger_angle=strategy.get("trigger_led_angle", ""),
            claims_allowed=allowed_block, claims_not_allowed=not_allowed_block,
            snippets=snippet_block, signals=signals_block,
        ),
        schema=EMAILS_SCHEMA, max_tokens=1800, meter=meter, step="draft",
    )
    emails = drafted.get("emails", []) if isinstance(drafted, dict) else []
    for em in emails:
        if isinstance(em, dict) and not isinstance(em.get("claims"), list):
            em["claims"] = []

    # 6) Claim verification (entailment) + constraint check, with one corrective
    #    redraft if anything is unsupported or violates the constraints.
    emails, verification = _verify_and_refine(
        emails, evidence, meter, target_name, allowed_block,
        signals_block, snippet_block,
    )

    claim_map = _build_claim_map(emails, evidence)

    return {
        "ok": True,
        "target_name": target_name,
        "meta": meta,
        "signals": signals,
        "fit": fit,
        "strategy": strategy,
        "emails": emails,
        "evidence": evidence,
        "claim_map": claim_map,
        "verification": verification,
        "snippet_count": len(store.snippets),
        "usage": meter.summary(),
    }


# ---------------------------------------------------------------------------
# Claim verification + constraint enforcement
# ---------------------------------------------------------------------------

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": {
            "type": "object", "properties": {
                "index": {"type": "integer"},
                "status": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
                "reason": {"type": "string"},
            }, "required": ["index", "status"]}},
    },
    "required": ["verdicts"],
}

VERIFY_SYSTEM = """You are a strict claim verifier. For each numbered (claim, evidence) pair, decide whether the EVIDENCE text directly supports the CLAIM:
- "supported": the evidence clearly states or strongly implies the claim.
- "partial": the evidence is related but does not fully establish the claim.
- "unsupported": the evidence does not support the claim, or no evidence was provided.
Judge ONLY from the evidence text shown. Do not use outside knowledge."""


def _word_count(s: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", s or ""))


PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,40}\]")


def _email_constraint_issues(em: dict) -> list[str]:
    """Spec constraints: 80-130 word body, <=7 word subject, no placeholder tokens."""
    issues = []
    body = em.get("body", "") or ""
    wc = _word_count(body)
    if wc < 70:
        issues.append(f"body too short ({wc} words; aim 80-130)")
    elif wc > 140:
        issues.append(f"body too long ({wc} words; aim 80-130)")
    if _word_count(em.get("subject", "")) > 8:
        issues.append("subject too long (aim <= 7 words)")
    if PLACEHOLDER_RE.search(body) or PLACEHOLDER_RE.search(em.get("subject", "") or ""):
        issues.append("contains bracket placeholder")
    return issues


def _annotate_claim_status(emails: list, evidence: dict, meter: TokenMeter) -> None:
    """Verify every target claim against its cited snippet and tag each claim with
    a 'status' (supported/partial/unsupported). Claims whose evidence id can't be
    resolved are unsupported without a model call."""
    flat = []  # (email_idx, claim_idx, claim_obj)
    pairs = []  # for the model: {index, claim, evidence}
    for ei, em in enumerate(emails):
        for ci, c in enumerate(em.get("claims", []) or []):
            src = evidence.get(c.get("evidence"))
            if not src:
                c["status"] = "unsupported"
                continue
            idx = len(flat)
            flat.append((ei, ci, c))
            pairs.append({"index": idx,
                          "claim": c.get("claim", ""),
                          "evidence": src.get("snippet", "")})
    if not pairs:
        return
    payload = "\n\n".join(
        f"[{p['index']}] CLAIM: {p['claim']}\nEVIDENCE: {p['evidence']}" for p in pairs
    )
    out = _complete_json(
        CHEAP_MODEL, VERIFY_SYSTEM,
        "Verify each pair and return a verdict per index.\n\n" + payload,
        schema=VERIFY_SCHEMA, max_tokens=1200, meter=meter, step="verify",
    )
    verdicts = {v.get("index"): v for v in (out.get("verdicts", []) or [])}
    for idx, (_ei, _ci, c) in enumerate(flat):
        v = verdicts.get(idx)
        c["status"] = (v or {}).get("status", "partial")
        if v and v.get("reason"):
            c["verify_reason"] = v["reason"]


def _collect_problems(emails: list) -> list[list[str]]:
    """Per-email list of problems (unsupported claims + constraint violations)."""
    problems = []
    for em in emails:
        probs = list(_email_constraint_issues(em))
        for c in em.get("claims", []) or []:
            if c.get("status") == "unsupported":
                probs.append(f"unsupported claim: \"{c.get('claim','')}\"")
        problems.append(probs)
    return problems


def _verify_and_refine(emails, evidence, meter, target_name, allowed_block,
                       signals_block, snippet_block, max_rounds: int = 1):
    """Verify claims + constraints; if problems remain, do one corrective redraft."""
    _annotate_claim_status(emails, evidence, meter)
    problems = _collect_problems(emails)
    rounds = 0
    while any(problems) and rounds < max_rounds:
        feedback = "\n".join(
            f"Email {i+1} ({emails[i].get('angle','?')}): " + "; ".join(p)
            for i, p in enumerate(problems) if p
        )
        revised = _complete_json(
            STRONG_MODEL, EMAIL_SYSTEM,
            EMAIL_REVISION_PROMPT.format(
                target_name=target_name, claims_allowed=allowed_block,
                current=json.dumps({"emails": emails}, ensure_ascii=False),
                problems=feedback,
            ),
            schema=EMAILS_SCHEMA, max_tokens=1800, meter=meter, step="redraft",
        )
        new_emails = revised.get("emails", []) if isinstance(revised, dict) else []
        if new_emails:
            emails = new_emails
            for em in emails:
                if not isinstance(em.get("claims"), list):
                    em["claims"] = []
        _annotate_claim_status(emails, evidence, meter)
        problems = _collect_problems(emails)
        rounds += 1

    verification = {
        "rounds": rounds,
        "remaining_issues": {f"email_{i+1}": p for i, p in enumerate(problems) if p},
        "claims_total": sum(len(em.get("claims", []) or []) for em in emails),
        "claims_supported": sum(
            1 for em in emails for c in (em.get("claims", []) or [])
            if c.get("status") in ("supported", "partial")
        ),
    }
    return emails, verification


def _build_claim_map(emails: list, evidence: dict) -> list:
    """Flatten all claims across emails with resolved citation + verified status."""
    rows = []
    for em in emails:
        for c in em.get("claims", []) or []:
            ev_id = c.get("evidence")
            src = evidence.get(ev_id)
            status = c.get("status", "partial")
            rows.append({
                "angle": em.get("angle"),
                "claim": c.get("claim"),
                "evidence_id": ev_id,
                "url": (src or {}).get("url"),
                "snippet": (src or {}).get("snippet"),
                "title": (src or {}).get("title"),
                "status": status,
                # 'resolved' kept for the UI: true only when verified as supported.
                "resolved": src is not None and status in ("supported", "partial"),
            })
    return rows
