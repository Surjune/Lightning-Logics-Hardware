"""Single-file HTML report (plots embedded) from a results dict."""

from __future__ import annotations

import base64
import html
from pathlib import Path

VERDICT_CLASS = {"PASS": "pass", "FAIL": "fail", "INCONCLUSIVE": "warn", "SKIPPED": "skip", "ERROR": "fail",
                 "INFO": "info", "EXPECTED-FAIL": "info", "MISSING": "fail"}

KEY_METRICS = {
    "pulse": [("image_dbc", "images", "{:.1f} dBc"), ("freq_r2", "R²", "{:.5f}"),
              ("psl_weighted_db", "PSL (weighted)", "{:.1f} dB"), ("psl_matched_db", "PSL", "{:.1f} dB"),
              ("edge_step_pct", "edge step", "{:.2f} %")],
    "tone": [("thd_db", "THD", "{:.1f} dB"), ("sfdr_dbc", "SFDR", "{:.1f} dBc")],
    "floor": [("floor_dbc", "floor", "{:.1f} dBc")],
    "idle": [("idle_dc_v", "DC", "{:.4f} V")],
    "pri": [("pri_jitter_pp_s", "jitter p-p", "{:.2e} s")],
    "transition": [("latency_s", "latency", "{:.4f} s"), ("n_mixed", "mixed", "{}")],
}

CSS = """
:root{--bg:#fbfbfa;--fg:#1f2933;--muted:#616e7c;--line:#e4e7eb;--card:#fff;
--pass:#047857;--fail:#b91c1c;--warn:#b45309;--info:#1d4ed8;--skip:#6b7280}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e4e7eb;--muted:#9aa5b1;--line:#2a3038;
--card:#181c21;--pass:#34d399;--fail:#f87171;--warn:#fbbf24;--info:#93c5fd;--skip:#9ca3af}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.meta{color:var(--muted);font-size:12.5px}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line)}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;font-variant-numeric:tabular-nums;
vertical-align:top}th{font-weight:600;font-size:12.5px;color:var(--muted)}
.v{font-weight:700;letter-spacing:.02em}.pass{color:var(--pass)}.fail{color:var(--fail)}
.warn{color:var(--warn)}.info{color:var(--info)}.skip{color:var(--skip)}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:14px 0}
img{max-width:100%;height:auto;border-radius:4px;margin-top:10px;background:#fff}
.wrap{overflow-x:auto}
"""


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value[:8]) + (" …" if len(value) > 8 else "")
    return html.escape(str(value))


def _badge(verdict: str) -> str:
    return f'<span class="v {VERDICT_CLASS.get(verdict, "")}">{html.escape(verdict)}</span>'


def _key_metrics(t: dict) -> str:
    parts = []
    for key, label, fmt in KEY_METRICS.get(t.get("kind"), []):
        v = t.get("metrics", {}).get(key)
        if v is not None:
            parts.append(f"{label} {fmt.format(v)}")
    return html.escape(" · ".join(parts))


def render_html(results: dict, base_dir: Path | None = None) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(t['name'])}</td><td>{html.escape(t.get('description', ''))}</td>"
        f"<td>{_badge(t['verdict'])}</td><td>{_key_metrics(t)}</td></tr>" for t in results["tests"])
    sections = []
    for t in results["tests"]:
        crit = "".join(
            f"<tr><td>{html.escape(c['metric'])}</td><td>{_fmt(c.get('value'))}</td>"
            f"<td>{html.escape(str(c.get('limit', '')))}</td><td>{html.escape(str(c.get('target') or ''))}</td>"
            f"<td>{_badge(c['verdict'])}</td><td>{html.escape(c.get('what', ''))}</td></tr>"
            for c in t.get("criteria", []))
        img = ""
        if t.get("plot") and base_dir is not None:
            p = base_dir / t["plot"]
            if p.exists():
                data = base64.b64encode(p.read_bytes()).decode()
                img = f'<img alt="{html.escape(t["name"])} diagnostics" src="data:image/png;base64,{data}">'
        reason = f'<p class="meta">{html.escape(t["reason"])}</p>' if t.get("reason") else ""
        table = (f'<div class="wrap"><table><tr><th>metric</th><th>value</th><th>limit</th><th>target</th>'
                 f'<th>verdict</th><th>what it checks</th></tr>{crit}</table></div>') if crit else ""
        sections.append(f'<section id="{html.escape(t["name"])}"><h2>{html.escape(t["name"])} '
                        f'{_badge(t["verdict"])}</h2><p class="meta">{html.escape(t.get("description", ""))}</p>'
                        f'{reason}{table}{img}</section>')
    bench = html.escape(", ".join(f"{k}={v}" for k, v in results.get("bench", {}).items()
                                  if not isinstance(v, (list, dict))))
    summary = " · ".join(f"{k} {v}" for k, v in results.get("summary", {}).items())
    ctx = results.get("context", {})
    floor = f" · instrument floor {ctx['floor_dbc']:.1f} dBc" if ctx.get("floor_dbc") is not None else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Transmitter validation report</title><style>{CSS}</style></head><body><main>"
            f"<h1>Transmitter validation {_badge(results['verdict'])}</h1>"
            f"<p class=meta>{html.escape(results.get('toolkit', ''))} · revision "
            f"{html.escape(str(results.get('revision')))} · {html.escape(results.get('timestamp', ''))}"
            f"<br>{bench}{floor}<br>{html.escape(summary)}</p>"
            f"<div class=wrap><table><tr><th>test</th><th>description</th><th>verdict</th><th>key figures</th></tr>"
            f"{rows}</table></div>{''.join(sections)}</main></body></html>")


def write_html(results: dict, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(render_html(results, path.parent), encoding="utf-8")
    return path
