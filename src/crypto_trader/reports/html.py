"""Self-contained HTML building blocks (no external assets): theme tokens, tables, SVG line charts with hover."""

from __future__ import annotations

import html
import json
import math
from datetime import UTC, datetime

SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--surface-2:#f3f2ef;--text:#0b0b0b;--text-2:#52514e;--muted:#8a8984;
--grid:#e4e3df;--border:#d9d8d3;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--pos:#2a78d6;--neg:#e34948;--mid:#f0efec;
%LIGHT%}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--surface:#1a1a19;
--surface-2:#242422;--text:#f0efec;--text-2:#c3c2b7;--muted:#8f8e86;--grid:#33332f;--border:#3a3a36;--pos:#3987e5;
--neg:#e66767;--mid:#383835;%DARK%}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--surface-2:#242422;--text:#f0efec;--text-2:#c3c2b7;
--muted:#8f8e86;--grid:#33332f;--border:#3a3a36;--pos:#3987e5;--neg:#e66767;--mid:#383835;%DARK%}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 8px;padding-top:8px;border-top:1px solid var(--border)}h3{font-size:15px;margin:20px 0 6px}
.sub{color:var(--text-2)}.muted{color:var(--muted)}.note{background:var(--surface-2);border-left:3px solid var(--border);
padding:8px 12px;margin:8px 0;color:var(--text-2)}
.tbl{overflow-x:auto;margin:8px 0}table{border-collapse:collapse;font-size:12.5px;width:100%}
th,td{padding:4px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--surface-2);color:var(--text-2);font-weight:600}
td:first-child,th:first-child,td.l,th.l{text-align:left}
.pos{color:var(--pos)}.neg{color:var(--neg)}.badge{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;
border:1px solid var(--border)}.b-ok::before{content:"✓ ";color:var(--good)}.b-bad::before{content:"✕ ";color:var(--crit)}
.b-warn::before{content:"! ";color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin:10px 0}
.tile{background:var(--surface-2);border-radius:8px;padding:10px 12px}.tile .v{font-size:20px;font-weight:600}
.tile .k{color:var(--text-2);font-size:12px}
.chart{position:relative;margin:8px 0 16px}.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--text-2);margin:4px 0}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:6px;
padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:5;white-space:nowrap}
details{margin:6px 0}summary{cursor:pointer;color:var(--text-2)}
"""

JS = """
document.querySelectorAll('.chart[data-series]').forEach(function(box){
 var d=JSON.parse(box.dataset.series),svg=box.querySelector('svg'),tip=box.querySelector('.tip'),
 hair=svg.querySelector('.hair'),W=+svg.dataset.w,L=+svg.dataset.l,R=+svg.dataset.r,x0=+svg.dataset.x0,x1=+svg.dataset.x1,
 fmt=box.dataset.fmt;
 function f(v){return v==null?'-':(fmt==='pct'?(v*100).toFixed(1)+'%':Math.round(v).toLocaleString())}
 svg.addEventListener('mousemove',function(e){var b=svg.getBoundingClientRect(),px=(e.clientX-b.left)*W/b.width;
  if(px<L||px>W-R){tip.style.display='none';hair.setAttribute('opacity',0);return}
  var t=x0+(px-L)/(W-L-R)*(x1-x0),rows=[];
  d.forEach(function(s){var a=s.p,lo=0,hi=a.length-1;if(!a.length)return;while(lo<hi){var m=(lo+hi)>>1;if(a[m][0]<t)lo=m+1;else hi=m}
   rows.push('<div><i style="display:inline-block;width:10px;height:3px;background:'+s.c+';margin-right:5px;vertical-align:middle"></i>'
   +s.n+': <b>'+f(a[lo][1])+'</b></div>')});
  var dt=new Date(t);tip.innerHTML='<div class="muted">'+dt.toISOString().slice(0,10)+'</div>'+rows.join('');
  tip.style.display='block';var bx=(e.clientX-b.left);tip.style.left=Math.min(bx+12,b.width-tip.offsetWidth-4)+'px';
  tip.style.top='8px';hair.setAttribute('x1',px);hair.setAttribute('x2',px);hair.setAttribute('opacity',1)});
 svg.addEventListener('mouseleave',function(){tip.style.display='none';hair.setAttribute('opacity',0)});
});
"""


def page(title: str, body: str, description: str = "") -> str:
    light = "".join(f"--s{i + 1}:{c};" for i, c in enumerate(SERIES_LIGHT))
    dark = "".join(f"--s{i + 1}:{c};" for i, c in enumerate(SERIES_DARK))
    css = CSS.replace("%LIGHT%", light).replace("%DARK%", dark)
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{esc(title)}</title><meta name='description' content='{esc(description)}'>"
            f"<style>{css}</style></head><body><main>{body}</main><script>{JS}</script></body></html>")


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def fmt(v, kind: str = "num") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "<span class='muted'>-</span>"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int | float):
        if kind == "pct":
            cls = "pos" if v > 0 else "neg" if v < 0 else ""
            return f"<span class='{cls}'>{v * 100:,.1f}%</span>"
        if kind == "krw":
            cls = "pos" if v > 0 else "neg" if v < 0 else ""
            return f"<span class='{cls}'>{v:,.0f}</span>"
        if kind == "r":
            cls = "pos" if v > 0 else "neg" if v < 0 else ""
            return f"<span class='{cls}'>{v:+.3f}</span>"
        if kind == "p":
            return f"{v:.3f}"
        if isinstance(v, int) or float(v).is_integer() and abs(v) > 100:
            return f"{v:,.0f}"
        return f"{v:,.2f}"
    return esc(v)


def table(rows: list[dict], cols: list[tuple[str, str, str]], max_rows: int | None = None) -> str:
    """cols: (key, header, kind) where kind in num|pct|krw|r|p|text."""
    if not rows:
        return "<p class='muted'>(no rows)</p>"
    head = "".join(f"<th class='{'l' if k == 'text' else ''}'>{esc(h)}</th>" for _, h, k in cols)
    body = []
    for r in rows[:max_rows] if max_rows else rows:
        tds = "".join(f"<td class='{'l' if kind == 'text' else ''}'>{fmt(r.get(key), kind)}</td>" for key, _, kind in cols)
        body.append(f"<tr>{tds}</tr>")
    return f"<div class='tbl'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def line_chart(series: list[tuple[str, list[list]]], value_fmt: str = "krw", height: int = 280,
               baseline: float | None = None, title: str = "") -> str:
    """Multi-series line chart (<=8 series, fixed slot order). Points are [ts_ms, value]."""
    series = [(n, p) for n, p in series if p][:8]
    if not series:
        return "<p class='muted'>(no data)</p>"
    W, H, L, R, T, B = 1100, height, 64, 12, 10, 26
    xs = [p[0] for _, pts in series for p in pts]
    ys = [p[1] for _, pts in series for p in pts if p[1] is not None]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if baseline is not None:
        y0, y1 = min(y0, baseline), max(y1, baseline)
    pad = (y1 - y0) * 0.05 or 1
    y0, y1 = y0 - pad, y1 + pad

    def X(t):
        return L + (t - x0) / ((x1 - x0) or 1) * (W - L - R)

    def Y(v):
        return T + (1 - (v - y0) / ((y1 - y0) or 1)) * (H - T - B)

    parts = [f"<svg viewBox='0 0 {W} {H}' data-w='{W}' data-l='{L}' data-r='{R}' data-x0='{x0}' data-x1='{x1}' role='img' "
             f"aria-label='{esc(title)}'>"]
    for k in range(5):
        v = y0 + (y1 - y0) * k / 4
        yy = Y(v)
        lab = f"{v * 100:.0f}%" if value_fmt == "pct" else f"{v:,.0f}"
        parts.append(f"<line x1='{L}' x2='{W - R}' y1='{yy:.1f}' y2='{yy:.1f}' stroke='var(--grid)' stroke-width='1'/>"
                     f"<text x='{L - 6}' y='{yy + 4:.1f}' text-anchor='end' font-size='11' fill='var(--muted)'>{lab}</text>")
    years = range(datetime.fromtimestamp(x0 / 1000, UTC).year, datetime.fromtimestamp(x1 / 1000, UTC).year + 2)
    for yr in years:
        for mo in (1, 7):
            t = datetime(yr, mo, 1, tzinfo=UTC).timestamp() * 1000
            if x0 <= t <= x1:
                parts.append(f"<text x='{X(t):.1f}' y='{H - 8}' text-anchor='middle' font-size='11' fill='var(--muted)'>"
                             f"{yr}-{mo:02d}</text><line x1='{X(t):.1f}' x2='{X(t):.1f}' y1='{H - B}' y2='{H - B + 4}' "
                             f"stroke='var(--border)'/>")
    if baseline is not None:
        parts.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(baseline):.1f}' y2='{Y(baseline):.1f}' stroke='var(--muted)' "
                     f"stroke-dasharray='3 3'/>")
    js = []
    for i, (name, pts) in enumerate(series):
        step = max(1, len(pts) // 600)
        sp = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
        d = "M" + " L".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in sp if v is not None)
        parts.append(f"<path d='{d}' fill='none' stroke='var(--s{i + 1})' stroke-width='2' stroke-linejoin='round'/>")
        js.append({"n": name, "c": SERIES_LIGHT[i], "p": [[int(t), v] for t, v in sp]})
    parts.append(f"<line class='hair' x1='0' x2='0' y1='{T}' y2='{H - B}' stroke='var(--text-2)' stroke-width='1' opacity='0'/>")
    parts.append("</svg>")
    legend = "".join(f"<span><i style='background:var(--s{i + 1})'></i>{esc(n)}</span>" for i, (n, _) in enumerate(series))
    return (f"<div class='chart' data-fmt='{value_fmt}' data-series='{esc(json.dumps(js))}'>"
            f"<div class='legend'>{legend}</div>{''.join(parts)}<div class='tip'></div></div>")


def heat_cell(v: float | None, scale: float) -> str:
    if v is None or not math.isfinite(v):
        return "<td class='muted'>-</td>"
    a = min(1.0, abs(v) / scale) if scale > 0 else 0
    var = "--pos" if v > 0 else "--neg"
    return (f"<td style='background:color-mix(in srgb,var({var}) {a * 55:.0f}%,var(--mid))' title='{v * 100:.2f}%'>"
            f"{v * 100:.1f}%</td>")
