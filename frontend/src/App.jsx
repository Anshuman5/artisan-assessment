import { useState, useEffect, useRef } from 'react';
import { api } from './api';
import { Logo, ScoreRing, Bar, EvidencePill, Section, CopyButton, Spinner } from './components';

const PERSONA_SENIORITY = ['C-Suite / Executive', 'VP', 'Director', 'Manager', 'Individual Contributor'];

export default function App() {
  const [tab, setTab] = useState('sender');
  const [senders, setSenders] = useState([]);
  const [activeSender, setActiveSender] = useState(null); // full sender object
  const [evidenceFocus, setEvidenceFocus] = useState(null); // {map, id}

  const refresh = () => api.listSenders().then((d) => setSenders(d.senders || [])).catch(() => {});
  useEffect(() => { refresh(); }, []);

  return (
    <div className="min-h-screen flex flex-col">
      <Header tab={tab} setTab={setTab} hasSender={!!activeSender} />
      <main className="flex-1 max-w-6xl w-full mx-auto px-5 py-7">
        {tab === 'sender' && (
          <SenderMode
            senders={senders}
            onSaved={(s) => { refresh(); setActiveSender(s); }}
            activeSender={activeSender}
            setActiveSender={setActiveSender}
            goTargets={() => setTab('target')}
            onEvidence={setEvidenceFocus}
          />
        )}
        {tab === 'target' && (
          <TargetMode
            senders={senders}
            activeSender={activeSender}
            setActiveSender={setActiveSender}
            onEvidence={setEvidenceFocus}
          />
        )}
      </main>
      {evidenceFocus && <EvidenceDrawer focus={evidenceFocus} onClose={() => setEvidenceFocus(null)} />}
      <footer className="text-center py-5 text-xs" style={{ color: 'var(--text-faint)' }}>
        Every claim is grounded in retrieved snippets · OutboundIQ
      </footer>
    </div>
  );
}

function Header({ tab, setTab, hasSender }) {
  return (
    <header className="sticky top-0 z-20 border-b backdrop-blur" style={{ borderColor: 'var(--border)', background: 'rgba(255,255,255,.8)' }}>
      <div className="max-w-6xl mx-auto px-5 h-16 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Logo />
          <div>
            <div className="font-bold text-[15px] leading-none">OutboundIQ</div>
            <div className="text-[11px] leading-none mt-1" style={{ color: 'var(--text-faint)' }}>public data → outbound strategy</div>
          </div>
        </div>
        <nav className="flex items-center gap-1 p-1 rounded-xl" style={{ background: 'var(--surface)', border: '1px solid var(--border)' }}>
          <TabBtn active={tab === 'sender'} onClick={() => setTab('sender')} n="1" label="ICP & Value Prop" />
          <TabBtn active={tab === 'target'} onClick={() => setTab('target')} n="2" label="Evaluate & Draft" dim={!hasSender} />
        </nav>
      </div>
    </header>
  );
}

function TabBtn({ active, onClick, n, label, dim }) {
  return (
    <button onClick={onClick} data-testid={`tab-${n}`}
      className="px-3.5 py-1.5 rounded-lg text-sm font-medium flex items-center gap-2 transition-all"
      style={active
        ? { background: 'var(--accent)', color: '#ffffff', border: '1px solid var(--accent)' }
        : { color: dim ? 'var(--text-faint)' : 'var(--text-dim)', border: '1px solid transparent' }}>
      <span className="mono text-[11px] opacity-70">{n}</span>{label}
    </button>
  );
}

/* ============================ MODE 1 ============================ */

const SENDER_STEPS = ['Fetching public pages', 'Extracting snippets', 'Inferring value proposition', 'Synthesizing ICP'];

function SenderMode({ senders, onSaved, activeSender, setActiveSender, goTargets, onEvidence }) {
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [step, setStep] = useState(0);
  const timer = useRef(null);

  const run = async () => {
    if (!url.trim()) return;
    setLoading(true); setErr(''); setStep(0);
    timer.current = setInterval(() => setStep((s) => Math.min(s + 1, SENDER_STEPS.length - 1)), 4500);
    try {
      const res = await api.analyzeSender(url.trim());
      setActiveSender(res);
      onSaved(res);
    } catch (e) { setErr(e.message); }
    finally { clearInterval(timer.current); setLoading(false); }
  };

  const loadExisting = async (id) => {
    setErr('');
    try { const s = await api.getSender(id); setActiveSender(s); } catch (e) { setErr(e.message); }
  };

  return (
    <div className="grid gap-6">
      <div className="card p-6 gridlines fade-in">
        <h1 className="text-xl font-bold mb-1">Infer a company's ICP & value proposition</h1>
        <p className="text-sm mb-5" style={{ color: 'var(--text-dim)' }}>
          Enter a sender company's website. The agent fetches its public pages, extracts evidence snippets, and infers a grounded value prop and ICP.
        </p>
        <div className="flex flex-col sm:flex-row gap-2">
          <input className="field flex-1 px-3.5 py-2.5 text-sm" placeholder="artisan.co" data-testid="input-sender-url"
            value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && run()} disabled={loading} />
          <button className="btn-primary px-5 py-2.5 text-sm whitespace-nowrap" onClick={run} disabled={loading} data-testid="button-analyze-sender">
            {loading ? 'Analyzing…' : 'Analyze company →'}
          </button>
        </div>
        {senders.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Saved:</span>
            {senders.slice(0, 8).map((s) => (
              <button key={s.id} className="chip px-2.5 py-1 hover:opacity-80" data-testid={`chip-sender-${s.id}`}
                onClick={() => loadExisting(s.id)}>{s.company_name || s.domain}</button>
            ))}
          </div>
        )}
        {err && <p className="text-sm mt-3" style={{ color: 'var(--bad)' }}>⚠ {err}</p>}
        {loading && <AgentProgress steps={SENDER_STEPS} step={step} />}
      </div>

      {activeSender?.profile && !loading && (
        <SenderResult data={activeSender} goTargets={goTargets} onEvidence={onEvidence} />
      )}
    </div>
  );
}

function AgentProgress({ steps, step }) {
  return (
    <div className="mt-5 grid gap-2 stagger">
      {steps.map((s, i) => (
        <div key={i} className="flex items-center gap-3 text-sm">
          <span className="w-5 h-5 rounded-full flex items-center justify-center text-[11px] mono"
            style={i < step ? { background: 'var(--good)', color: '#ffffff' }
              : i === step ? { background: 'var(--accent-soft)', color: 'var(--accent)', border: '1px solid var(--accent)' }
                : { background: 'var(--surface-2)', color: 'var(--text-faint)', border: '1px solid var(--border)' }}>
            {i < step ? '✓' : i + 1}
          </span>
          <span style={{ color: i <= step ? 'var(--text)' : 'var(--text-faint)' }}>{s}</span>
          {i === step && <span className="ml-auto"><Spinner label="" /></span>}
        </div>
      ))}
    </div>
  );
}

function SenderResult({ data, goTargets, onEvidence }) {
  const p = data.profile;
  const ev = data.evidence || {};
  const icp = p.icp || {};
  const pill = (id) => <EvidencePill key={id} id={id} onClick={(x) => onEvidence({ map: ev, id: x })} />;
  const confColor = { high: 'var(--good)', medium: 'var(--warn)', low: 'var(--bad)' }[p.confidence] || 'var(--text-dim)';

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-2xl font-bold">{p.company_name}</h2>
            <span className="chip px-2 py-0.5" style={{ color: confColor, borderColor: confColor + '55' }}>
              {p.confidence} confidence
            </span>
          </div>
          <p className="text-sm mt-1" style={{ color: 'var(--text-dim)' }}>{data.meta?.domain} · {data.snippet_count} snippets · {p.category}</p>
        </div>
        <button className="btn-primary px-4 py-2 text-sm" onClick={goTargets} data-testid="button-go-targets">
          Evaluate a target with this ICP →
        </button>
      </div>

      <div className="grid md:grid-cols-5 gap-5">
        <div className="md:col-span-3 grid gap-5">
          <Section title="Value Proposition">
            <p className="text-base leading-relaxed mb-2" style={{ color: 'var(--text)' }}>{p.value_proposition}</p>
            <p className="text-sm italic mb-2" style={{ color: 'var(--text-dim)' }}>“{p.one_liner}”</p>
            <div className="flex flex-wrap gap-1.5">{(p.value_prop_evidence || []).map(pill)}</div>
          </Section>

          <Section title="Differentiators">
            <ul className="grid gap-2.5">
              {(p.differentiators || []).map((d, i) => (
                <li key={i} className="flex items-start gap-2.5 text-sm">
                  <span style={{ color: 'var(--accent)' }} className="mt-0.5">◆</span>
                  <span className="flex-1">{d.point} <span className="inline-flex gap-1 ml-1 align-middle">{(d.evidence || []).map(pill)}</span></span>
                </li>
              ))}
            </ul>
          </Section>
        </div>

        <div className="md:col-span-2">
          <Section title="Ideal Customer Profile" sub="Structured & grounded">
            <Field label="Target industries"><Tags items={icp.target_industries} /></Field>
            <Field label="Company size bands"><Tags items={icp.company_size_bands} mono /></Field>
            <Field label="Buyer personas">
              <ul className="grid gap-1.5 mt-1">
                {(icp.buyer_personas || []).map((b, i) => (
                  <li key={i} className="text-sm">
                    <span className="font-medium">{b.role}</span>
                    <span style={{ color: 'var(--text-faint)' }}> — {b.why}</span>
                  </li>
                ))}
              </ul>
            </Field>
            <Field label="Common triggers"><Bullets items={icp.common_triggers} /></Field>
            <Field label="Pain points"><Bullets items={icp.pain_points} /></Field>
            <div className="flex flex-wrap gap-1.5 mt-1">{(icp.icp_evidence || []).map(pill)}</div>
          </Section>
        </div>
      </div>

      {p.notes && (
        <div className="card p-4 text-sm fade-in" style={{ color: 'var(--text-dim)' }}>
          <span className="font-medium" style={{ color: 'var(--text)' }}>Analyst notes & gaps: </span>{p.notes}
        </div>
      )}

      <UsageFooter usage={data.usage} snippetCount={data.snippet_count} />
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div className="mb-3.5 last:mb-0">
      <div className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)', letterSpacing: '.07em' }}>{label}</div>
      {children}
    </div>
  );
}
function Tags({ items = [], mono }) {
  return <div className="flex flex-wrap gap-1.5">{(items || []).map((t, i) => (
    <span key={i} className={`chip px-2 py-0.5 ${mono ? 'mono text-[11px]' : ''}`}>{t}</span>
  ))}</div>;
}
function Bullets({ items = [] }) {
  return <ul className="grid gap-1 mt-0.5">{(items || []).map((t, i) => (
    <li key={i} className="text-sm flex gap-2" style={{ color: 'var(--text-dim)' }}>
      <span style={{ color: 'var(--accent-2)' }}>·</span>{t}</li>
  ))}</ul>;
}

/* ============================ MODE 2 ============================ */

const TARGET_STEPS = ['Fetching target site', 'Extracting snippets', 'Web-searching live signals', 'Scoring ICP fit', 'Drafting outbound emails'];

function TargetMode({ senders, activeSender, setActiveSender, onEvidence }) {
  const [senderId, setSenderId] = useState(activeSender?.id || '');
  const [targetUrl, setTargetUrl] = useState('');
  const [role, setRole] = useState('');
  const [seniority, setSeniority] = useState(PERSONA_SENIORITY[1]);
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState(0);
  const [err, setErr] = useState('');
  const [result, setResult] = useState(null);
  const timer = useRef(null);

  useEffect(() => { if (activeSender?.id) setSenderId(activeSender.id); }, [activeSender]);

  const run = async () => {
    if (!senderId || !targetUrl.trim() || !role.trim()) {
      setErr('Pick a sender ICP, a target URL, and a persona role.'); return;
    }
    setLoading(true); setErr(''); setStep(0); setResult(null);
    timer.current = setInterval(() => setStep((s) => Math.min(s + 1, TARGET_STEPS.length - 1)), 5000);
    try {
      const res = await api.evaluateTarget({ sender_id: senderId, target_url: targetUrl.trim(), persona_role: role.trim(), persona_seniority: seniority });
      setResult(res);
    } catch (e) { setErr(e.message); }
    finally { clearInterval(timer.current); setLoading(false); }
  };

  const currentSender = senders.find((s) => s.id === senderId);

  return (
    <div className="grid gap-6">
      <div className="card p-6 gridlines fade-in">
        <h1 className="text-xl font-bold mb-1">Evaluate a target & draft outbound</h1>
        <p className="text-sm mb-5" style={{ color: 'var(--text-dim)' }}>
          The agent researches the target with live web sources, scores fit against the sender's ICP, and writes two evidence-backed emails.
        </p>
        <div className="grid md:grid-cols-2 gap-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1.5 block" style={{ color: 'var(--text-faint)' }}>Sender ICP</label>
            <select className="field w-full px-3 py-2.5 text-sm" value={senderId} data-testid="select-sender"
              onChange={(e) => setSenderId(e.target.value)} disabled={loading}>
              <option value="">Select a sender profile…</option>
              {senders.map((s) => <option key={s.id} value={s.id}>{s.company_name || s.domain}</option>)}
            </select>
            {currentSender?.one_liner && <p className="text-xs mt-1.5" style={{ color: 'var(--text-faint)' }}>{currentSender.one_liner}</p>}
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1.5 block" style={{ color: 'var(--text-faint)' }}>Target company URL</label>
            <input className="field w-full px-3 py-2.5 text-sm" placeholder="gusto.com" value={targetUrl} data-testid="input-target-url"
              onChange={(e) => setTargetUrl(e.target.value)} disabled={loading} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1.5 block" style={{ color: 'var(--text-faint)' }}>Recipient role</label>
            <input className="field w-full px-3 py-2.5 text-sm" placeholder="Head of Sales Development" value={role} data-testid="input-role"
              onChange={(e) => setRole(e.target.value)} disabled={loading} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1.5 block" style={{ color: 'var(--text-faint)' }}>Seniority</label>
            <select className="field w-full px-3 py-2.5 text-sm" value={seniority} data-testid="select-seniority"
              onChange={(e) => setSeniority(e.target.value)} disabled={loading}>
              {PERSONA_SENIORITY.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>
        <button className="btn-primary px-5 py-2.5 text-sm mt-4" onClick={run} disabled={loading} data-testid="button-evaluate">
          {loading ? 'Researching…' : 'Research, score & draft →'}
        </button>
        {err && <p className="text-sm mt-3" style={{ color: 'var(--bad)' }}>⚠ {err}</p>}
        {loading && <AgentProgress steps={TARGET_STEPS} step={step} />}
      </div>

      {result && !loading && <TargetResult result={result} onEvidence={onEvidence} />}
    </div>
  );
}

function TargetResult({ result, onEvidence }) {
  const fit = result.fit || {};
  const ev = result.evidence || {};
  const pill = (id) => <EvidencePill key={id} id={id} onClick={(x) => onEvidence({ map: ev, id: x })} />;

  return (
    <div className="grid gap-5">
      <div className="grid lg:grid-cols-3 gap-5">
        {/* Fit */}
        <div className="card p-5 fade-in">
          <h3 className="text-sm font-semibold uppercase tracking-wide mb-4" style={{ color: 'var(--text-dim)' }}>ICP Fit — {result.target_name}</h3>
          <div className="flex items-start gap-5 mb-4">
            <div className="shrink-0"><ScoreRing score={fit.fit_score || 0} band={fit.fit_band || ''} /></div>
            <p className="text-sm leading-relaxed" style={{ color: 'var(--text-dim)' }}>{fit.summary}</p>
          </div>
          <div className="grid gap-3">
            {(fit.dimension_scores || []).map((d, i) => <Bar key={i} label={d.dimension} score={d.score} />)}
          </div>
        </div>

        {/* Signals */}
        <div className="card p-5 fade-in lg:col-span-2">
          <h3 className="text-sm font-semibold uppercase tracking-wide mb-3" style={{ color: 'var(--text-dim)' }}>
            Live signals <span className="mono text-xs ml-1" style={{ color: 'var(--text-faint)' }}>({(result.signals || []).length})</span>
          </h3>
          {(result.signals || []).length === 0 && <p className="text-sm" style={{ color: 'var(--text-faint)' }}>No external signals surfaced.</p>}
          <ul className="grid gap-2.5">
            {(result.signals || []).map((s, i) => (
              <li key={i} className="flex items-start gap-3 text-sm">
                <span className="chip mono text-[10px] px-1.5 py-0.5 mt-0.5 whitespace-nowrap">{s.date_hint || '—'}</span>
                <span className="flex-1">{s.finding}{' '}
                  <a href={s.url} target="_blank" rel="noreferrer" className="evidence-pill ml-0.5">↗ {s.title?.slice(0, 28) || 'source'}</a>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* Messaging strategy */}
      {result.strategy && <StrategyPanel strategy={result.strategy} />}

      {/* Emails */}
      <div className="grid md:grid-cols-2 gap-5">
        {(result.emails || []).map((em, i) => <EmailCard key={i} em={em} pill={pill} />)}
      </div>

      {/* Claim map */}
      <ClaimMap rows={result.claim_map || []} onEvidence={(x) => onEvidence({ map: ev, id: x })} />

      <UsageFooter usage={result.usage} snippetCount={result.snippet_count} />
    </div>
  );
}

function EmailCard({ em, pill }) {
  const angleStyle = em.angle === 'pain-led'
    ? { color: '#dc2626', bg: 'rgba(220,38,38,.08)', border: 'rgba(220,38,38,.25)' }
    : { color: '#2563eb', bg: 'rgba(37,99,235,.08)', border: 'rgba(37,99,235,.25)' };
  const full = `Subject: ${em.subject}\n\n${em.body}`;
  return (
    <div className="card p-5 fade-in flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <span className="chip px-2.5 py-1 text-xs font-semibold uppercase tracking-wide"
          style={{ color: angleStyle.color, background: angleStyle.bg, borderColor: angleStyle.border }}>
          {em.angle}
        </span>
        <CopyButton text={full} />
      </div>
      <div className="mb-3 pb-3 border-b" style={{ borderColor: 'var(--border)' }}>
        <div className="text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Subject</div>
        <div className="font-semibold text-sm mt-0.5">{em.subject}</div>
      </div>
      <p className="text-sm leading-relaxed whitespace-pre-wrap flex-1" style={{ color: 'var(--text)' }}>{em.body}</p>
      {(em.claims || []).length > 0 && (
        <div className="mt-4 pt-3 border-t" style={{ borderColor: 'var(--border)' }}>
          <div className="text-[11px] uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>Claims in this email</div>
          <ul className="grid gap-1.5">
            {em.claims.map((c, i) => (
              <li key={i} className="text-xs flex items-start gap-2" style={{ color: 'var(--text-dim)' }}>
                <span className="mt-0.5">{pill(c.evidence)}</span>
                <span className="flex-1">{c.claim}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ClaimMap({ rows, onEvidence }) {
  const resolved = rows.filter((r) => r.resolved).length;
  const statusMeta = {
    supported: { color: 'var(--good)', label: '✓ verified' },
    partial: { color: 'var(--warn)', label: '~ partial' },
    unsupported: { color: 'var(--bad)', label: '⚠ unsupported' },
  };
  return (
    <div className="card p-5 fade-in">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide" style={{ color: 'var(--text-dim)' }}>Claim map / evidence panel</h3>
          <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>Every factual claim is verified against its cited snippet before it ships.</p>
        </div>
        <span className="chip px-2.5 py-1 text-xs mono"
          style={{ color: resolved === rows.length && rows.length ? 'var(--good)' : 'var(--warn)' }}>
          {resolved}/{rows.length} grounded
        </span>
      </div>
      {rows.length === 0 && <p className="text-sm" style={{ color: 'var(--text-faint)' }}>No claims recorded.</p>}
      <div className="grid gap-2">
        {rows.map((r, i) => {
          const sm = statusMeta[r.status] || (r.resolved ? statusMeta.supported : statusMeta.unsupported);
          return (
            <div key={i} className="grid grid-cols-[80px_1fr_auto] gap-3 items-start py-2 border-b last:border-0 text-sm"
              style={{ borderColor: 'var(--border-soft)' }}>
              <span className="chip text-[10px] px-1.5 py-0.5 uppercase tracking-wide text-center"
                style={r.angle === 'pain-led' ? { color: '#dc2626' } : { color: '#2563eb' }}>{r.angle}</span>
              <span className="flex-1">{r.claim}</span>
              <div className="text-right flex flex-col items-end gap-1">
                <span className="text-[11px] mono" style={{ color: sm.color }}>{sm.label}</span>
                {r.resolved && (r.url
                  ? <a href={r.url} target="_blank" rel="noreferrer" className="evidence-pill">↗ source</a>
                  : <span className="evidence-pill" onClick={() => onEvidence(r.evidence_id)}>{r.evidence_id}</span>)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StrategyPanel({ strategy }) {
  const allowed = strategy.claims_allowed || [];
  const notAllowed = strategy.claims_not_allowed || [];
  return (
    <div className="card p-5 fade-in">
      <h3 className="text-sm font-semibold uppercase tracking-wide mb-3" style={{ color: 'var(--text-dim)' }}>
        Messaging strategy <span className="text-xs font-normal" style={{ color: 'var(--text-faint)' }}>· angles + approved claims gate the drafter</span>
      </h3>
      <div className="grid md:grid-cols-2 gap-4 text-sm">
        <div>
          <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Pain-led angle</div>
          <p style={{ color: 'var(--text-dim)' }}>{strategy.pain_led_angle}</p>
          <div className="text-[11px] uppercase tracking-wider mt-3 mb-1" style={{ color: 'var(--text-faint)' }}>Trigger-led angle</div>
          <p style={{ color: 'var(--text-dim)' }}>{strategy.trigger_led_angle}</p>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--good)' }}>Allowed claims ({allowed.length})</div>
          <ul className="grid gap-1 mb-3">
            {allowed.slice(0, 6).map((c, i) => (
              <li key={i} className="text-xs flex gap-1.5" style={{ color: 'var(--text-dim)' }}>
                <span style={{ color: 'var(--good)' }}>✓</span>{typeof c === 'string' ? c : c.claim}</li>
            ))}
          </ul>
          {notAllowed.length > 0 && <>
            <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--bad)' }}>Off-limits ({notAllowed.length})</div>
            <ul className="grid gap-1">
              {notAllowed.slice(0, 4).map((c, i) => (
                <li key={i} className="text-xs flex gap-1.5" style={{ color: 'var(--text-faint)' }}>
                  <span style={{ color: 'var(--bad)' }}>✕</span>{typeof c === 'string' ? c : c.claim}</li>
              ))}
            </ul>
          </>}
        </div>
      </div>
    </div>
  );
}

function UsageFooter({ usage, snippetCount }) {
  if (!usage) return null;
  const t = usage.total_tokens || 0;
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px] mono" style={{ color: 'var(--text-faint)' }}>
      <span className="chip px-2 py-0.5">{t.toLocaleString()} tokens</span>
      <span className="chip px-2 py-0.5">{usage.input_tokens?.toLocaleString()} in · {usage.output_tokens?.toLocaleString()} out</span>
      <span className="chip px-2 py-0.5">{usage.calls} model calls</span>
      {snippetCount != null && <span className="chip px-2 py-0.5">{snippetCount} snippets retrieved</span>}
      <span style={{ color: 'var(--text-faint)' }}>· grounded on retrieved snippets, not full-page stuffing</span>
    </div>
  );
}

/* ============================ EVIDENCE DRAWER ============================ */

function EvidenceDrawer({ focus, onClose }) {
  const src = focus.map?.[focus.id];
  return (
    <div className="fixed inset-0 z-40 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0" style={{ background: 'rgba(0,0,0,.5)' }} />
      <div className="relative w-full max-w-md h-full card rounded-none border-l p-6 overflow-y-auto"
        style={{ animation: 'fadeIn .25s ease', background: 'var(--surface)' }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold">Evidence</h3>
          <button className="btn-ghost px-2.5 py-1 text-sm" onClick={onClose} data-testid="button-close-evidence">✕</button>
        </div>
        {!src ? <p className="text-sm" style={{ color: 'var(--text-faint)' }}>Source not found for {focus.id}.</p> : (
          <div className="grid gap-3">
            <div className="flex items-center gap-2">
              <span className="chip mono text-[11px] px-2 py-0.5">{src.id}</span>
              <span className="chip text-[11px] px-2 py-0.5 capitalize">{src.page_kind}</span>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Title</div>
              <div className="text-sm font-medium">{src.title}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Snippet</div>
              <p className="text-sm leading-relaxed" style={{ color: 'var(--text-dim)' }}>“{src.snippet}”</p>
            </div>
            <a href={src.url} target="_blank" rel="noreferrer" className="btn-ghost px-3 py-2 text-sm text-center break-all" data-testid="link-source">
              ↗ {src.url}
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
