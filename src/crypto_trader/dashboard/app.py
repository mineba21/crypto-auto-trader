"""FastAPI dashboard: one HTML page polling JSON endpoints. Read-only: it cannot place orders or reset the kill switch."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import polars as pl
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from crypto_trader.core.config import Settings
from crypto_trader.core.models import Mode
from crypto_trader.core.timeutils import fmt_kst, now_ms
from crypto_trader.features.orderflow import orderbook_frame_obi
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore

_TICKER_CACHE: dict = {"ts": 0, "data": []}


def _ticker(markets: list[str]) -> list[dict]:
    if time.time() - _TICKER_CACHE["ts"] < 5:
        return _TICKER_CACHE["data"]
    try:
        r = httpx.get("https://api.upbit.com/v1/ticker", params={"markets": ",".join(markets)}, timeout=5)
        data = [{"market": x["market"], "price": x["trade_price"], "change": x["signed_change_rate"],
                 "vol24h_krw": x["acc_trade_price_24h"], "ts": x["timestamp"]} for x in r.json()]
    except Exception as e:  # noqa: BLE001
        data = [{"error": str(e)}]
    _TICKER_CACHE.update(ts=time.time(), data=data)
    return data


def _latest(store: ParquetStore, kind: str, exchange: str, symbol: str, since_ms: int) -> pl.DataFrame:
    try:
        return store.read(kind, exchange, symbol, start_ms=since_ms, dedupe=False)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def market_snapshot(settings: Settings, store: ParquetStore) -> dict:
    since = now_ms() - 3 * 3_600_000
    out = {"tickers": _ticker(settings.markets), "flow": [], "derivatives": [], "liquidations": []}
    for m in settings.markets:
        tr = _latest(store, "trades", "upbit", m, since)
        row = {"market": m, "trades_3h": tr.height}
        if not tr.is_empty():
            buy = tr.filter(pl.col("aggressor") == "BID")["volume"].sum()
            sell = tr.filter(pl.col("aggressor") == "ASK")["volume"].sum()
            last15 = tr.filter(pl.col("event_ts") > now_ms() - 900_000)
            b15 = last15.filter(pl.col("aggressor") == "BID")["volume"].sum()
            s15 = last15.filter(pl.col("aggressor") == "ASK")["volume"].sum()
            row.update(cvd_3h=buy - sell, delta_15m=b15 - s15, last_trade=fmt_kst(int(tr["received_ts"].max())))
        ob = _latest(store, "orderbook", "upbit", m, now_ms() - 600_000)
        if not ob.is_empty():
            o = orderbook_frame_obi(ob.tail(50), depth=5)
            row.update(obi5=float(o["obi"][-1]), spread_bps=float(o["spread_bps"][-1]))
        out["flow"].append(row)
    for fut in settings.futures_symbols.values():
        inst = fut.removesuffix("USDT") + "-USDT-SWAP"
        oi = _latest(store, "open_interest", "okx", inst, now_ms() - 86_400_000)
        fr = _latest(store, "funding", "okx_estimate", inst, now_ms() - 86_400_000)
        d = {"symbol": inst}
        if not oi.is_empty():
            snap = oi.filter(pl.col("period") == "snapshot") if "period" in oi.columns else oi
            if not snap.is_empty():
                d.update(oi_usd=float(snap["oi_usd"][-1]), oi_ts=fmt_kst(int(snap["received_ts"][-1])))
        if not fr.is_empty():
            d.update(funding_est=float(fr["funding_rate"][-1]))
        liq = _latest(store, "liquidations", "okx", inst, now_ms() - 86_400_000)
        if not liq.is_empty():
            d.update(long_liq_usd_24h=float(liq.filter(pl.col("side") == "long_liq")["notional_usd"].fill_null(0).sum()),
                     short_liq_usd_24h=float(liq.filter(pl.col("side") == "short_liq")["notional_usd"].fill_null(0).sum()))
        out["derivatives"].append(d)
    return out


def account_snapshot(settings: Settings, mode: str) -> dict:
    p = settings.path(settings.ops_db) if mode == settings.mode else settings.path(settings.ops_db).with_name(f"ops_{mode.lower()}.sqlite")
    if not Path(p).exists():
        return {"mode": mode, "available": False}
    db = OpsDB(p, mode)
    try:
        ks = db.get_kv("kill_switch", {"active": False, "reasons": []})
        orders = [o for o in db.load_orders(active_only=True)]
        eq = db.equity_curve(2000)
        return {
            "mode": mode, "available": True, "kill_switch": ks,
            "paper_account": db.get_kv("paper_account"), "risk_state": db.get_kv("risk_state"),
            "ws_health": db.get_kv("ws_health"),
            "positions": [p.__dict__ for p in db.load_positions().values()],
            "open_orders": [{"coid": o.client_order_id, "symbol": o.request.symbol, "side": str(o.request.side),
                             "type": str(o.request.order_type), "state": str(o.state), "price": o.request.price,
                             "volume": o.request.volume, "filled": o.filled_volume} for o in orders],
            "fills": db.fills(30), "signals": db.signals(30), "events": db.events(40), "heartbeats": db.heartbeats(),
            "equity": eq[-1] if eq else None, "equity_curve": [[e["ts"], e["equity_krw"]] for e in eq],
        }
    finally:
        db.close()


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trader Dashboard</title><style>
:root{color-scheme:light;--bg:#fcfcfb;--bg2:#f3f2ef;--t:#0b0b0b;--t2:#52514e;--b:#d9d8d3;--good:#0ca30c;--crit:#d03b3b;--warn:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#1a1a19;--bg2:#242422;--t:#f0efec;--t2:#c3c2b7;--b:#3a3a36}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--bg2:#242422;--t:#f0efec;--t2:#c3c2b7;--b:#3a3a36}
body{margin:0;background:var(--bg);color:var(--t);font:13px/1.45 -apple-system,"Segoe UI","Noto Sans KR",sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 8px}h2{font-size:15px;margin:18px 0 6px;color:var(--t2)}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.card{background:var(--bg2);border-radius:8px;padding:10px 12px;overflow-x:auto}
table{border-collapse:collapse;width:100%}td,th{padding:3px 6px;border-bottom:1px solid var(--b);text-align:right;white-space:nowrap}
td:first-child,th:first-child{text-align:left}th{color:var(--t2);font-weight:600}.ok::before{content:"● ";color:var(--good)}
.bad::before{content:"● ";color:var(--crit)}.pill{display:inline-block;border:1px solid var(--b);border-radius:10px;padding:0 8px;margin-right:6px}
.muted{color:var(--t2)}</style></head><body><main>
<h1>Crypto Auto Trader <span class="muted" id="mode"></span></h1><div id="status"></div>
<div class="row"><div class="card"><h2>Upbit 시세</h2><div id="tickers"></div></div>
<div class="card"><h2>Order flow (Upbit 체결/호가)</h2><div id="flow"></div></div>
<div class="card"><h2>해외 선물 (OKX, 필터 전용)</h2><div id="deriv"></div></div></div>
<div class="row"><div class="card"><h2>자산 / 위험 한도</h2><div id="acct"></div></div>
<div class="card"><h2>포지션</h2><div id="pos"></div></div><div class="card"><h2>미체결 주문</h2><div id="orders"></div></div></div>
<div class="row"><div class="card"><h2>최근 신호 (차단 사유 포함)</h2><div id="sig"></div></div>
<div class="card"><h2>최근 체결</h2><div id="fills"></div></div></div>
<div class="row"><div class="card"><h2>시스템 이벤트</h2><div id="events"></div></div>
<div class="card"><h2>컴포넌트 / WS 상태</h2><div id="hb"></div></div></div>
<p class="muted">읽기 전용 대시보드 · 시각은 KST · 5초마다 갱신</p></main><script>
const q=new URLSearchParams(location.search);const MODE=q.get('mode')||'%MODE%';
function kst(ms){return ms?new Date(ms).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}):'-'}
function n(v,d=0){return v==null?'-':Number(v).toLocaleString(undefined,{maximumFractionDigits:d})}
function tbl(rows,cols){if(!rows||!rows.length)return'<span class="muted">(없음)</span>';
 return '<table><tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+(c[2]?c[2](r[c[0]],r):(r[c[0]]??'-'))+'</td>').join('')+'</tr>').join('')+'</table>'}
async function tick(){
 const [m,a]=await Promise.all([fetch('api/market').then(r=>r.json()),fetch('api/account?mode='+MODE).then(r=>r.json())]);
 document.getElementById('mode').textContent=MODE;
 const ks=a.kill_switch||{};document.getElementById('status').innerHTML=
  '<span class="pill">MODE '+MODE+'</span><span class="pill '+(ks.active?'bad':'ok')+'">Kill switch '+(ks.active?'ACTIVE: '+ks.reasons.map(r=>r.code).join(','):'off')+'</span>'
  +'<span class="pill '+((a.ws_health||{}).connected?'ok':'bad')+'">Upbit WS</span>'+(a.available?'':'<span class="pill bad">ops DB 없음</span>');
 document.getElementById('tickers').innerHTML=tbl(m.tickers,[['market','market'],['price','price',v=>n(v,2)],['change','24h',v=>v==null?'-':(v*100).toFixed(2)+'%'],['vol24h_krw','24h KRW',v=>n(v/1e8,1)+'억'],['ts','time',kst]]);
 document.getElementById('flow').innerHTML=tbl(m.flow,[['market','market'],['trades_3h','trades 3h'],['cvd_3h','CVD 3h',v=>n(v,3)],['delta_15m','Δ15m',v=>n(v,3)],['obi5','OBI5',v=>v==null?'-':v.toFixed(2)],['spread_bps','spread bp',v=>n(v,1)],['last_trade','last']]);
 document.getElementById('deriv').innerHTML=tbl(m.derivatives,[['symbol','inst'],['oi_usd','OI USD',v=>n(v/1e6,1)+'M'],['funding_est','funding(est)',v=>v==null?'-':(v*100).toFixed(4)+'%'],['long_liq_usd_24h','long liq 24h',v=>n(v/1e3,0)+'k'],['short_liq_usd_24h','short liq 24h',v=>n(v/1e3,0)+'k'],['oi_ts','at']]);
 const pa=a.paper_account||{},rs=a.risk_state||{};
 document.getElementById('acct').innerHTML=tbl([{k:'평가금액',v:n((a.equity||{}).equity_krw)},{k:'KRW',v:n(pa.cash??(a.equity||{}).cash_krw)},{k:'실현손익',v:n((a.equity||{}).realized_pnl)},{k:'미실현손익',v:n((a.equity||{}).unrealized_pnl)},{k:'일간 손실 한도',v:n(rs.daily_loss_limit)},{k:'거래당 위험',v:n(rs.per_trade_risk)},{k:'연속 손실',v:rs.consecutive_losses??'-'}],[['k',''],['v','']]);
 document.getElementById('pos').innerHTML=tbl(a.positions,[['symbol','symbol'],['volume','qty',v=>n(v,6)],['avg_price','avg',v=>n(v,2)],['stop_price','stop',v=>n(v,2)],['take_profit','target',v=>n(v,2)],['strategy','strategy']]);
 document.getElementById('orders').innerHTML=tbl(a.open_orders,[['symbol','symbol'],['side','side'],['type','type'],['state','state'],['price','price',v=>n(v,2)],['volume','vol',v=>n(v,6)]]);
 document.getElementById('sig').innerHTML=tbl(a.signals,[['decision_ts','time',kst],['symbol','sym'],['strategy','strategy'],['action','action'],['accepted','ok',v=>v?'✓':'✕'],['reason','reason']]);
 document.getElementById('fills').innerHTML=tbl(a.fills,[['ts','time',kst],['symbol','sym'],['side','side'],['price','price',v=>n(v,2)],['volume','vol',v=>n(v,6)],['fee','fee',v=>n(v,1)]]);
 document.getElementById('events').innerHTML=tbl(a.events,[['ts','time',kst],['level','lvl'],['component','comp'],['message','message']]);
 document.getElementById('hb').innerHTML=tbl(a.heartbeats,[['component','component'],['status','status'],['ts','last',kst],['detail','detail']])+
  '<p class="muted">WS reconnects: '+((a.ws_health||{}).reconnects??'-')+' · gaps: '+(((a.ws_health||{}).gaps)||[]).length+' · last error: '+((a.ws_health||{}).last_error||'-')+'</p>';
}
tick();setInterval(()=>tick().catch(console.error),5000);</script></body></html>"""


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="crypto-auto-trader dashboard")
    store = ParquetStore(settings.path(settings.data_dir))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.replace("%MODE%", str(settings.mode))

    @app.get("/api/market")
    def market() -> JSONResponse:
        return JSONResponse(_clean(market_snapshot(settings, store)))

    @app.get("/api/account")
    def account(mode: str = str(settings.mode)) -> JSONResponse:
        mode = Mode(mode.upper())
        return JSONResponse(_clean(account_snapshot(settings, str(mode))))

    return app
