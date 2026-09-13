"""Snapshot storage for computed reports (JSON cache + web index).

Design intent: the engine computes once per distinct parameter set; the result
is written as an immutable JSON snapshot under ``reports/`` and keyed by a hash
of the command + its parameters. A lightweight data *fingerprint* (latest trade
date + row counts) guards the cache: if the underlying market data has not
changed, re-running the exact same request returns the stored snapshot instead
of recomputing. This is what makes a future web view "a few clicks, just data"
with no recompute and no server-side state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        # psycopg2 numeric columns arrive as Decimal; float is fine for reports.
        return float(o)
    if hasattr(o, "item"):  # numpy scalars from polars round-trips
        try:
            return o.item()
        except Exception:
            return str(o)
    raise TypeError(f"Not JSON serializable: {type(o)!r}")


def to_json(obj) -> str:
    """JSON-encode an object, tolerating dates and numpy scalars."""
    return json.dumps(obj, default=_json_default, indent=2, sort_keys=True)


def _fingerprint() -> str:
    """Short hash of the market-data state; used to invalidate stale snapshots."""
    from src.db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), MAX(trade_date) FROM daily_market_summary"
            )
            summary_count, latest = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM daily_broker_rollup")
            rollup_count = cur.fetchone()[0]
        raw = f"{latest}|{summary_count}|{rollup_count}"
    except Exception:
        raw = "empty"
    finally:
        conn.close()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def snapshot_key(command: str, params: dict) -> str:
    """Stable 16-char id for a (command, params) combination."""
    raw = json.dumps(
        {"command": command, "params": params}, sort_keys=True, default=str
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def snapshot_path(command: str, params: dict) -> Path:
    return REPORTS_DIR / f"{snapshot_key(command, params)}.json"


def load_snapshot(command: str, params: dict):
    """Return cached data for (command, params) if present and data unchanged."""
    path = snapshot_path(command, params)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if payload.get("fingerprint") != _fingerprint():
        return None
    return payload.get("data")


def save_snapshot(command: str, params: dict, data, meta=None) -> Path:
    """Persist a computed report to reports/<key>.json and refresh the index."""
    REPORTS_DIR.mkdir(exist_ok=True)
    payload = {
        "command": command,
        "params": params,
        "fingerprint": _fingerprint(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "data": data,
    }
    path = snapshot_path(command, params)
    path.write_text(to_json(payload))
    regenerate_index()
    return path


def _available_dates() -> list[str]:
    """Distinct trade dates present in the DB (for the index date picker).

    Defensive: returns an empty list when the DB is unreachable so the web
    server can still boot and show the landing page.
    """
    try:
        from src.db import get_conn

        conn = get_conn()
    except Exception:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trade_date FROM daily_market_summary "
                "ORDER BY trade_date DESC"
            )
            return [str(r[0]) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def validate_as_of(as_of: str | None) -> str | None:
    """Return a clean YYYY-MM-DD string or None when invalid."""
    if not as_of:
        return None
    try:
        return date.fromisoformat(str(as_of)).isoformat()
    except ValueError:
        return None


def index_urls() -> list[dict]:
    """Metadata for every stored snapshot, newest first."""
    out: list[dict] = []
    REPORTS_DIR.mkdir(exist_ok=True)
    for path in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        out.append(
            {
                "command": payload.get("command"),
                "params": payload.get("params", {}),
                "generated_at": payload.get("generated_at"),
                "file": path.name,
            }
        )
    return out


_UI_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEPSE Screener &mdash; Reports</title>
<style>
:root{--bg:#0f1419;--panel:#171e26;--line:#2a3441;--text:#e6edf3;--mut:#8b98a5;--acc:#4da3ff;--ok:#34d399;--bad:#f87171;--vio:#d8b4fe}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);z-index:30}
.brand{padding:.6rem 1rem;font-weight:700;font-size:1.05rem;color:var(--acc)}
nav{display:flex;flex-wrap:wrap;gap:.25rem;padding:0 .5rem .5rem}
nav button{background:transparent;color:var(--mut);border:1px solid transparent;padding:.4rem .7rem;border-radius:6px;cursor:pointer;font-size:.9rem}
nav button:hover{color:var(--text)}
nav button.active{color:var(--acc);border-color:var(--acc)}
main{padding:1rem;max-width:1120px;margin:0 auto}
.view{display:none}
.view.active{display:block}
.controls{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end;margin-bottom:.75rem;padding:.75rem;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.controls label{display:flex;flex-direction:column;font-size:.72rem;color:var(--mut);gap:.25rem}
.controls input,.controls select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:.4rem .5rem;font-size:.9rem}
.controls button{background:var(--acc);color:#0b0f14;border:none;padding:.45rem .9rem;border-radius:6px;font-weight:600;cursor:pointer}
.controls button:hover{filter:brightness(1.1)}
.block{margin:1.1rem 0}
.block h3{margin:.2rem 0 .5rem;color:var(--mut);font-weight:600;font-size:.95rem}
table{width:100%;border-collapse:collapse;font-size:.84rem;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:.42rem .6rem;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#1c2631;color:var(--mut);font-weight:600}
tbody tr:hover td{background:#1b2530}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--ok)} .neg{color:var(--bad)} .mut{color:var(--mut)}
a.sym{color:var(--acc);cursor:pointer;font-weight:600;text-decoration:none}
a.sym:hover{text-decoration:underline}
a.broker{color:var(--vio);cursor:pointer;text-decoration:none}
a.broker:hover{text-decoration:underline}
.empty{color:var(--mut);padding:1rem .3rem}
#status{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--line);padding:.35rem .8rem;color:var(--mut);font-size:.78rem;z-index:40}
details{margin-top:1.5rem;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.5rem 1rem;font-size:.82rem}
summary{cursor:pointer;color:var(--mut);font-weight:600}
</style></head><body>
<header>
  <div class="brand">NEPSE Screener</div>
  <nav>
    <button class="active" data-view="top">Top Turnover</button>
    <button data-view="inspect">Inspect Symbol</button>
    <button data-view="broker">Broker</button>
    <button data-view="momentum">Momentum</button>
    <button data-view="wash">Wash</button>
    <button data-view="run">Full Scan</button>
    <button data-view="signals">Signals</button>
  </nav>
</header>
<main>
<div id="status"></div>

<section class="view active" id="view-top">
  <div class="controls"><label>Date<select id="top-date">__DATE_OPTS__</select></label>
  <label>Limit<input id="top-limit" type="number" value="20" min="1"></label>
  <button onclick="loadTop()">Load</button></div>
  <div class="block"><h3>Top by total turnover</h3><div id="top-out" class="empty">Select a date and click Load. Click any symbol to inspect it.</div></div>
</section>

<section class="view" id="view-inspect">
  <div class="controls"><label>Symbol<input id="insp-sym" value=""></label>
  <label>Sessions<input id="insp-sess" type="number" value="22" min="5"></label>
  <button onclick="loadInspect()">Inspect</button></div>
  <div class="block"><h3 id="recent-h">Recent Sessions</h3><div id="insp-recent" class="empty">Enter a symbol to inspect.</div></div>
  <div class="block"><h3>Broker Net Flows (by 22D)</h3><div id="insp-brokers"></div></div>
  <div class="block"><h3>Signal History</h3><div id="insp-signals"></div></div>
</section>

<section class="view" id="view-broker">
  <div class="controls"><label>Broker ID<input id="brok-id" type="number" value="1" min="1"></label>
  <label>Top N<input id="brok-top" type="number" value="5" min="1"></label>
  <label>Sessions<input id="brok-sess" type="number" value="66" min="5"></label>
  <button onclick="loadBroker()">Load</button></div>
  <div class="block"><h3 id="brok-h">Top holdings</h3><div id="brok-out" class="empty">Enter a broker ID and click Load. Click a symbol to inspect it.</div></div>
</section>

<section class="view" id="view-momentum">
  <div class="controls"><label>Short wnd<input id="mom-short" type="number" value="5" min="1"></label>
  <label>Base wnd<input id="mom-base" type="number" value="22" min="1"></label>
  <label>As-of<input id="mom-asof" type="date"></label>
  <button onclick="loadMomentum()">Run</button></div>
  <div class="block"><h3>Gainers</h3><div id="mom-gain" class="empty">Run the momentum scan.</div></div>
  <div class="block"><h3>Losers</h3><div id="mom-los" class="empty"></div></div>
</section>

<section class="view" id="view-wash">
  <div class="controls"><label>Window<input id="wash-wnd" type="number" value="22" min="5"></label>
  <label>Min qty<input id="wash-mq" type="number" value="5000" min="0"></label>
  <label>As-of<input id="wash-asof" type="date"></label>
  <button onclick="loadWash()">Run</button></div>
  <div class="block"><h3>Broker internal matching</h3><div id="wash-broker" class="empty">Run the wash scan.</div></div>
  <div class="block"><h3>Session cross / wash trades</h3><div id="wash-session" class="empty"></div></div>
</section>

<section class="view" id="view-run">
  <div class="controls"><label>Top N<input id="run-top" type="number" value="20" min="1"></label>
  <label>Holder wnd<input id="run-holder" type="number" value="22"></label>
  <label>As-of<input id="run-asof" type="date"></label>
  <button onclick="loadRun()">Run Scan</button></div>
  <div class="block"><h3>Track A &mdash; broker-flow</h3><div id="run-a" class="empty">Run the full scan.</div></div>
  <div class="block"><h3>Track B &mdash; stealth accumulation</h3><div id="run-b" class="empty"></div></div>
</section>

<section class="view" id="view-signals">
  <div class="controls"><label>Symbol<input id="sig-sym"></label>
  <label>Broker<input id="sig-broker" type="number" min="1"></label>
  <label>Signal<input id="sig-signal"></label>
  <label>Track<input id="sig-track"></label>
  <label>Streak<input id="sig-streak" type="number" min="1"></label>
  <label>Limit<input id="sig-limit" type="number" value="100" min="1"></label>
  <button onclick="loadSignals()">Query</button></div>
  <div class="block"><div id="sig-out" class="empty">Set filters and click Query.</div></div>
</section>

<details><summary>Saved snapshots (cached JSON)</summary>
<table><thead><tr><th>Command</th><th>Params</th><th>Generated</th><th>File</th></tr></thead>
<tbody>__SNAPSHOT_ROWS__</tbody></table>
</details>
</main>
<script>
var q=function(id){return document.getElementById(id)};
var val=function(id){var el=q(id);return el?el.value:''};
var fmt=function(v,d){d=d==null?2:d;return (v===null||v===undefined)?'\u2014':Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});};
var fmtInt=function(v){return (v===null||v===undefined)?'\u2014':Number(v).toLocaleString('en-US');};
var pctCls=function(v){return (v===null||v===undefined)?'mut':(v>=0?'pos':'neg');};
var pct=function(v){if(v===null||v===undefined)return '\u2014';return (v>=0?'+':'')+Number(v).toFixed(2)+'%';};
function showTab(name){var sels=document.querySelectorAll('.view');for(var i=0;i<sels.length;i++)sels[i].classList.remove('active');
var btns=document.querySelectorAll('nav button');for(var i=0;i<btns.length;i++)btns[i].classList.toggle('active',btns[i].dataset.view===name);
q('view-'+name).classList.add('active');}
var nbtns=document.querySelectorAll('nav button');for(var i=0;i<nbtns.length;i++)(function(b){b.addEventListener('click',function(){showTab(b.dataset.view);});})(nbtns[i]);
function updateTab(name){showTab(name);window.scrollTo({top:0,behavior:'smooth'});}
function setStatus(m){q('status').textContent=m;}
function renderTable(rows,cols){if(!rows||!rows.length)return '<div class="empty">No data.</div>';
var h='<table><thead><tr>';for(var i=0;i<cols.length;i++)h+='<th>'+cols[i].label+'</th>';h+='</tr></thead><tbody>';
for(var r=0;r<rows.length;r++){var row=rows[r];h+='<tr>';for(var i=0;i<cols.length;i++){var c=cols[i],v=row[c.key],cls='',td;
if(c.type==='num'){cls='num';td=fmt(v);}
else if(c.type==='int'){cls='num';td=fmtInt(v);}
else if(c.type==='pct'){cls='num '+pctCls(v);td=pct(v);}
else if(c.type==='sym'){cls='';td='<a class="sym" data-sym="'+v+'">'+v+'</a>';}
else if(c.type==='broker'){cls='';td='<a class="broker" data-broker="'+v+'">'+v+'</a>';}
else{cls='';td=(v===null||v===undefined)?'\u2014':v;}
h+='<td class="'+cls+'">'+td+'</td>';}
h+='</tr>';}
h+='</tbody></table>';return h;}
function bindClicks(container){if(!container)return;container.addEventListener('click',function(e){
var s=e.target.closest('a[data-sym]');if(s){q('insp-sym').value=s.dataset.sym;updateTab('inspect');loadInspect();return;}
var b=e.target.closest('a[data-broker]');if(b){q('brok-id').value=b.dataset.broker;updateTab('broker');loadBroker();}});}
function api(path){setStatus('Loading '+path+'\u2026');return fetch('/api/'+path).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(j){setStatus((j.cached?'cached: ':'computed: ')+path);return j.data;}).catch(function(e){setStatus('Error: '+e.message);return null;});}
var TOP_COLS=[{key:'rank',label:'Rank'},{key:'symbol',label:'Symbol',type:'sym'},{key:'close',label:'Close',type:'num'},{key:'change_pct',label:'Change %',type:'pct'},{key:'qty',label:'Qty',type:'int'},{key:'turnover',label:'Turnover',type:'num'}];
function loadTop(){var d=val('top-date'),l=val('top-limit')||20;api('top?as_of='+encodeURIComponent(d)+'&limit='+l).then(function(data){if(!data)return;q('top-out').innerHTML=renderTable(data.rows,TOP_COLS);bindClicks(q('top-out'));});}
var RECENT_COLS=[{key:'trade_date',label:'Date'},{key:'close_price',label:'Close',type:'num'},{key:'change_pct',label:'Change %',type:'pct'},{key:'qty',label:'Qty',type:'int'},{key:'turnover',label:'Turnover',type:'num'},{key:'rank',label:'Rank'}];
var BROKER_COLS=[{key:'broker_id',label:'Broker',type:'broker'},{key:'net_1d',label:'Net 1D',type:'int'},{key:'net_5d',label:'Net 5D',type:'int'},{key:'net_22d',label:'Net 22D',type:'int'},{key:'net_66d',label:'Net 66D',type:'int'},{key:'buy_vwap',label:'Buy VWAP',type:'num'},{key:'margin_pct',label:'Margin %',type:'pct'}];
var SIG_COLS=[{key:'trade_date',label:'Date'},{key:'symbol',label:'Symbol',type:'sym'},{key:'broker_id',label:'Broker',type:'broker'},{key:'track',label:'Track'},{key:'signal',label:'Signal'},{key:'turnover_rank',label:'Rank'},{key:'net_1d',label:'Net 1D',type:'int'},{key:'net_5d',label:'Net 5D',type:'int'},{key:'net_22d',label:'Net 22D',type:'int'},{key:'net_66d',label:'Net 66D',type:'int'},{key:'margin_pct',label:'Margin %',type:'pct'},{key:'t1_change_pct',label:'Change %',type:'pct'}];
function loadInspect(){var sym=val('insp-sym').trim().toUpperCase();if(!sym)return;api('inspect/'+sym+'?sessions='+(val('insp-sess')||22)).then(function(data){if(!data)return;q('recent-h').textContent='Recent Sessions \u2014 '+sym;q('insp-recent').innerHTML=renderTable(data.recent,RECENT_COLS);q('insp-brokers').innerHTML=renderTable(data.brokers,BROKER_COLS);q('insp-signals').innerHTML=renderTable(data.signals,SIG_COLS);bindClicks(q('insp-recent'));bindClicks(q('insp-brokers'));bindClicks(q('insp-signals'));});}
var HOLD_COLS=[{key:'symbol',label:'Symbol',type:'sym'},{key:'net_t1',label:'Net T1',type:'int'},{key:'net_t5',label:'Net T5',type:'int'},{key:'net_t22',label:'Net T22',type:'int'},{key:'net_t66',label:'Net T66',type:'int'},{key:'margin_pct',label:'Margin %',type:'pct'}];
function loadBroker(){var id=val('brok-id');if(!id)return;api('broker/'+id+'?top='+(val('brok-top')||5)+'&sessions='+(val('brok-sess')||66)).then(function(data){if(!data)return;q('brok-h').textContent='Broker '+id+' \u2014 top holdings';q('brok-out').innerHTML=renderTable(data.holdings,HOLD_COLS);bindClicks(q('brok-out'));});}
var MOM_COLS=[{key:'symbol',label:'Symbol',type:'sym'},{key:'avg_rank_base',label:'Avg Rank (Base)',type:'num'},{key:'avg_rank_short',label:'Avg Rank (Short)',type:'num'},{key:'rank_drift',label:'Rank Drift',type:'num'},{key:'turnover_ratio',label:'Turnover Ratio',type:'num'},{key:'close',label:'Close',type:'num'},{key:'price_change_pct_window',label:'Wnd Change %',type:'pct'},{key:'top_accumulator',label:'Top Accum',type:'broker'},{key:'top_distributor',label:'Top Distrib',type:'broker'}];
function loadMomentum(){api('momentum?short='+(val('mom-short')||5)+'&base='+(val('mom-base')||22)+'&as_of='+val('mom-asof')).then(function(data){if(!data)return;q('mom-gain').innerHTML=renderTable(data.gainers,MOM_COLS);q('mom-los').innerHTML=renderTable(data.losers,MOM_COLS);bindClicks(q('mom-gain'));bindClicks(q('mom-los'));});}
var WASH_BROKER=[{key:'broker_id',label:'Broker',type:'broker'},{key:'buy_qty',label:'Buy',type:'int'},{key:'sell_qty',label:'Sell',type:'int'},{key:'matched_qty',label:'Matched',type:'int'},{key:'gross_volume',label:'Gross',type:'int'},{key:'match_pct',label:'Match %',type:'pct'}];
var WASH_SESSION=[{key:'symbol',label:'Symbol',type:'sym'},{key:'total_qty',label:'Total Qty',type:'int'},{key:'crossed_qty',label:'Crossed',type:'int'},{key:'session_match_pct',label:'Match %',type:'pct'}];
function loadWash(){api('wash?window='+(val('wash-wnd')||22)+'&min_qty='+(val('wash-mq')||5000)+'&as_of='+val('wash-asof')).then(function(data){if(!data)return;q('wash-broker').innerHTML=renderTable(data.broker,WASH_BROKER);q('wash-session').innerHTML=renderTable(data.session,WASH_SESSION);bindClicks(q('wash-broker'));bindClicks(q('wash-session'));});}
var TRACK_A_COLS=[{key:'symbol',label:'Symbol',type:'sym'},{key:'signal',label:'Signal'},{key:'close',label:'Close',type:'num'},{key:'net_t1',label:'Net T1',type:'int'},{key:'net_t5',label:'Net T5',type:'int'},{key:'net_t22',label:'Net T22',type:'int'},{key:'net_t66',label:'Net T66',type:'int'},{key:'margin_pct',label:'Margin %',type:'pct'},{key:'t1_turnover',label:'T1 Turnover',type:'num'}];
var TRACK_B_COLS=[{key:'symbol',label:'Symbol',type:'sym'},{key:'signal',label:'Signal'},{key:'broker_id',label:'Broker',type:'broker'},{key:'close',label:'Close',type:'num'},{key:'net_t22',label:'Net T22',type:'int'},{key:'absorption_pct',label:'Absorb %',type:'pct'},{key:'t22_price_change_pct',label:'22D Δ%',type:'pct'},{key:'volume_inflection',label:'Vol Inflect',type:'num'},{key:'margin_pct',label:'Margin %',type:'pct'}];
function loadRun(){api('run?top='+(val('run-top')||20)+'&as_of='+val('run-asof')+'&top_holder_window='+(val('run-holder')||22)).then(function(data){if(!data)return;q('run-a').innerHTML=renderTable(data.track_a,TRACK_A_COLS);q('run-b').innerHTML=renderTable(data.track_b,TRACK_B_COLS);bindClicks(q('run-a'));bindClicks(q('run-b'));});}
function loadSignals(){var p=[];if(val('sig-sym'))p.push('symbol='+encodeURIComponent(val('sig-sym').trim().toUpperCase()));if(val('sig-broker'))p.push('broker='+val('sig-broker'));if(val('sig-signal'))p.push('signal='+encodeURIComponent(val('sig-signal')));if(val('sig-track'))p.push('track='+encodeURIComponent(val('sig-track')));if(val('sig-streak'))p.push('streak='+val('sig-streak'));p.push('limit='+(val('sig-limit')||100));api('signals?'+p.join('&')).then(function(data){if(!data)return;if(val('sig-streak'))q('sig-out').innerHTML=renderTable(data.streaks,SIG_COLS);else q('sig-out').innerHTML=renderTable(data.rows,SIG_COLS);bindClicks(q('sig-out'));});}
</script>
</body></html>"""


def index_html() -> str:
    """Self-contained navigable single-page UI for all report views."""
    dates = _available_dates()
    date_opts = (
        "".join(f'<option value="{d}">{d}</option>' for d in dates)
        or '<option value="">no data yet</option>'
    )
    snaps = index_urls()
    rows = "".join(
        "<tr>"
        f"<td>{s['command']}</td>"
        f"<td><code>{json.dumps(s['params'])}</code></td>"
        f"<td>{s['generated_at']}</td>"
        f"<td><a href='/reports/{s['file']}'>view JSON</a></td>"
        "</tr>"
        for s in snaps
    ) or '<tr><td colspan="4"><em>No reports computed yet.</em></td></tr>'
    return (
        _UI_HTML.replace("__DATE_OPTS__", date_opts)
        .replace("__SNAPSHOT_ROWS__", rows)
    )


def regenerate_index() -> None:
    """Write reports/index.html from the current snapshots."""
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "index.html").write_text(index_html())
