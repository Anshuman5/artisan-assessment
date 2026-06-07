import { useState } from 'react';

export function Logo({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-label="OutboundIQ logo">
      <rect x="2" y="2" width="28" height="28" rx="8" stroke="url(#g)" strokeWidth="2" />
      {/* radar sweep: concentric arcs + an outbound arrow node */}
      <path d="M8 22a10 10 0 0 1 10-10" stroke="url(#g)" strokeWidth="2" strokeLinecap="round" opacity="0.55" />
      <path d="M8 22a6 6 0 0 1 6-6" stroke="url(#g)" strokeWidth="2" strokeLinecap="round" opacity="0.85" />
      <path d="M8 22l9-9" stroke="url(#g)" strokeWidth="2.2" strokeLinecap="round" />
      <path d="M17 13h5v5" stroke="url(#g)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="8" cy="22" r="2" fill="#7c6cff" />
      <defs>
        <linearGradient id="g" x1="2" y1="2" x2="30" y2="30" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7c6cff" />
          <stop offset="1" stopColor="#4cc9f0" />
        </linearGradient>
      </defs>
    </svg>
  );
}

export function ScoreRing({ score = 0, band = '', size = 96 }) {
  const r = (size - 12) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, score));
  const off = c - (pct / 100) * c;
  const color = pct >= 67 ? 'var(--good)' : pct >= 40 ? 'var(--warn)' : 'var(--bad)';
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="var(--border)" strokeWidth="7" fill="none" />
        <circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth="7" fill="none"
          strokeLinecap="round" strokeDasharray={c} strokeDashoffset={off}
          style={{ transition: 'stroke-dashoffset 1s cubic-bezier(.22,1,.36,1)' }} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-bold mono" style={{ color }}>{pct}</span>
        <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{band}</span>
      </div>
    </div>
  );
}

export function Bar({ label, score }) {
  const color = score >= 67 ? 'var(--good)' : score >= 40 ? 'var(--warn)' : 'var(--bad)';
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span style={{ color: 'var(--text-dim)' }}>{label}</span>
        <span className="mono font-medium" style={{ color }}>{score}</span>
      </div>
      <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--surface-2)' }}>
        <div className="h-full rounded-full" style={{ width: `${score}%`, background: color, transition: 'width .9s cubic-bezier(.22,1,.36,1)' }} />
      </div>
    </div>
  );
}

export function EvidencePill({ id, onClick }) {
  if (!id) return null;
  const isUrl = String(id).startsWith('http');
  const label = isUrl ? '↗ source' : id;
  return (
    <span className="evidence-pill inline-flex items-center gap-1" onClick={() => onClick?.(id)}
      title={isUrl ? id : `Evidence ${id}`}>{label}</span>
  );
}

export function Section({ title, sub, children, right }) {
  return (
    <div className="card p-5 fade-in">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-sm font-semibold tracking-wide uppercase" style={{ color: 'var(--text-dim)', letterSpacing: '.06em' }}>{title}</h3>
          {sub && <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>{sub}</p>}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

export function CopyButton({ text, label = 'Copy' }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn-ghost text-xs px-2.5 py-1" data-testid="button-copy"
      onClick={() => {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch {}
        document.body.removeChild(ta);
        setDone(true); setTimeout(() => setDone(false), 1400);
      }}>
      {done ? '✓ Copied' : label}
    </button>
  );
}

export function Spinner({ label }) {
  return (
    <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--text-dim)' }}>
      <svg className="animate-spin" width="16" height="16" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="9" stroke="var(--border)" strokeWidth="3" />
        <path d="M21 12a9 9 0 0 0-9-9" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" />
      </svg>
      {label}
    </div>
  );
}
