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
            try:
                cur.execute(
                    "SELECT COUNT(*), COUNT(market_cap), MAX(trade_date) FROM daily_market_summary"
                )
                res = cur.fetchone()
                summary_count, mcap_count, latest = res if res else (0, 0, None)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.execute(
                    "SELECT COUNT(*), MAX(trade_date) FROM daily_market_summary"
                )
                summary_count, latest = cur.fetchone()
                mcap_count = 0
            cur.execute("SELECT COUNT(*) FROM daily_broker_rollup")
            rollup_count = cur.fetchone()[0]
        try:
            import os
            from pathlib import Path
            src_dir = Path(__file__).parent
            mtimes = [p.stat().st_mtime for p in src_dir.glob("*.py")]
            code_mtime = max(mtimes) if mtimes else 0
        except Exception:
            code_mtime = 0
            
        raw = f"{latest}|{summary_count}|{mcap_count}|{rollup_count}|{code_mtime}"
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
    try:
        path.write_text(to_json(payload))
    except Exception:
        pass
    try:
        regenerate_index()
    except Exception:
        pass
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
th,td{padding:.42rem .6rem;vertical-align:middle;border-bottom:1px solid var(--line);white-space:nowrap}
th.text-left,td.text-left{text-align:left}
th.text-center,td.text-center{text-align:center}
th.text-right,td.text-right{text-align:right}
th{background:#1c2631;color:var(--mut);font-weight:600}
tbody tr:hover td{background:#1b2530}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--ok)} .neg{color:var(--bad)} .mut{color:var(--mut)}
th.has-help{cursor:help;border-bottom:1px dotted var(--mut)}
.concl{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.7rem 1rem;font-size:.84rem}
.concl-head{color:var(--text);margin:.1rem 0 .5rem}
.concl ul{margin:.3rem 0 .5rem;padding-left:1.2rem;color:var(--text)}
.concl li{margin:.28rem 0;line-height:1.45}
.concl-tail{color:var(--acc);margin:.4rem 0 0;font-weight:600}
a.sym{color:var(--acc);cursor:pointer;font-weight:600;text-decoration:none}
a.sym:hover{text-decoration:underline}
a.broker{color:var(--vio);cursor:pointer;text-decoration:none}
a.broker:hover{text-decoration:underline}
.empty{color:var(--mut);padding:1rem .3rem}
table.meta{max-width:520px}
table.meta th{width:110px;color:var(--mut);vertical-align:top;white-space:nowrap}
div.notes{margin-top:.9rem}
div.notes h4{margin:.4rem 0 .4rem;color:var(--mut);font-weight:600;font-size:.9rem}
#status{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--line);padding:.35rem .8rem;color:var(--mut);font-size:.78rem;z-index:40}
.spin{display:inline-block;width:.68rem;height:.68rem;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:sp .6s linear infinite;vertical-align:-.12rem;margin-right:.4rem}
.spin.hidden{display:none!important}
@keyframes sp{to{transform:rotate(360deg)}}
button[disabled]{opacity:.55;cursor:not-allowed}
details{margin-top:1.5rem;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.5rem 1rem;font-size:.82rem}
summary{cursor:pointer;color:var(--mut);font-weight:600}
.calbtn{background:var(--panel);border:1px solid var(--line);border-radius:6px;cursor:pointer;padding:.42rem .5rem;font-size:.9rem;color:var(--mut);line-height:1}
.calbtn:hover{color:var(--acc);border-color:var(--acc)}
.calpop{position:fixed;z-index:80;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.4);padding:.5rem;width:250px}
.calpop .calhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem;font-weight:600}
.calpop .calhead button{background:none;border:none;cursor:pointer;font-size:1.15rem;color:var(--acc);padding:0 .3rem}
.calpop table{width:100%;border:none;background:none}
.calpop th,.calpop td{border:none}
.calpop th{color:var(--mut);font-size:.66rem;padding:.12rem 0;text-align:center}
.calpop td{padding:1px}
.calpop td button{width:100%;background:none;border:none;border-radius:4px;cursor:pointer;padding:.32rem 0;font-size:.78rem;color:var(--text);text-align:center}
.calpop td button:hover{background:var(--line)}
.calpop td button.sel{background:var(--acc);color:#0b0f14;font-weight:600}
td.actions{white-space:nowrap}
td.actions button{background:var(--panel);border:1px solid var(--line);border-radius:4px;color:var(--mut);cursor:pointer;font-size:.72rem;padding:.14rem .42rem;margin-right:.25rem}
td.actions button:hover{color:var(--acc);border-color:var(--acc)}
.badge{display:inline-block;padding:.18rem .45rem;border-radius:4px;font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.badge[data-cap],.badge[data-sec]{cursor:pointer;text-decoration:none;transition:all .15s}
.badge[data-cap]:hover,.badge[data-sec]:hover{transform:translateY(-1px);filter:brightness(1.2)}
.badge.gainer{background:rgba(52,211,153,.15);color:var(--ok);border:1px solid rgba(52,211,153,.35)}
.badge.stealth{background:rgba(77,163,255,.15);color:var(--acc);border:1px solid rgba(77,163,255,.35)}
.badge.stable{background:rgba(216,180,254,.15);color:var(--vio);border:1px solid rgba(216,180,254,.35)}
.badge.loser{background:rgba(248,113,113,.15);color:var(--bad);border:1px solid rgba(248,113,113,.35)}
.badge.fading{background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.35)}
.badge.neutral{background:rgba(139,152,165,.15);color:var(--mut);border:1px solid rgba(139,152,165,.35)}
.badge.large{background:rgba(52,211,153,.15);color:var(--ok);border:1px solid rgba(52,211,153,.35)}
.badge.mid{background:rgba(77,163,255,.15);color:var(--acc);border:1px solid rgba(77,163,255,.35)}
.badge.small{background:rgba(216,180,254,.15);color:var(--vio);border:1px solid rgba(216,180,254,.35)}
.badge.sec{background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--line);text-transform:none}
.view-intro{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:8px;padding:.7rem .9rem;margin-bottom:.85rem;font-size:.82rem;line-height:1.45}
.view-intro h2{margin:0 0 .25rem;font-size:.92rem;color:var(--text);font-weight:600}
.view-intro p{margin:.2rem 0;color:var(--text)}
.view-intro .tips{margin-top:.45rem;display:flex;flex-wrap:wrap;gap:.4rem;font-size:.74rem;color:var(--mut)}
.view-intro .tag{background:rgba(255,255,255,.04);border:1px solid var(--line);padding:.12rem .4rem;border-radius:4px;color:var(--mut)}
.view-intro .tag b{color:var(--text)}
</style></head><body>
<header>
  <div class="brand">NEPSE Screener</div>
  <nav>
    <button class="active" data-view="top" title="Top turnover ranking across the exchange for a given trading session.">Top Turnover</button>
    <button data-view="inspect" title="Centralized single-stock dashboard: verdict, broker net flows, momentum, and signals.">Stock Central</button>
    <button data-view="market" title="Global view of all NEPSE stocks categorized by Buy/Hold/Sell/Avoid with broker details.">Market Overview</button>
    <button data-view="sectors" title="Sector Rotation Dashboard: Track macro capital flow across industries.">Sectors</button>
    <button data-view="syndicates" title="Detect broker syndicates colluding to accumulate the same stocks.">Syndicates</button>
    <button data-view="backtest" title="Historical T+5 and T+20 Win Rate Backtester.">Backtester</button>
    <button data-view="broker" title="Broker holdings &amp; activity: multi-session net flows and accumulated symbols.">Broker</button>
    <button data-view="momentum" title="Turnover &amp; rank rotation: compare recent vs baseline liquidity shifts.">Momentum</button>
    <button data-view="wash" title="Internal broker matching: detect same-broker buy and sell cross-trades.">Wash</button>
    <button data-view="smartmoney" title="Track C: Playwright scraper + AI insights on smart money absorption.">Smart Money Alerts</button>
    <button data-view="run" title="Dual-track screener: Track A momentum and Track B stealth accumulation.">Full Scan</button>
    <button data-view="signals" title="Historical screener signals and persistence streaks.">Signals</button>
    <button data-view="analyze" title="Rank-price correlation, broker signature classification, and forward returns.">Analyze</button>
    <button data-view="watchlist" title="Personal research journal: track symbols, thesis notes, and trade plans.">Watchlist</button>
  </nav>
</header>
<main>
<div id="status"><span id="spin" class="spin hidden"></span><span id="status-msg"></span></div>

<section class="view active" id="view-top">
  <div class="view-intro">
    <h2>Top Turnover Ranking</h2>
    <p>Displays NEPSE equities ranked by total monetary turnover (NRS) for any trading session. Highlights where exchange liquidity, market participation, and institutional order flow are concentrated.</p>
    <div class="tips">
      <span class="tag"><b>Turnover (NRS)</b>: Total traded value</span>
      <span class="tag"><b>Sector &amp; Cap Tier</b>: Classification and size segmentation</span>
      <span class="tag"><b>Navigation</b>: Click any ticker to inspect historical candles &amp; broker flows</span>
    </div>
  </div>
  <div class="controls"><label title="Trading session to report on. Defaults to the latest; pick any session.">Session Date<input id="top-date" class="dti" type="date" list="top-dates" value="__LATEST_DATE__"></label>
  <datalist id="top-dates">__DATE_OPTS__</datalist>
  <label title="Filter by sector name (e.g. Hydro Power, Commercial Banks)">Sector
    <select id="top-sector" class="input-filter" onchange="loadTop()">
      <option value="">All Sectors</option>
    </select>
  </label>
  <label title="Filter by market cap tier">Cap Tier
    <select id="top-cap" onchange="loadTop()">
      <option value="">All Tiers</option>
      <option value="LARGE">Large-Cap (&ge;20B)</option>
      <option value="MID">Mid-Cap (5B&ndash;20B)</option>
      <option value="SMALL">Small-Cap (&lt;5B)</option>
    </select>
  </label>
  <label title="Number of highest-turnover symbols to show.">Top N<input id="top-limit" type="number" value="20" min="1"></label>
  <button onclick="loadTop()">Load</button></div>
  <div class="block"><h3>Top by total turnover</h3><div id="top-out" class="empty">Pick a trading date (clear it for the latest) and click Load. Click any symbol to inspect it.</div></div>
</section>

<section class="view" id="view-inspect">
  <div class="view-intro">
    <h2>Stock Central Dashboard</h2>
    <p>Unified single-ticker deep dive: Actionable verdicts, multi-window price action, turnover momentum profiles, net broker inventory flows, and historical screener alerts.</p>
    <div class="tips">
      <span class="tag"><b>Verdict</b>: Strong Buy / Buy / Hold / Avoid</span>
      <span class="tag"><b>Fundamental Context</b>: Sector, market cap tier, and 52-week channel position</span>
      <span class="tag"><b>Momentum Profile</b>: Short vs baseline liquidity expansion &amp; state badge</span>
      <span class="tag"><b>Broker Net Flow</b>: Cumulative net buying/selling across T1 (1D), T5 (1W), T22 (1M), and T66 (1Q)</span>
      <span class="tag"><b>Signal History</b>: Multi-day persistent scanner alerts</span>
    </div>
  </div>
  <div class="controls"><label title="NEPSE ticker, e.g. LEC">Ticker<input id="insp-sym" placeholder="e.g. LEC" onkeydown="if(event.key==='Enter')loadInspect()"></label>
  <label title="How many recent trading sessions to display.">Lookback<input id="insp-sess" type="number" value="22" min="5"></label>
  <button onclick="loadInspect()">Inspect</button></div>
  <div class="block" id="insp-pos-wrap" style="display:none">
    <h3 id="insp-pos-h">Positioning Verdict &amp; Context</h3>
    <div id="insp-pos-context" style="margin-bottom:1rem;"></div>
    <div id="insp-pos-breakdown"></div>
  </div>
  <div class="block" id="insp-meta-wrap" style="display:none"><h3>Fundamental &amp; Technical Profile</h3><div id="insp-meta"></div></div>
  <div class="block" id="insp-mom-wrap" style="display:none"><h3>Momentum &amp; Liquidity Profile</h3><div id="insp-mom"></div></div>
  <div class="block" id="insp-snap-wrap" style="display:none"><h3>Key Accumulators &amp; Distributors Snapshot</h3><div id="insp-snap"></div></div>
  <div class="block"><h3 id="recent-h">Daily History</h3><div id="insp-recent" class="empty">Type a ticker and click Inspect.</div></div>
  <div class="block" id="insp-accum-wrap" style="display:none"><h3>Top Accumulator Brokers (Net Buyers)</h3><div id="insp-accum-brokers"></div></div>
  <div class="block" id="insp-dist-wrap" style="display:none"><h3>Top Distributor Brokers (Net Sellers / Dumping)</h3><div id="insp-dist-brokers"></div></div>
  <div class="block"><h3>All Broker Net Flows</h3><div id="insp-brokers"></div></div>
  <div class="block"><h3>Signal History</h3><div id="insp-signals"></div></div>
</section>

<section class="view" id="view-market">
  <div class="view-intro">
    <h2>Global Market Overview</h2>
    <p>Complete categorization of all active NEPSE stocks based on their institutional supply absorption and actionable verdict.</p>
    <div class="tips">
      <span class="tag"><b>Verdict</b>: Strong Buy / Buy / Hold / Avoid</span>
      <span class="tag"><b>Top Accumulator</b>: The broker quietly buying the most shares today.</span>
      <span class="tag"><b>Top Distributor</b>: The broker dumping the most shares today.</span>
    </div>
  </div>
  <div class="controls">
    <button onclick="loadMarket()">Load Global Scanner</button>
  </div>
  <div class="block"><h3 id="market-h">Categorized Market Screener</h3>
    <div id="market-out" class="empty">Click "Load Global Scanner" to scan all active NEPSE stocks. (May take 5-10 seconds on first run of the day).</div>
  </div>
</section>

<section class="view" id="view-sectors">
  <div class="view-intro">
    <h2>Sector Rotation Dashboard</h2>
    <p>Track macro capital flows to spot which industries are absorbing the most liquidity and pulling the heaviest institutional accumulation.</p>
    <div class="tips">
      <span class="tag"><b>Sector Turnover</b>: Total volume flowing into the sector.</span>
      <span class="tag"><b>Top Accumulator</b>: The broker with the heaviest net-buy footprint across all stocks in this sector.</span>
    </div>
  </div>
  <div class="controls">
    <button onclick="loadSector()">Load Sector Breakdown</button>
  </div>
  <div class="block"><h3 id="sector-h">Sector Capital Flow Ranking</h3>
    <div id="sector-out" class="empty">Click "Load Sector Breakdown" to map capital rotation.</div>
  </div>
</section>

<section class="view" id="view-syndicates">
  <div class="view-intro">
    <h2>Broker Syndicate Detection</h2>
    <p>Network analysis identifying distinct brokers who hunt in packs, consistently co-accumulating the exact same stocks over the last 22 days.</p>
    <div class="tips">
      <span class="tag"><b>Co-occurrences</b>: The number of distinct stocks both brokers were top buyers in.</span>
    </div>
  </div>
  <div class="controls">
    <button onclick="loadSyndicate()">Detect Syndicates</button>
  </div>
  <div class="block"><h3>Identified Broker Packs (Last 22 Days)</h3>
    <div id="syndicate-out" class="empty">Click "Detect Syndicates" to run network clustering.</div>
  </div>
</section>

<section class="view" id="view-backtest">
  <div class="view-intro">
    <h2>Algorithmic Signal Backtester</h2>
    <p>Replaces intuition with statistics. Evaluates the historical Win Rate of all triggered signals over T+5 and T+20 holding periods.</p>
    <div class="tips">
      <span class="tag"><b>Win %</b>: Percentage of historical trades that resulted in a > 0% return.</span>
    </div>
  </div>
  <div class="controls">
    <button onclick="loadBacktest()">Run Backtest</button>
  </div>
  <div class="block"><h3>Historical Strategy Performance</h3>
    <div id="backtest-out" class="empty">Click "Run Backtest" to evaluate algorithm.</div>
  </div>
</section>

<section class="view" id="view-broker">
  <div class="view-intro">
    <h2>Broker Holdings &amp; Flow Surveillance</h2>
    <p>Surveillance workstation tracking an individual NEPSE member broker's accumulated stock inventory and net share flows across multi-session holding horizons.</p>
    <div class="tips">
      <span class="tag"><b>Net T1 / T5 / T22 / T66</b>: Net shares acquired (+) or distributed (&minus;) over 1, 5, 22, and 66 sessions</span>
      <span class="tag"><b>Margin %</b>: Unrealized gain/loss margin vs latest close</span>
      <span class="tag"><b>Distributions</b>: Stocks with the largest negative net flow (dumping / selling)</span>
    </div>
  </div>
  <div class="controls"><label title="NEPSE broker ID number (e.g. 18 = a specific member broker).">Broker ID<input id="brok-id" type="number" value="1" min="1" placeholder="e.g. 18"></label>
  <label title="Number of largest positions to display.">Show Top<input id="brok-top" type="number" value="5" min="1"></label>
  <label title="How many recent trading sessions to analyse.">Lookback<input id="brok-sess" type="number" value="66" min="5"></label>
  <button onclick="loadBroker()">Load</button></div>
  <div class="block"><h3 id="brok-h">Top Accumulations (Net Buying)</h3><div id="brok-out" class="empty">Enter a broker ID and click Load.</div></div>
  <div class="block"><h3 id="brok-dist-h">Top Distributions (Net Selling / Dumping)</h3><div id="brok-dist-out" class="empty">Enter a broker ID and click Load.</div></div>
</section>

<section class="view" id="view-momentum">
  <div class="view-intro">
    <h2>Turnover Momentum &amp; Liquidity Shifts</h2>
    <p>Dual-window liquidity rotation scanner comparing recent trading activity (e.g. 5 sessions) against a longer baseline (e.g. 22 sessions) to detect institutional buying surges, fading liquidity, and peer momentum similarity.</p>
    <div class="tips">
      <span class="tag"><b>Turnover Ratio</b>: Recent avg daily turnover &divide; baseline avg turnover (&gt;1.5x = expansion)</span>
      <span class="tag"><b>Rank Drift</b>: Baseline Rank &minus; Recent Rank (positive = climbed the liquidity board)</span>
      <span class="tag"><b>Peer Match</b>: Normalized Euclidean distance similarity (0&ndash;100%)</span>
      <span class="tag"><b>Badges</b>: MOMENTUM_GAINER &middot; STEALTH_BUILDING &middot; HIGH_VOLUME_STABLE &middot; MOMENTUM_LOSER &middot; LIQUIDITY_FADING</span>
    </div>
  </div>
  <div class="controls"><label title="Optional: inspect a specific ticker's turnover momentum & find similar peer stocks.">Ticker (Optional)<input id="mom-sym" placeholder="e.g. GHL"></label>
  <label title="Number of recent trading sessions to measure (e.g. 5 ≈ 1 week, 20 ≈ 1 month). Must be shorter than Baseline Window.">Recent Window<input id="mom-short" type="number" value="5" min="1"></label>
  <label title="Longer baseline reference window (e.g. 22 ≈ 1 month, 66 ≈ 1 quarter) to compare against.">Baseline Window<input id="mom-base" type="number" value="22" min="1"></label>
  <label title="Analysis end date. Defaults to the latest; pick another date.">As of Date<input id="mom-asof" class="dti" type="date" value="__LATEST_DATE__"></label>
  <button onclick="loadMomentum()">Run</button></div>
  <div class="block" id="mom-single-wrap" style="display:none"><h3>Symbol Momentum Diagnostic</h3><div id="mom-single"></div></div>
  <div class="block" id="mom-similar-wrap" style="display:none"><h3>Similar Momentum Profiles (Nearest Peers)</h3><div id="mom-similar"></div></div>
  <div class="block"><h3>Gainers &mdash; climbing the board</h3><div id="mom-gain" class="empty">Compares recent vs baseline turnover to find structural activity shifts.</div></div>
  <div class="block"><h3>Losers &mdash; fading activity</h3><div id="mom-los" class="empty"></div></div>
</section>

<section class="view" id="view-wash">
  <div class="view-intro">
    <h2>Wash Trading &amp; Internal Broker Cross Detection</h2>
    <p>Identifies circular and self-matched transactions where the same member broker executes both the buy and sell sides of trades for a symbol on the same trading session.</p>
    <div class="tips">
      <span class="tag"><b>Matched Qty</b>: min(Buy Qty, Sell Qty) internal cross volume</span>
      <span class="tag"><b>Match %</b>: Internal matched volume percentage (Matched &times; 2 &divide; Gross)</span>
      <span class="tag"><b>Purpose</b>: Filters artificial turnover generation, tax-swapping, and non-economic cross trades</span>
    </div>
  </div>
  <div class="controls"><label title="Number of recent sessions to scan for internal broker matching.">Lookback<input id="wash-wnd" type="number" value="22" min="5"></label>
  <label title="Minimum total quantity for a wash match to count.">Min Volume<input id="wash-mq" type="number" value="5000" min="0"></label>
  <label title="Analysis end date. Defaults to the latest; pick another date.">As of Date<input id="wash-asof" class="dti" type="date" value="__LATEST_DATE__"></label>
  <button onclick="loadWash()">Run</button></div>
  <div class="block"><h3>Broker internal matching</h3><div id="wash-broker" class="empty">Finds same-broker buy+sell matches. Click Run.</div></div>
  <div class="block"><h3>Session cross / wash trades</h3><div id="wash-session" class="empty"></div></div>
</section>

<section class="view" id="view-run">
  <div class="view-intro">
    <h2>Dual-Track Quantitative Screener</h2>
    <p>Runs dual-track market screening with dominant-broker overlays to classify high-volume market leaders and detect stealth institutional accumulation.</p>
    <div class="tips">
      <span class="tag"><b>Track A (Top 30 Turnover)</b>: Institutional Accumulation &middot; Bull Traps / Distribution &middot; Capitulation &middot; Breakouts</span>
      <span class="tag"><b>Track B (Ranks 31+)</b>: Silent accumulation where top broker absorbs &ge;15% of daily volume with positive returns</span>
      <span class="tag"><b>Sector &amp; Cap Filters</b>: Focus scan on specific sectors or market-cap tiers</span>
    </div>
  </div>
  <div class="controls"><label title="Size of the top-turnover universe scanned for Track A.">Turnover Top N<input id="run-top" type="number" value="20" min="1"></label>
  <label title="Sessions used to identify the dominant net-buyer broker per symbol (22 ≈ 1 month, 66 ≈ quarterly).">Dominant Broker Window<input id="run-holder" type="number" value="22"></label>
  <label title="Filter by sector name">Sector
    <select id="run-sector" class="input-filter" onchange="loadRun()">
      <option value="">All Sectors</option>
    </select>
  </label>
  <label title="Filter by market cap tier">Cap Tier
    <select id="run-cap" onchange="loadRun()">
      <option value="">All Tiers</option>
      <option value="LARGE">Large-Cap (&ge;20B)</option>
      <option value="MID">Mid-Cap (5B&ndash;20B)</option>
      <option value="SMALL">Small-Cap (&lt;5B)</option>
    </select>
  </label>
  <label title="Analysis end date. Defaults to the latest; pick another date.">As of Date<input id="run-asof" class="dti" type="date" value="__LATEST_DATE__"></label>
  <button onclick="loadRun()">Run Scan</button></div>
  <div class="block"><h3>Track A &mdash; Broker Flow Signals</h3><div id="run-a" class="empty">Runs Track A + Track B with a dominant-broker overlay.</div></div>
  <div class="block"><h3>Track B &mdash; Stealth Accumulation Setups</h3><div id="run-b" class="empty"></div></div>
</section>

<section class="view" id="view-smartmoney">
  <div class="view-intro">
    <h2>Smart Money Absorption (Playwright + Gemini AI)</h2>
    <p>Scrapes live ShareSansar top brokers, identifies extreme absorption accumulation, and uses AI to generate instant actionable insights.</p>
    <div class="tips">
      <span class="tag"><b>Web Scraper</b>: Headless Chromium bypasses rate limits.</span>
      <span class="tag"><b>AI Analyst</b>: Gemini summarizes the technicals and flows.</span>
    </div>
  </div>
  <div class="controls">
  <label title="Analysis end date. Defaults to the latest.">As of Date<input id="sm-asof" class="dti" type="date" value="__LATEST_DATE__"></label>
  <button onclick="loadSmartmoney()">Run AI Scan</button></div>
  <div class="block"><h3>Real-time Alerts</h3><div id="sm-out" class="empty">Click "Run AI Scan" to trigger the pipeline (can take ~15s).</div></div>
</section>

<section class="view" id="view-signals">
  <div class="view-intro">
    <h2>Historical Screener Signals &amp; Streaks</h2>
    <p>Searchable audit database of historical Track A and Track B screener alerts to identify sustained accumulation campaigns and verify setup persistence.</p>
    <div class="tips">
      <span class="tag"><b>Min Streak</b>: Consecutive live trading sessions with active screener signals (weekends excluded)</span>
      <span class="tag"><b>Persistence</b>: Streaks &ge;2 sessions show substantially higher statistical follow-through</span>
    </div>
  </div>
  <div class="controls"><label title="Filter by ticker.">Ticker<input id="sig-sym" placeholder="e.g. LEC"></label>
  <label title="Filter by broker ID.">Broker ID<input id="sig-broker" type="number" min="1" placeholder="any"></label>
  <label title="Filter by signal name.">Signal Type<input id="sig-signal" placeholder="e.g. SILENT_ACCUMULATION"></label>
  <label title="A = top-turnover broker-flow; B = stealth setups.">Track (A or B)<input id="sig-track" placeholder="A or B"></label>
  <label title="Only show signals live for at least N consecutive sessions (weekends don't break a streak).">Min Streak<input id="sig-streak" type="number" min="1" placeholder="any"></label>
  <label title="Maximum number of results to return.">Max Rows<input id="sig-limit" type="number" value="100" min="1"></label>
  <button onclick="loadSignals()">Query</button></div>
  <div class="block"><div id="sig-out" class="empty">Filter persisted signal history and click Query. Min Streak = consecutive live sessions.</div></div>
</section>

<section class="view" id="view-analyze">
  <div class="view-intro">
    <h2>Rank &harr; Price Correlation &amp; Broker Signatures</h2>
    <p>Statistical predictability engine analyzing how turnover board leadership and broker crowd behaviors correlate with future price performance and directional edge.</p>
    <div class="tips">
      <span class="tag"><b>Broker Signatures</b>: MULTI (broad buying) &middot; SINGLE (lone driver) &middot; DISTRIBUTE (net selling) &middot; NEUTRAL</span>
      <span class="tag"><b>Prediction Horizons</b>: Historical win rates and average returns over T+1, T+3, and T+5 windows</span>
    </div>
  </div>
  <div class="controls"><label title="NEPSE ticker, e.g. ADBL">Ticker<input id="anl-sym" placeholder="e.g. ADBL"></label>
  <label title="Number of recent trading sessions analysed + prediction window.">Lookback<input id="anl-sess" type="number" value="30" min="5"></label>
  <button onclick="loadAnalyze()">Analyze</button></div>
  <div class="block"><h3 id="anl-h">Timeline</h3><div id="anl-timeline" class="empty">Type a ticker and click Analyze. Click a Top Accum broker to jump to the Broker tab.</div></div>
  <div class="block"><h3>Rank &harr; Price</h3><div id="anl-relation" class="empty"></div></div>
  <div class="block"><h3>Signature Performance</h3><div id="anl-sigperf" class="empty"></div></div>
  <div class="block"><h3>Prediction</h3><div id="anl-prediction" class="empty"></div></div>
  <div class="block"><h3>Read / Conclusion</h3><div id="anl-conclusion" class="empty"></div></div>
</section>

<section class="view" id="view-watchlist">
  <div class="view-intro">
    <h2>Personal Research Journal &amp; Trade Planner</h2>
    <p>Durable research notebook to record investment theses, thematic tags, planned trade execution levels (entry, target, stop), and chronological dated field notes with live PnL tracking.</p>
    <div class="tips">
      <span class="tag"><b>Active / Archive</b>: Retain completed trade journals while archiving inactive setups</span>
      <span class="tag"><b>Live PnL</b>: Automatically marked to market against the latest session closing price</span>
      <span class="tag"><b>CLI Sync</b>: Fully synced with CLI commands (<code>watch add</code>, <code>watch note</code>, <code>watch enter</code>, <code>watch exit</code>)</span>
    </div>
  </div>
  <div class="block"><h3>Add symbol</h3><div class="controls">
    <label title="NEPSE ticker, e.g. LEC">Symbol<input id="wl-symbol" placeholder="e.g. LEC"></label>
    <label title="Why the ticker is on watch">Thesis<input id="wl-thesis" placeholder="optional"></label>
    <label title="Comma-separated research tags">Tags<input id="wl-tags" placeholder="optional"></label>
    <label title="Optional first dated journal note">First note<input id="wl-note" placeholder="optional"></label>
    <button id="wl-add-btn" onclick="wlAdd()">Add</button>
  </div></div>
  <div class="controls"><button onclick="loadWatchlist()">Refresh</button></div>
  <div class="block"><h3>Watchlist</h3><div id="wl-list" class="empty">Add a symbol above, or manage via CLI (<code>watch add ...</code>). Click a symbol for its dated journal.</div></div>
  <div class="block"><h3 id="wl-detail-h">Journal details</h3><div id="wl-detail" class="empty">Click a symbol above to view its thesis, trade plan and dated notes.</div></div>
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
async function initSectorDropdowns(){
  try{
    var res=await fetch('/api/sectors');
    if(!res.ok)return;
    var data=await res.json();
    var sectors=data.data||data;
    ['top-sector','run-sector'].forEach(function(id){
      var el=q(id);
      if(!el)return;
      var currentVal=el.value;
      el.innerHTML='<option value="">All Sectors</option>'+sectors.map(function(s){return '<option value="'+s+'">'+s+'</option>';}).join('');
      if(currentVal)el.value=currentVal;
    });
  }catch(err){console.error('Failed to load sectors:',err);}
}
document.addEventListener('DOMContentLoaded',initSectorDropdowns);
function showTab(name){var sels=document.querySelectorAll('.view');for(var i=0;i<sels.length;i++)sels[i].classList.remove('active');
var btns=document.querySelectorAll('nav button');for(var i=0;i<btns.length;i++)btns[i].classList.toggle('active',btns[i].dataset.view===name);
q('view-'+name).classList.add('active');}
var nbtns=document.querySelectorAll('nav button');for(var i=0;i<nbtns.length;i++)(function(b){b.addEventListener('click',function(){showTab(b.dataset.view);if(b.dataset.view==='watchlist')loadWatchlist();});})(nbtns[i]);
function updateTab(name){showTab(name);window.scrollTo({top:0,behavior:'smooth'});}
function setStatus(m){q('status-msg').textContent=m;}
var _busy=0;
function busy(on){_busy+=on?1:-1;if(_busy<0)_busy=0;var s=q('spin');if(s)s.className='spin'+(_busy>0?'':' hidden');}
function badgeHtml(st){if(!st)return'\u2014';var cls='neutral';if(st==='MOMENTUM_GAINER')cls='gainer';else if(st==='STEALTH_BUILDING')cls='stealth';else if(st==='HIGH_VOLUME_STABLE')cls='stable';else if(st==='MOMENTUM_LOSER')cls='loser';else if(st==='LIQUIDITY_FADING')cls='fading';return '<span class="badge '+cls+'">'+st+'</span>';}
function capBadgeHtml(cap){if(!cap||cap==='UNKNOWN')return '<span class="badge neutral" title="Market cap unclassified">\u2014</span>';var cls='small';if(cap==='LARGE')cls='large';else if(cap==='MID')cls='mid';return '<a class="badge '+cls+'" data-cap="'+cap+'" title="Filter by '+cap+' cap tier">'+cap+'</a>';}
function secBadgeHtml(sec){if(!sec)return '\u2014';return '<a class="badge sec" data-sec="'+sec+'" title="Filter by '+sec+' sector">'+sec+'</a>';}
function renderInspectMeta(d){
  if(!d)return '';
  var sec=d.sector?secBadgeHtml(d.sector):'\u2014';
  var cap=d.cap_tier?capBadgeHtml(d.cap_tier):'\u2014';
  if(d.market_cap)cap+=' <span class="mut">('+fmt(d.market_cap,1)+'M NPR)</span>';
  var r52='\u2014';
  if(d.fifty_two_week_high!=null&&d.fifty_two_week_low!=null){
    r52='NRS '+fmt(d.fifty_two_week_low)+' \u2192 NRS '+fmt(d.fifty_two_week_high);
    if(d.distance_52w_high_pct!=null&&d.distance_52w_low_pct!=null){
      r52+=' <span class="mut">('+(d.distance_52w_high_pct>=0?'+':'')+d.distance_52w_high_pct.toFixed(1)+'% to High, '+(d.distance_52w_low_pct>=0?'+':'')+d.distance_52w_low_pct.toFixed(1)+'% to Low)</span>';
    }
    if(d.range_52w_pct!=null){
      r52+=' <span class="tag"><b>'+d.range_52w_pct.toFixed(1)+'%</b> within 52W range</span>';
    }
  }
  var vwapTxt=(d.vwap!=null)?('NRS '+fmt(d.vwap)):'\u2014';
  var rows=[
    ['Sector',sec],
    ['Market Cap Tier',cap],
    ['52-Week Channel',r52],
    ['Session VWAP',vwapTxt]
  ];
  var h='<table class="meta"><tbody>';
  for(var i=0;i<rows.length;i++)h+='<tr><th>'+rows[i][0]+'</th><td>'+rows[i][1]+'</td></tr>';
  return h+'</tbody></table>';
}
function renderMomentumCard(m,s,b){if(!m)return '<div class="empty">No momentum diagnostic available.</div>';var statusBadge=badgeHtml(m.status);var ratioTxt=(m.turnover_ratio!==null&&m.turnover_ratio!==undefined)?(m.turnover_ratio.toFixed(2)+'x'):'\u2014';if(m.percentile_turnover_ratio!==null&&m.percentile_turnover_ratio!==undefined){ratioTxt+=' <span class="mut">(Top '+(100.0-m.percentile_turnover_ratio).toFixed(1)+'% on NEPSE)</span>';}var driftTxt=(m.rank_drift!==null&&m.rank_drift!==undefined)?((m.rank_drift>0?'+':'')+m.rank_drift.toFixed(1)):'\u2014';if(m.percentile_rank_drift!==null&&m.percentile_rank_drift!==undefined){driftTxt+=' <span class="mut">(Top '+(100.0-m.percentile_rank_drift).toFixed(1)+'% drift)</span>';}var chgTxt=(m.price_change_pct_window!==null&&m.price_change_pct_window!==undefined)?((m.price_change_pct_window>0?'+':'')+m.price_change_pct_window.toFixed(2)+'%'):'\u2014';var closeTxt=(m.close!==null&&m.close!==undefined)?('NRS '+fmt(m.close)):'\u2014';var aiScoreTxt='\u2014';if(m.ai_confidence!==null&&m.ai_confidence!==undefined){var p=m.ai_confidence*100;var bCls=(p>70)?'gainer':(p>40?'stealth':'neutral');aiScoreTxt='<span class="badge '+bCls+'" style="font-weight:bold; font-size: 1.1em;">'+p.toFixed(1)+'% Breakout Probability</span>';}var rows=[['Status',statusBadge],['AI Confidence Score',aiScoreTxt],['Turnover Ratio ('+(s||5)+'D vs '+(b||22)+'D)',ratioTxt],['Daily Turnover (Mean)','Recent '+(s||5)+'D: '+(m.avg_turnover_short?('NRS '+fmt(m.avg_turnover_short)):'\u2014')+' | Baseline '+(b||22)+'D: '+(m.avg_turnover_base?('NRS '+fmt(m.avg_turnover_base)):'\u2014')],['Turnover Rank Progression','Baseline #'+m.avg_rank_base+' \u2192 Recent #'+m.avg_rank_short+' (Drift: '+driftTxt+')'],['Recent Window \u0394% / Close','<span class="'+pctCls(m.price_change_pct_window)+'">'+chgTxt+'</span> | '+closeTxt],['Dominant Brokers','Accumulator (Buyer): '+(m.top_accumulator?'<a class="broker" data-broker="'+m.top_accumulator+'">'+m.top_accumulator+'</a>':'\u2014')+' | Distributor (Seller): '+(m.top_distributor?'<a class="broker" data-broker="'+m.top_distributor+'">'+m.top_distributor+'</a>':'\u2014')]];var h='<table class="meta"><tbody>';for(var i=0;i<rows.length;i++)h+='<tr><th>'+rows[i][0]+'</th><td>'+rows[i][1]+'</td></tr>';return h+'</tbody></table>';}

function renderTable(rows,cols){if(!rows||!rows.length)return '<div class="empty">No data.</div>';
var h='<table><thead><tr>';for(var i=0;i<cols.length;i++){
var thCls=cols[i].cls?(' '+cols[i].cls):'';
var hasHelp=cols[i].desc?' has-help':'';
h+='<th class="'+(thCls+hasHelp).trim()+'"'+(cols[i].desc?' title="'+cols[i].desc+'"':'')+'>'+cols[i].label+'</th>';}
h+='</tr></thead><tbody>';
for(var r=0;r<rows.length;r++){var row=rows[r];h+='<tr>';for(var i=0;i<cols.length;i++){var c=cols[i],v=row[c.key],cls=c.cls?c.cls:'',td;
if(c.type==='num'){cls+=' num text-right';td=fmt(v);}
else if(c.type==='int'){cls+=' num text-right';td=fmtInt(v);}
else if(c.type==='int_flow'){cls+=' num text-right '+pctCls(v);td=(v>0?'+':'')+fmtInt(v);}
else if(c.type==='pct'){cls+=' num text-right '+pctCls(v);td=pct(v);}
else if(c.type==='pct_raw'){cls+=' num text-right';td=(v===null||v===undefined)?'\u2014':Number(v).toFixed(1)+'%';}
else if(c.type==='ai_score'){cls+=' text-center';if(v===null||v===undefined){td='\u2014';}else{var p=v*100;var bCls=(p>70)?'gainer':(p>40?'stealth':'neutral');td='<span class="badge '+bCls+'" style="font-weight:bold">'+p.toFixed(1)+'%</span>';}}
else if(c.type==='badge'){cls+=' text-center';td=badgeHtml(v);}
else if(c.type==='cap'){cls+=' text-center';td=capBadgeHtml(v);}
else if(c.type==='sec'){cls+=' text-center';td=secBadgeHtml(v);}
else if(c.type==='sym'){cls+=' text-center';td='<a class="sym" data-sym="'+v+'">'+v+'</a>';}
else if(c.type==='wsym'){cls+=' text-center';td='<a class="sym" data-ws="'+v+'">'+v+'</a>';}
else if(c.type==='broker'){cls+=' text-center';td='<a class="broker" data-broker="'+v+'">'+v+'</a>';}
else if(c.type==='verdict_lazy'){cls+=' text-center';td='<span class="badge neutral lazy-verdict" data-sym="'+row.symbol+'">Loading...</span>';}
else{td=(v===null||v===undefined)?'\u2014':v;}
h+='<td class="'+cls.trim()+'">'+td+'</td>';}
h+='</tr>';}
h+='</tbody></table>';return h;}
function bindClicks(container){if(!container)return;container.addEventListener('click',function(e){
var s=e.target.closest('a[data-sym]');if(s){q('insp-sym').value=s.dataset.sym;updateTab('inspect');loadInspect();return;}
var b=e.target.closest('a[data-broker]');if(b){q('brok-id').value=b.dataset.broker;updateTab('broker');loadBroker();return;}
var c=e.target.closest('a[data-cap]');if(c){var capVal=c.dataset.cap;if(q('top-cap'))q('top-cap').value=capVal;if(q('run-cap'))q('run-cap').value=capVal;updateTab('top');loadTop();return;}
var sec=e.target.closest('a[data-sec]');if(sec){var secVal=sec.dataset.sec;if(q('top-sector'))q('top-sector').value=secVal;if(q('run-sector'))q('run-sector').value=secVal;updateTab('top');loadTop();return;}
});}
function showMarketClosedBanner(j){
  var b = q('market-banner');
  if(!b){
    b = document.createElement('div');
    b.id = 'market-banner';
    b.style.cssText = 'background:#fff3cd; color:#856404; padding:10px 15px; margin:10px 0; border:1px solid #ffeeba; border-radius:4px; font-weight:bold;';
    var header = document.querySelector('header') || document.body;
    header.parentNode.insertBefore(b, header.nextSibling);
  }
  b.textContent = 'Market Closed / No Trading Session on ' + j.requested_date + ' \u2014 Latest available trading session is ' + j.latest_available + '.';
  b.style.display = 'block';
}
function hideMarketClosedBanner(){
  var b = q('market-banner');
  if(b) b.style.display = 'none';
}
function api(path){setStatus('Loading '+path+'\u2026');busy(true);return fetch('/api/'+path).then(function(r){if(!r.ok){return r.json().catch(function(){return {error:'HTTP '+r.status};}).then(function(err){throw new Error((err&&err.error)?err.error:('HTTP '+r.status));});}return r.json();}).then(function(j){if(j.market_closed){setStatus('Market Closed on '+j.requested_date+' (Latest: '+j.latest_available+')');showMarketClosedBanner(j);return null;}hideMarketClosedBanner();setStatus((j.cached?'cached: ':'computed: ')+path);return j.data;}).catch(function(e){setStatus('Error: '+e.message);return null;}).finally(function(){busy(false);});}
var TOP_COLS=[
  {key:'rank',label:'Rank',desc:'Turnover rank across the exchange (1 = highest turnover)'},
  {key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},
  {key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},
  {key:'sector',label:'Sector',type:'sec',desc:'NEPSE industry classification'},
  {key:'cap_tier',label:'Cap',type:'cap',desc:'Market cap tier: LARGE (>=20B), MID (5B-20B), SMALL (<5B)'},
  {key:'close',label:'Close',type:'num',desc:'Session closing price in NRS'},
  {key:'vwap',label:'VWAP',type:'num',desc:'Session volume-weighted average price'},
  {key:'change_pct',label:'Change %',type:'pct',desc:'Session close-to-close price change %'},
  {key:'qty',label:'Qty',type:'int',desc:'Total shares traded that session'},
  {key:'turnover',label:'Turnover',type:'num',desc:'Total session turnover in NRS'}
];
function loadLazyVerdicts(containerId){
  var root = containerId ? q(containerId) : document;
  if(!root) return;
  var badges = root.querySelectorAll('.lazy-verdict:not(.loaded)');
  badges.forEach(function(el){
    el.classList.add('loaded');
    var sym = el.dataset.sym;
    if(!sym) return;
    fetch('/api/position/' + sym)
      .then(function(res){return res.json();})
      .then(function(j){
        if(j && j.data && j.data.breakdown) {
          var bd = j.data.breakdown;
          var vClass = 'neutral';
          if(bd.verdict.indexOf('Strong Buy') !== -1) vClass = 'gainer';
          else if(bd.verdict.indexOf('Buy') !== -1) vClass = 'stealth';
          else if(bd.verdict.indexOf('Avoid') !== -1) vClass = 'loser';
          el.className = 'badge loaded ' + vClass;
          el.textContent = bd.verdict;
          el.title = bd.supply_price_action + ' | ' + bd.operator_intent + ' | ' + bd.verdict_detail;
        } else {
          el.textContent = 'N/A';
        }
      })
      .catch(function(){el.textContent = 'Error';});
  });
}

function loadTop(){
  var d=val('top-date'),l=val('top-limit')||20,s=val('top-sector'),c=val('top-cap');
  var url='top?as_of='+encodeURIComponent(d)+'&limit='+l;
  if(s)url+='&sector='+encodeURIComponent(s.trim());
  if(c)url+='&cap_tier='+encodeURIComponent(c.trim());
  api(url).then(function(data){
    if(!data)return;
    q('top-out').innerHTML=renderTable(data.rows,TOP_COLS);
    bindClicks(q('top-out'));
    loadLazyVerdicts('top-out');
  });
}
var RECENT_COLS=[
  {key:'trade_date',label:'Date',desc:'Trading session date'},
  {key:'close_price',label:'Close',type:'num',desc:'Session closing price in NRS'},
  {key:'change_pct',label:'Δ%',type:'pct',desc:'Session price change %'},
  {key:'qty',label:'Qty',type:'int',desc:'Total shares traded'},
  {key:'turnover',label:'Turnover',type:'num',desc:'Session turnover in NRS'},
  {key:'rank',label:'Rank',type:'num',desc:'Turnover rank (1 = most traded)'}
];
var BROKER_COLS=[
  {key:'broker_id',label:'Broker',type:'broker',desc:'NEPSE broker ID (click to inspect)'},
  {key:'net_1d',label:'Net 1D',type:'int_flow',desc:'Net shares bought (+) or sold (-) on latest session'},
  {key:'net_5d',label:'Net 5D',type:'int_flow',desc:'Net shares bought (+) or sold (-) over 5 sessions'},
  {key:'net_22d',label:'Net 22D',type:'int_flow',desc:'Net shares bought (+) or sold (-) over 22 sessions'},
  {key:'net_66d',label:'Net 66D',type:'int_flow',desc:'Net shares bought (+) or sold (-) over 66 sessions'},
  {key:'buy_vwap',label:'Buy VWAP',type:'num',desc:'Broker volume-weighted average buy price'},
  {key:'close',label:'Close',type:'num',desc:'Latest session closing price'},
  {key:'margin_pct',label:'Margin %',type:'pct',desc:'Broker unrealized profit/loss margin %'}
];
var SIG_COLS=[
  {key:'trade_date',label:'Date',desc:'Signal occurrence date'},
  {key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol'},
  {key:'broker_id',label:'Broker',type:'broker',desc:'Broker ID'},
  {key:'track',label:'Track',desc:'Detection track'},
  {key:'signal',label:'Signal',desc:'Signal classification'},
  {key:'turnover_rank',label:'Rank',type:'num',desc:'Turnover rank that session'},
  {key:'net_1d',label:'Net 1D',type:'int_flow',desc:'Net shares 1D'},
  {key:'net_5d',label:'Net 5D',type:'int_flow',desc:'Net shares 5D'},
  {key:'net_22d',label:'Net 22D',type:'int_flow',desc:'Net shares 22D'},
  {key:'net_66d',label:'Net 66D',type:'int_flow',desc:'Net shares 66D'},
  {key:'margin_pct',label:'Margin %',type:'pct',desc:'Margin %'},
  {key:'t1_change_pct',label:'T+1 Δ%',type:'pct',desc:'Forward T+1 price change %'}
];

function loadInspect() {
  var sym = val('insp-sym').trim().toUpperCase();
  if(!sym) return;
  
  var sess = val('insp-sess') || 22;
  
  Promise.all([
    api('inspect/'+sym+'?sessions='+sess),
    api('position/'+sym)
  ]).then(function(res) {
    var data = res[0];
    var posData = res[1];
    
    if(!data) return;
    
    // Position rendering
    if(posData && !posData.error && posData.context) {
      q('insp-pos-wrap').style.display='block';
      q('insp-pos-h').textContent='Positioning Verdict & Context \u2014 '+sym;
      var ctx=posData.context;
      var ss=posData.share_structure;
      var bd=posData.breakdown;
      
      var ctxHtml='<div class="stat-row" style="margin-bottom:0.75rem; font-size:0.88rem; color:var(--text);">' +
          '<span><strong>Tradable Float:</strong> ' + fmtInt(ss.public_shares) + ' (' + ss.public_ratio_pct + '%)</span> &middot; ' +
          '<span><strong>Promoter Shares:</strong> ' + fmtInt(ss.promoter_shares) + '</span> &middot; ' +
          '<span><strong>Float Turnover:</strong> ' + ss.float_turnover_pct + '%</span></div>';
          
      var rows=[
        {label: 'Price', val: fmt(ctx.ltp) + ' ('+pct(ctx.price_change_pct)+')'},
        {label: 'Day Range', val: fmt(ctx.day_low) + ' - ' + fmt(ctx.day_high) + ' (Rejection: ' + (ctx.upper_rejection*100).toFixed(0) + '%)'},
        {label: 'Total Qty / RVOL', val: fmtInt(ctx.total_qty) + ' / ' + ctx.rvol + 'x'},
        {label: 'Pressure Ratio', val: ctx.pressure_ratio + 'x'},
        {label: 'Top 3 Buy/Sell %', val: ctx.top_3_buy_pct + '% / ' + ctx.top_3_sell_pct + '%'},
        {label: 'Net Absorption Ratio', val: ctx.net_absorption_ratio + 'x'},
        {label: 'Wash %', val: ctx.wash_pct + '%'}
      ];
      var tHtml='<table class="meta"><tbody>';
      for(var i=0;i<rows.length;i++) tHtml+='<tr><th>'+rows[i].label+'</th><td>'+rows[i].val+'</td></tr>';
      tHtml+='</tbody></table>';
      q('insp-pos-context').innerHTML=ctxHtml + tHtml;
      
      var vClass = 'neutral';
      if(bd.verdict.indexOf('Strong Buy')!==-1) vClass='gainer';
      else if(bd.verdict.indexOf('Buy')!==-1) vClass='stealth';
      else if(bd.verdict.indexOf('Avoid')!==-1) vClass='loser';
      
      var bdHtml='<div class="concl"><div class="concl-head"><span class="badge '+vClass+'">'+bd.verdict+'</span></div>' +
          '<ul><li><strong>Supply &amp; Price Action:</strong> '+bd.supply_price_action+'</li>' +
          '<li><strong>Operator Intent:</strong> '+bd.operator_intent+'</li>' +
          '<li><strong>Actionable Verdict:</strong> '+bd.verdict_detail+'</li></ul></div>';
      q('insp-pos-breakdown').innerHTML=bdHtml;
    } else {
      q('insp-pos-wrap').style.display='none';
    }

    // Existing inspect rendering
    q('recent-h').textContent='Daily History \u2014 '+sym;
    
    if(data.sector||data.cap_tier||data.fifty_two_week_high!=null||data.vwap!=null){
      q('insp-meta-wrap').style.display='block';
      q('insp-meta').innerHTML=renderInspectMeta(data);
    } else {
      q('insp-meta-wrap').style.display='none';
    }
    
    if(data.momentum){
      q('insp-mom-wrap').style.display='block';
      q('insp-mom').innerHTML=renderMomentumCard(data.momentum,5,22);
      bindClicks(q('insp-mom'));
    } else {
      q('insp-mom-wrap').style.display='none';
    }
    
    if(q('insp-snap-wrap')){
      try {
        if (typeof renderInspectHoldingsSnapshot === 'function') {
          var snapHtml=renderInspectHoldingsSnapshot(data);
          if(snapHtml){
            q('insp-snap-wrap').style.display='block';
            q('insp-snap').innerHTML=snapHtml;
            bindClicks(q('insp-snap'));
          }else{
            q('insp-snap-wrap').style.display='none';
          }
        } else {
          q('insp-snap-wrap').style.display='none';
        }
      } catch (e) {
        console.error('Error rendering snapshot:', e);
        q('insp-snap-wrap').style.display='none';
      }
    }
    
    if(q('insp-accum-wrap')){
      var acc=data.accumulators||(data.brokers||[]).filter(function(b){return (b.net_22d||0)>0;});
      if(acc.length){
        q('insp-accum-wrap').style.display='block';
        q('insp-accum-brokers').innerHTML=renderTable(acc.slice(0,5),BROKER_COLS);
        bindClicks(q('insp-accum-brokers'));
      }else{
        q('insp-accum-wrap').style.display='none';
      }
    }
    
    if(q('insp-dist-wrap')){
      var dst=data.distributors||(data.brokers||[]).filter(function(b){return (b.net_22d||0)<0;}).sort(function(a,b){return (a.net_22d||0)-(b.net_22d||0);});
      if(dst.length){
        q('insp-dist-wrap').style.display='block';
        q('insp-dist-brokers').innerHTML=renderTable(dst.slice(0,5),BROKER_COLS);
        bindClicks(q('insp-dist-brokers'));
      }else{
        q('insp-dist-wrap').style.display='none';
      }
    }
    
    q('insp-recent').innerHTML=renderTable(data.recent,RECENT_COLS);
    q('insp-brokers').innerHTML=renderTable(data.brokers,BROKER_COLS);
    q('insp-signals').innerHTML=renderTable(data.signals,SIG_COLS);
    
    bindClicks(q('insp-recent'));
    bindClicks(q('insp-brokers'));
    bindClicks(q('insp-signals'));
    loadLazyVerdicts('view-inspect');
  });
}
var MARKET_COLS=[{key:'symbol',label:'Symbol',type:'sym'},{key:'ltp',label:'Price',type:'num'},{key:'verdict',label:'Verdict'},{key:'score',label:'Score',type:'num'},{key:'top_accum',label:'Top Accumulator'},{key:'top_dist',label:'Top Distributor'},{key:'net_abs',label:'Absorption'},{key:'wash_pct',label:'Wash %',type:'pct'}];
function loadMarket(){
  q('market-out').innerHTML='<div class="empty">Scanning market... Please wait.</div>';
  api('market').then(function(data){
    if(!data || !data.market){ q('market-out').innerHTML='<div class="empty neg">Failed to load market data.</div>'; return; }
    
    var latest_date = data.market.length > 0 ? data.market[0].trade_date : null;
    if(latest_date) {
      q('market-h').innerHTML = 'Categorized Market Screener <span class="badge neutral" style="margin-left:10px;">Data as of ' + latest_date + '</span>';
    }
    
    var rows = data.market.map(function(item){
      var ctx = item.context || {};
      var bd = item.breakdown || {};
      var accum = (ctx.top_buyer_broker ? 'Broker ' + ctx.top_buyer_broker : 'None');
      var dist = (ctx.top_seller_broker ? 'Broker ' + ctx.top_seller_broker : 'None');
      return {
        symbol: item.symbol,
        ltp: ctx.ltp,
        verdict: bd.verdict,
        score: bd.score,
        top_accum: accum,
        top_dist: dist,
        net_abs: ctx.net_absorption_ratio + 'x',
        wash_pct: ctx.wash_pct
      };
    });
    
    var sortedRows = rows.sort(function(a, b){
      if(b.score !== a.score) return b.score - a.score;
      return a.symbol.localeCompare(b.symbol);
    });
    
    var h = '<div style="display:flex; flex-direction:column; gap:2rem;">';
    
    var groups = [
      { name: 'Strong Buy', match: 'Strong Buy', cls: 'gainer' },
      { name: 'Buy / Stealth', match: 'Buy', cls: 'stealth' },
      { name: 'Hold / Neutral', match: 'Hold', cls: 'neutral' },
      { name: 'Avoid / Distribution', match: 'Avoid', cls: 'loser' }
    ];
    
    groups.forEach(function(g){
      var filterRows = sortedRows.filter(function(r){
        if(g.match === 'Buy') return r.verdict.indexOf('Buy') !== -1 && r.verdict.indexOf('Strong Buy') === -1;
        return r.verdict.indexOf(g.match) !== -1;
      });
      if(filterRows.length > 0){
        h += '<div><h4 style="margin-bottom:0.5rem;"><span class="badge ' + g.cls + '">' + g.name + ' (' + filterRows.length + ')</span></h4>';
        h += renderTable(filterRows, MARKET_COLS);
        h += '</div>';
      }
    });
    h += '</div>';
    
    q('market-out').innerHTML = h;
    bindClicks(q('market-out'));
  });
}
var SECTOR_COLS=[{key:'sector',label:'Sector'},{key:'total_turnover',label:'Total Turnover (NRS)',type:'num'},{key:'avg_price_change',label:'Avg Change %',type:'pct'},{key:'num_stocks',label:'Active Stocks',type:'int'},{key:'top_accum',label:'Top Accumulator'}];
function loadSector(){
  q('sector-out').innerHTML='<div class="empty">Mapping sector rotation...</div>';
  api('sector').then(function(data){
    if(!data || !data.sectors){ q('sector-out').innerHTML='<div class="empty neg">Failed to load sector data.</div>'; return; }
    
    var rows = data.sectors.map(function(item){
      return {
        sector: item.sector,
        total_turnover: item.total_turnover,
        avg_price_change: item.avg_price_change,
        num_stocks: item.num_stocks,
        top_accum: (item.broker_id ? 'Broker ' + item.broker_id + ' <span class="pos">(+' + fmtInt(item.net_qty) + ')</span>' : 'None')
      };
    });
    
    var h = '<div style="display:flex; flex-direction:column; gap:2rem;">';
    h += '<div><h4 style="margin-bottom:0.5rem;"><span class="badge neutral">Macro Flow Rankings</span></h4>';
    h += renderTable(rows, SECTOR_COLS);
    h += '</div></div>';
    
    q('sector-out').innerHTML = h;
  });
}
var SYNDICATE_COLS=[{key:'broker_a',label:'Broker A'},{key:'broker_b',label:'Broker B'},{key:'co_occurrences',label:'Co-accumulations',type:'int'},{key:'symbols',label:'Symbols Hunted Together'}];
function loadSyndicate(){
  q('syndicate-out').innerHTML='<div class="empty">Running clustering algorithm...</div>';
  api('syndicate').then(function(data){
    if(!data || !data.syndicates){ q('syndicate-out').innerHTML='<div class="empty neg">Failed to detect syndicates.</div>'; return; }
    
    var rows = data.syndicates.map(function(item){
      return {
        broker_a: '<a class="broker" data-broker="'+item.broker_a+'">Broker '+item.broker_a+'</a>',
        broker_b: '<a class="broker" data-broker="'+item.broker_b+'">Broker '+item.broker_b+'</a>',
        co_occurrences: item.co_occurrences,
        symbols: item.symbols.join(', ')
      };
    });
    
    q('syndicate-out').innerHTML = renderTable(rows, SYNDICATE_COLS);
    bindClicks(q('syndicate-out'));
  });
}
var BACKTEST_COLS=[{key:'signal',label:'Strategy Signal'},{key:'count',label:'Total Trades',type:'int'},{key:'t5_win',label:'T+5 Win %',type:'pct'},{key:'t5_avg',label:'T+5 Avg %',type:'pct'},{key:'t20_win',label:'T+20 Win %',type:'pct'},{key:'t20_avg',label:'T+20 Avg %',type:'pct'}];
function loadBacktest(){
  q('backtest-out').innerHTML='<div class="empty">Calculating historical performance...</div>';
  api('backtest').then(function(data){
    if(!data || !data.backtest){ q('backtest-out').innerHTML='<div class="empty neg">Failed to run backtest.</div>'; return; }
    q('backtest-out').innerHTML = renderTable(data.backtest, BACKTEST_COLS);
  });
}
var ANL_TIME_COLS=[{key:'trade_date',label:'Date',desc:'Trading session date'},{key:'rank',label:'Rank',desc:'Turnover rank that session; 1 = most traded on the whole exchange'},{key:'rank_pctile',label:'Rank %ile',type:'num',desc:'rank \u00f7 symbols traded that day; lower = busier'},{key:'close',label:'Close',type:'num',desc:'Session closing price'},{key:'change_pct',label:'Change %',type:'pct',desc:'Session close-to-close price change'},{key:'qty',label:'Qty',type:'int',desc:'Shares traded that session'},{key:'turnover',label:'Turnover',type:'num',desc:'NRS turnover that session'},{key:'top_accum_id',label:'Top Accum',type:'broker',desc:'Broker that net-bought the most that day (click to open the Broker tab)'},{key:'top_accum_net',label:'Top Net',type:'int',desc:"That broker's net buy in shares (buy \u2212 sell)"},{key:'net_breadth',label:'Breadth',type:'int',desc:'How many brokers were net buyers that session'},{key:'concentration',label:'Conc',type:'num',desc:"|top buyer's net| \u00f7 sum of all brokers' |nets|; low = no single dominant buyer"},{key:'sustained',label:'Sust',desc:'Is the top-accumulator broker the same as the previous session?'},{key:'signature',label:'Signature',desc:'Crowd type: MULTI = \u22653 net buyers \u00b7 SINGLE = 1 net buyer \u00b7 DISTRIBUTE = top netter sold \u00b7 NEUTRAL = unclear'},{key:'fwd_1',label:'T+1',type:'pct',desc:'Forward close-to-close return 1 session later'},{key:'fwd_3',label:'T+3',type:'pct',desc:'Forward close-to-close return 3 sessions later'}];
var ANL_REL_COLS=[{key:'bucket',label:'Bucket',desc:"Turnover tier by rank %ile: LEADER = top 15%, MID = 15\u201350%, MINOR = the rest"},{key:'n',label:'N',type:'int',desc:'Number of sessions in the bucket'},{key:'t1_avg',label:'T+1 Avg %',type:'pct',desc:'Average close-to-close return 1 session later'},{key:'t1_win',label:'T+1 Win %',type:'pct',desc:'Share of sessions with a positive T+1 return'},{key:'t3_avg',label:'T+3 Avg %',type:'pct',desc:'Average close-to-close return 3 sessions later'},{key:'t3_win',label:'T+3 Win %',type:'pct',desc:'Share of sessions with a positive T+3 return'}];
var ANL_SIG_COLS=[{key:'signature',label:'Signature',desc:'Crowd type: MULTI = \u22653 net buyers \u00b7 SINGLE = 1 \u00b7 DISTRIBUTE = top netter sold \u00b7 NEUTRAL = unclear'},{key:'n',label:'Sessions',type:'int',desc:'Number of sessions with this signature in the window'},{key:'t1_avg',label:'T+1 Avg %',type:'pct',desc:'Average close-to-close return 1 session later'},{key:'t1_win',label:'T+1 Win %',type:'pct',desc:'Share of sessions with a positive T+1 return'},{key:'t3_avg',label:'T+3 Avg %',type:'pct',desc:'Average close-to-close return 3 sessions later'},{key:'t3_win',label:'T+3 Win %',type:'pct',desc:'Share of sessions with a positive T+3 return'}];
var ANL_PRED_COLS=[{key:'horizon',label:'Horizon',desc:'Forward horizon in sessions (T+1 = next session)'},{key:'signature',label:'Signature',desc:'Crowd type of the latest predictable session'},{key:'as_of',label:'As Of',desc:'Latest session with a full forward window'},{key:'bias',label:'Bias',desc:'Direction from the historical average return of this signature'},{key:'confidence',label:'Conf',desc:'Sample-size confidence: LOW <3 \u00b7 MEDIUM <8 \u00b7 HIGH \u22658 (not edge strength)'},{key:'n',label:'N',type:'int',desc:'Historical sample count for this signature'},{key:'hist_avg_return_pct',label:'Hist Avg %',type:'pct',desc:'Historical average forward return for this signature'},{key:'hist_win_rate_pct',label:'Hist Win %',type:'pct',desc:'Historical share of positive forward returns'},{key:'fwd_actual_pct',label:'Actual %',type:'pct',desc:'Realised return from the As-Of session (validation)'}];
function anlBucketRows(rel){if(!rel||!rel.buckets)return [];return rel.buckets.map(function(b){var t1=b.fwd_1||{},t3=b.fwd_3||{};return {bucket:b.bucket,n:b.n,t1_avg:t1.avg_return_pct,t1_win:t1.win_rate_pct,t3_avg:t3.avg_return_pct,t3_win:t3.win_rate_pct};});}
function anlRelation(rel){if(!rel)return '<div class="empty">No data.</div>';var s1=rel.spearman_rank_t1_return,s2=rel.spearman_rank_close;var h='<div class="mut" style="margin-bottom:.5rem">Rank &harr; T+1 return r='+((s1===null||s1===undefined)?'\u2014':s1.toFixed(3))+' &middot; rank &harr; close r='+((s2===null||s2===undefined)?'\u2014':s2.toFixed(3))+' &middot; n='+rel.n+'</div>';return h+renderTable(anlBucketRows(rel),ANL_REL_COLS);}
function anlSigPerf(perf){if(!perf)return [];return ['MULTI','SINGLE','DISTRIBUTE','NEUTRAL'].map(function(sig){var g=perf[sig];if(!g)return null;var h1=g.horizons[1]||{},h3=g.horizons[3]||{};return {signature:sig,n:g.samples,t1_avg:h1.avg_return_pct,t1_win:h1.win_rate_pct,t3_avg:h3.avg_return_pct,t3_win:h3.win_rate_pct};}).filter(function(r){return r;});}
function anlLongestRun(rows,key,val){var best=0,run=0,last=null;for(var i=0;i<rows.length;i++){if(rows[i][key]===val){run++;if(run>best){best=run;last=rows[i];}}else{run=0;}}return best>=2?{len:best,last:last}:{len:0,last:null};}
function anlConclTakeaway(dom,run,lb,ph){var bull=0;if(dom==='MULTI')bull++;if(run.len>=2)bull++;if(lb&&lb.fwd_1&&lb.fwd_1.avg_return_pct>0)bull++;if(ph&&ph.bias==='BULLISH')bull++;if(bull>=3)return 'Bullish-leaning accumulation setup, but the predictive edge is modest and volatile \u2014 better as a watch/lean candidate than a high-probability entry.';if(bull>=2)return 'Mildly constructive accumulation with a modest historical edge \u2014 a watch candidate; the edge is not strong enough for high-confidence entries.';if(bull>=1)return 'Broad accumulation but with only a weak, inconsistent historical edge \u2014 keep on the radar, no high-conviction lean.';return 'Early read: not enough consistent signal \u2014 treat as neutral until accumulation broadens or strengthens.';}
function anlConclusion(data){if(!data||data.error)return '<div class="empty">'+(data&&data.error?'No data for this symbol.':'No data.')+'</div>';var h='<div class="concl">';var pred=data.prediction||[],ph=pred[pred.length-1];var head=ph?(ph.bias+' / '+ph.confidence+' \u00b7 T+'+ph.horizon):'Not enough data to project.';h+='<p class="concl-head">Net read: <b>'+head+'</b></p><ul>';var perf=data.signature_perf||{},tot=0,dom=null,domN=-1,sigs=['MULTI','SINGLE','DISTRIBUTE','NEUTRAL'];for(var i=0;i<sigs.length;i++){var g=perf[sigs[i]];if(g){tot+=g.samples;if(g.samples>domN){domN=g.samples;dom=sigs[i];}}}if(dom){h+='<li>Dominant crowd: <b>'+dom+'</b> ('+domN+'/'+tot+' sessions).'+(perf.DISTRIBUTE&&perf.DISTRIBUTE.samples===0?' No net-selling (DISTRIBUTE) session in the window \u2014 buying held up even on down days.':'')+'</li>';}var run=anlLongestRun(data.timeline||[],'sustained',true);if(run.len>=2&&run.last){h+='<li>Broker <b>'+run.last.top_accum_id+'</b> was the dominant net buyer for <b>'+run.len+'</b> consecutive sessions (sticky, repeated accumulation).</li>';}var rel=data.rank_relation,lb=null;if(rel&&rel.buckets){for(var j=0;j<rel.buckets.length;j++){if(rel.buckets[j].bucket==='LEADER'){lb=rel.buckets[j];break;}}}if(lb&&lb.fwd_1&&lb.fwd_1.avg_return_pct!==null&&lb.fwd_1.avg_return_pct!==undefined){h+='<li>Top-turnover (LEADER) days averaged <b>'+pct(lb.fwd_1.avg_return_pct)+'</b> next session with a <b>'+fmt(lb.fwd_1.win_rate_pct,2)+'%</b> win rate \u2014 the edge has come from high-turnover sessions.</li>';}if(ph&&ph.hist_win_rate_pct!==null&&ph.hist_win_rate_pct!==undefined){h+='<li>Caveat: historical win rate is only <b>'+fmt(ph.hist_win_rate_pct,2)+'%</b> over <b>'+ph.n+'</b> samples \u2014 the edge is modest and volatile; HIGH confidence is sample-size based, not edge strength.</li>';}h+='</ul><p class="concl-tail">'+anlConclTakeaway(dom,run,lb,ph)+'</p></div>';return h;}
function loadAnalyze(){var sym=val('anl-sym').trim().toUpperCase();if(!sym)return;api('analyze/'+sym+'?sessions='+(val('anl-sess')||30)).then(function(data){if(!data)return;if(data.error){q('anl-timeline').innerHTML='<div class="empty">'+data.error+'</div>';q('anl-conclusion').innerHTML='<div class="empty">'+data.error+'</div>';return;}q('anl-h').textContent='Timeline \u2014 '+sym;q('anl-timeline').innerHTML=renderTable(data.timeline,ANL_TIME_COLS);q('anl-relation').innerHTML=anlRelation(data.rank_relation);q('anl-sigperf').innerHTML=renderTable(anlSigPerf(data.signature_perf),ANL_SIG_COLS);q('anl-prediction').innerHTML=renderTable(data.prediction,ANL_PRED_COLS);bindClicks(q('anl-timeline'));q('anl-conclusion').innerHTML=anlConclusion(data);});}
var HOLD_COLS=[{key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},{key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},{key:'net_t1',label:'Net T1',type:'int_flow',desc:'Net shares bought (+) or sold (-) on latest session'},{key:'net_t5',label:'Net T5',type:'int_flow',desc:'Net shares bought (+) or sold (-) over last 5 sessions'},{key:'net_t22',label:'Net T22',type:'int_flow',desc:'Net shares bought (+) or sold (-) over last 22 sessions'},{key:'net_t66',label:'Net T66',type:'int_flow',desc:'Net shares bought (+) or sold (-) over last 66 sessions'},{key:'margin_pct',label:'Margin %',type:'pct',desc:'Unrealized profit/loss margin vs latest close price'}];
function loadBroker(){var id=val('brok-id');if(!id)return;api('broker/'+id+'?top='+(val('brok-top')||5)+'&sessions='+(val('brok-sess')||66)).then(function(data){if(!data)return;q('brok-h').textContent='Broker '+id+' \u2014 Top Accumulations (Net Buying)';q('brok-out').innerHTML=renderTable(data.accumulations||data.holdings,HOLD_COLS);bindClicks(q('brok-out'));loadLazyVerdicts('brok-out');if(q('brok-dist-out')){q('brok-dist-h').textContent='Broker '+id+' \u2014 Top Distributions (Net Selling / Dumping)';q('brok-dist-out').innerHTML=renderTable(data.distributions||[],HOLD_COLS);bindClicks(q('brok-dist-out'));loadLazyVerdicts('brok-dist-out');}});}
var MOM_COLS=[{key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},{key:'status',label:'Status',type:'badge',desc:'Momentum classification: STEALTH_BUILDING, MOMENTUM_GAINER, etc.'},{key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},{key:'avg_rank_base',label:'Avg Rank (Base)',type:'num',desc:'Average daily turnover rank over baseline window (1 = most traded)'},{key:'avg_rank_short',label:'Avg Rank (Short)',type:'num',desc:'Average daily turnover rank over recent window (1 = most traded)'},{key:'rank_drift',label:'Rank Drift',type:'num',desc:'Avg Base Rank − Avg Recent Rank (positive = climbed the turnover board)'},{key:'turnover_ratio',label:'Turnover Ratio',type:'num',desc:'Recent average daily turnover ÷ baseline average (e.g. 2.0 = twice as liquid recently)'},{key:'close',label:'Close',type:'num',desc:'Latest session closing price in NRS'},{key:'price_change_pct_window',label:'Wnd Change %',type:'pct',desc:'Close price change % across the recent window'},{key:'top_accumulator',label:'Top Accum',type:'broker',desc:'Broker with largest net buy in recent window (click to inspect)'},{key:'top_distributor',label:'Top Distrib',type:'broker',desc:'Broker with largest net sell in recent window (click to inspect)'}];
var MOM_SIMILAR_COLS=[{key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},{key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},{key:'similarity_pct',label:'Match %',type:'pct_raw',desc:'Similarity score based on normalized momentum profile'},{key:'status',label:'Status',type:'badge',desc:'Standardized momentum state classification'},{key:'turnover_ratio',label:'Turnover Ratio',type:'num',desc:'Recent average daily turnover ÷ baseline average'},{key:'rank_drift',label:'Rank Drift',type:'num',desc:'Avg Base Rank − Avg Recent Rank (positive = climbed the board)'},{key:'avg_rank_short',label:'Avg Rank (Short)',type:'num',desc:'Average daily turnover rank over recent window'},{key:'price_change_pct_window',label:'Wnd Change %',type:'pct',desc:'Close price change % across recent window'},{key:'top_accumulator',label:'Top Accum',type:'broker',desc:'Broker with largest net buy in recent window'},{key:'top_distributor',label:'Top Distrib',type:'broker',desc:'Broker with largest net sell in recent window'}];
function loadMomentum(){var sym=(val('mom-sym')||'').trim().toUpperCase();var s=val('mom-short')||5;var b=val('mom-base')||22;var asof=val('mom-asof');var url='momentum?short='+s+'&base='+b+'&as_of='+asof;if(sym)url+='&symbol='+encodeURIComponent(sym);api(url).then(function(data){if(!data)return;if(sym&&data.symbol_momentum){q('mom-single-wrap').style.display='block';q('mom-single').innerHTML=renderMomentumCard(data.symbol_momentum,s,b);q('mom-similar-wrap').style.display='block';q('mom-similar').innerHTML=renderTable(data.similar,MOM_SIMILAR_COLS);bindClicks(q('mom-single'));bindClicks(q('mom-similar'));}else{q('mom-single-wrap').style.display='none';q('mom-similar-wrap').style.display='none';}q('mom-gain').innerHTML=renderTable(data.gainers,MOM_COLS);q('mom-los').innerHTML=renderTable(data.losers,MOM_COLS);bindClicks(q('mom-gain'));bindClicks(q('mom-los'));loadLazyVerdicts('view-momentum');});}
var WASH_BROKER=[{key:'broker_id',label:'Broker',type:'broker',desc:'NEPSE broker ID (click to inspect)'},{key:'buy_qty',label:'Buy',type:'int',desc:'Total shares bought by this broker'},{key:'sell_qty',label:'Sell',type:'int',desc:'Total shares sold by this broker'},{key:'matched_qty',label:'Matched',type:'int',desc:'Quantity matched internally (min(buy, sell))'},{key:'gross_volume',label:'Gross',type:'int',desc:'Total volume (buy + sell)'},{key:'match_pct',label:'Match %',type:'pct',desc:'Internal cross percentage (matched \u00d7 2 \u00f7 gross)'}];
var WASH_SESSION=[{key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},{key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},{key:'total_qty',label:'Total Qty',type:'int',desc:'Total shares traded across the exchange in lookback'},{key:'crossed_qty',label:'Crossed',type:'int',desc:'Total internally matched shares across all brokers'},{key:'session_match_pct',label:'Match %',type:'pct',desc:'Percentage of exchange volume internally crossed'}];
function loadWash(){api('wash?window='+(val('wash-wnd')||22)+'&min_qty='+(val('wash-mq')||5000)+'&as_of='+val('wash-asof')).then(function(data){if(!data)return;q('wash-broker').innerHTML=renderTable(data.broker,WASH_BROKER);q('wash-session').innerHTML=renderTable(data.session,WASH_SESSION);bindClicks(q('wash-broker'));bindClicks(q('wash-session'));loadLazyVerdicts('wash-session');});}
var TRACK_A_COLS=[
  {key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},
  {key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},
  {key:'sector',label:'Sector',type:'sec',desc:'NEPSE industry classification'},
  {key:'cap_tier',label:'Cap',type:'cap',desc:'Market cap tier: LARGE, MID, SMALL'},
  {key:'signal',label:'Signal',desc:'Track A signal: MOMENTUM_ENTRY, TOP_BUYER_ABSORPTION, or RETAIL_DISTRIBUTION_TRAP'},
  {key:'close',label:'Close',type:'num',desc:'Session closing price in NRS'},
  {key:'net_t1',label:'Net T1',type:'int',desc:'Net shares bought (+) or sold (-) on latest session'},
  {key:'net_t5',label:'Net T5',type:'int',desc:'Net shares bought (+) or sold (-) over last 5 sessions'},
  {key:'net_t22',label:'Net T22',type:'int',desc:'Net shares bought (+) or sold (-) over last 22 sessions'},
  {key:'net_t66',label:'Net T66',type:'int',desc:'Net shares bought (+) or sold (-) over last 66 sessions'},
  {key:'margin_pct',label:'Margin %',type:'pct',desc:'Top buyer unrealized margin % vs current close'},
  {key:'ai_confidence',label:'AI Score',type:'ai_score',desc:'ML probability of breakout (>3% in 5 days)'},
  {key:'t1_turnover',label:'T1 Turnover',type:'num',desc:'Latest session turnover in NRS'}
];
var TRACK_B_COLS=[
  {key:'symbol',label:'Symbol',type:'sym',desc:'NEPSE ticker symbol (click to inspect)'},
  {key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},
  {key:'sector',label:'Sector',type:'sec',desc:'NEPSE industry classification'},
  {key:'cap_tier',label:'Cap',type:'cap',desc:'Market cap tier: LARGE, MID, SMALL'},
  {key:'signal',label:'Signal',desc:'Track B signal: STEALTH_ACCUMULATION_BASE or EARLY_PIVOT_ACCUMULATION'},
  {key:'broker_id',label:'Broker',type:'broker',desc:'Primary accumulating broker ID (click to inspect)'},
  {key:'close',label:'Close',type:'num',desc:'Session closing price in NRS'},
  {key:'net_t22',label:'Net T22',type:'int',desc:'Net shares accumulated by broker over last 22 sessions'},
  {key:'absorption_pct',label:'Absorb %',type:'pct',desc:'Broker net buy as % of total market volume in 22 sessions'},
  {key:'dispersion_pct',label:'Dispers %',type:'pct',desc:'Selling dispersion across top 3 sellers'},
  {key:'t22_price_change_pct',label:'22D Δ%',type:'pct',desc:'Price change % over the 22-session accumulation window'},
  {key:'volume_inflection',label:'Vol Inflect',type:'num',desc:'5-day average volume ÷ 22-day average volume'},
  {key:'margin_pct',label:'Margin %',type:'pct',desc:'Broker unrealized profit/loss margin % vs current close'},
  {key:'ai_confidence',label:'AI Score',type:'ai_score',desc:'ML probability of breakout (>3% in 5 days)'}
];

function loadSmartmoney(){
  setStatus('Triggering AI pipeline...');
  var url='smartmoney';
  var asof=val('sm-asof');
  if(asof)url+='?as_of='+asof;
  api(url).then(function(data){
    if(!data){ q('sm-out').innerHTML='<div class="empty">Pipeline failed.</div>'; return; }
    if(data.error) { q('sm-out').innerHTML='<div class="empty">Error: '+data.error+'</div>'; return; }
    if(data.message) { q('sm-out').innerHTML='<div class="empty">'+data.message+'</div>'; return; }
    var html = '';
    for(var i=0; i<data.brokers.length; i++) {
        var b = data.brokers[i];
        html += '<div class="concl" style="margin-bottom:1rem;"><h4 class="concl-head">' + b.stock_symbol + ' - Broker ' + b.broker_id + ' (' + b.broker_name + ')</h4>';
        html += '<p><strong>Net Buy:</strong> ' + b.net_qty + '</p>';
        html += '<p style="color:var(--text); white-space:pre-wrap;">' + b.ai_insight + '</p></div>';
    }
    q('sm-out').innerHTML = html;
  });
}

function loadRun(){
  var s=val('run-sector'),c=val('run-cap');
  var url='run?top_n='+(val('run-top')||20)+'&holdings_sessions='+(val('run-holder')||22);
  if(s)url+='&sector='+encodeURIComponent(s.trim());
  if(c)url+='&cap_tier='+encodeURIComponent(c.trim());
  api(url).then(function(data){
    if(!data)return;
    q('run-a').innerHTML=renderTable(data.track_a,TRACK_A_COLS);
    q('run-b').innerHTML=renderTable(data.track_b,TRACK_B_COLS);
    bindClicks(q('run-a'));
    bindClicks(q('run-b'));
    loadLazyVerdicts('view-run');
  });
}
function loadSignals(){var p=[];if(val('sig-sym'))p.push('symbol='+encodeURIComponent(val('sig-sym').trim().toUpperCase()));if(val('sig-broker'))p.push('broker='+val('sig-broker'));if(val('sig-signal'))p.push('signal='+encodeURIComponent(val('sig-signal')));if(val('sig-track'))p.push('track='+encodeURIComponent(val('sig-track')));if(val('sig-streak'))p.push('streak='+val('sig-streak'));p.push('limit='+(val('sig-limit')||100));api('signals?'+p.join('&')).then(function(data){if(!data)return;if(val('sig-streak'))q('sig-out').innerHTML=renderTable(data.streaks,SIG_COLS);else q('sig-out').innerHTML=renderTable(data.rows,SIG_COLS);bindClicks(q('sig-out'));loadLazyVerdicts('sig-out');});}
var WL_COLS=[{key:'symbol',label:'Symbol',type:'wsym',desc:'Watchlist symbol (click to view details)'},{key:'verdict',label:'Verdict',type:'verdict_lazy',desc:'Long-only positional verdict (hover for breakdown)'},{key:'status',label:'Status',desc:'Watchlist status: WATCHING, STALKING, ENTERED, EXITED, ARCHIVED'},{key:'close_price',label:'Price',type:'num',desc:'Latest session closing price in NRS'},{key:'price_change_pct',label:'Chg %',type:'pct',desc:'Latest session price change %'},{key:'turnover_rank',label:'Rank',desc:'Latest session turnover rank (1 = most traded)'},{key:'entry_price',label:'Entry',type:'num',desc:'Planned or executed entry price'},{key:'target_price',label:'Target',type:'num',desc:'Price target in NRS'},{key:'stop_price',label:'Stop',type:'num',desc:'Stop loss price in NRS'},{key:'outcome',label:'Outcome',desc:'Recorded trade outcome: WIN, LOSS, SCRATCH, EXPIRED'},{key:'added_date',label:'Added',desc:'Date symbol was added to watchlist'},{key:'updated_date',label:'Updated',desc:'Date of last status or note update'}];
var WL_NOTE_COLS=[{key:'note_date',label:'Date'},{key:'note',label:'Note'}];
function renderWatchMeta(m){var rows=[['Status',m.status],['Thesis',m.thesis],['Tags',m.tags],['Entry',(m.entry_date||'—')+' @ '+(m.entry_price===null||m.entry_price===undefined?'—':m.entry_price)],['Target',m.target_price],['Stop',m.stop_price],['Quantity',m.quantity],['Exit',(m.exit_date||'—')+' @ '+(m.exit_price===null||m.exit_price===undefined?'—':m.exit_price)],['Outcome',m.outcome],['Added',m.added_date],['Updated',m.updated_date]];var h='<table class="meta"><tbody>';for(var i=0;i<rows.length;i++)h+='<tr><th>'+rows[i][0]+'</th><td>'+(rows[i][1]===null||rows[i][1]===undefined||rows[i][1]===''?'—':rows[i][1])+'</td></tr>';return h+'</tbody></table>';}
function bindWatchClicks(container){if(!container)return;container.addEventListener('click',function(e){var s=e.target.closest('a[data-ws]');if(s)loadWatchDetail(s.dataset.ws);});}
function bindWatchActions(container){if(!container)return;container.addEventListener('click',function(e){var b=e.target.closest('button[data-wl]');if(!b)return;var sym=b.dataset.sym,act=b.dataset.wl;if(act==='note')wlNote(sym);else if(act==='enter')wlEnter(sym);else if(act==='close')wlExit(sym);else if(act==='archive')wlArchive(sym);});}
function renderWatchlist(rows){
  if(!rows||!rows.length){q('wl-list').innerHTML='<div class="empty">No watchlist items yet. Add one above.</div>';return;}
  var h='<table><thead><tr>';for(var i=0;i<WL_COLS.length;i++)h+='<th>'+WL_COLS[i].label+'</th>';h+='<th>Actions</th></tr></thead><tbody>';
  for(var r=0;r<rows.length;r++){var row=rows[r];h+='<tr>';
    for(var i=0;i<WL_COLS.length;i++){var c=WL_COLS[i],v=row[c.key],cls='';
      if(c.type==='wsym'){h+='<td><a class="sym" data-ws="'+v+'">'+v+'</a></td>';continue;}
      if(c.type==='num'){cls='num';v=fmt(v);}
      else if(c.type==='int'){cls='num';v=fmtInt(v);}
      else if(c.type==='pct'){cls='num '+pctCls(v);v=pct(v);}
      else if(v===null||v===undefined){v='\u2014';}
      h+='<td class="'+cls+'">'+v+'</td>';}
    var s=row.symbol;
    var a=(row.outcome==='OPEN')
      ?'<button data-wl="close" data-sym="'+s+'" title="Close trade">Close</button> '
      :'<button data-wl="enter" data-sym="'+s+'" title="Record entry plan">Enter</button> ';
    a+='<button data-wl="note" data-sym="'+s+'" title="Add a dated journal note">Note</button> ';
    a+='<button data-wl="archive" data-sym="'+s+'" title="Archive, keep journal">Archive</button>';
    h+='<td class="actions">'+a+'</td></tr>';
  }
  h+='</tbody></table>';
  q('wl-list').innerHTML=h;bindWatchClicks(q('wl-list'));bindWatchActions(q('wl-list'));
}
function wlPost(action,payload){setStatus('Saving '+action+'\u2026');busy(true);return fetch('/api/watchlist/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(r){return r.json();}).then(function(j){if(j.error)throw new Error(j.error);setStatus('Saved '+action);return j;}).catch(function(e){setStatus('Error: '+e.message);return null;}).finally(function(){busy(false);});}
function wlAdd(){var btn=q('wl-add-btn'),sym=val('wl-symbol').trim().toUpperCase();if(!sym){setStatus('Enter a symbol first.');return;}btn.disabled=true;busy(true);wlPost('add',{symbol:sym,thesis:val('wl-thesis'),tags:val('wl-tags'),note:val('wl-note')}).then(function(){btn.disabled=false;q('wl-symbol').value=q('wl-thesis').value=q('wl-tags').value=q('wl-note').value='';loadWatchlist();}).catch(function(){btn.disabled=false;});}
function wlNote(sym){var t=prompt('Journal note for '+sym+'?');if(t===null||!t.trim())return;wlPost('note',{symbol:sym,note:t.trim()}).then(function(){loadWatchlist();});}
function wlEnter(sym){var p=prompt('Entry price for '+sym+'?');if(p===null||p==='')return;var t=prompt('Target (optional)?'),s=prompt('Stop (optional)?'),q=prompt('Quantity (optional)?');wlPost('enter',{symbol:sym,price:parseFloat(p),target:t===''?null:parseFloat(t),stop:s===''?null:parseFloat(s),quantity:q===''?null:parseFloat(q)}).then(function(){loadWatchlist();});}
function wlExit(sym){var p=prompt('Exit price for '+sym+'?');if(p===null||p==='')return;wlPost('exit',{symbol:sym,price:parseFloat(p),outcome:'CLOSED'}).then(function(){loadWatchlist();});}
function wlArchive(sym){if(!confirm('Archive '+sym+'? Its journal is kept.'))return;wlPost('archive',{symbol:sym}).then(function(){loadWatchlist();});}
function loadWatchlist(){setStatus('Loading watchlist\u2026');api('watchlist').then(function(data){if(!data)return;renderWatchlist(data);});}
function loadWatchDetail(sym){q('wl-detail-h').textContent='Journal \u2014 '+sym;api('watchlist/'+sym).then(function(data){if(!data)return;var m=data.metadata;if(!m){q('wl-detail').innerHTML='<div class="empty">Not on the watchlist.</div>';return;}q('wl-detail').innerHTML=renderWatchMeta(m)+'<div class="notes"><h4>Dated journal notes</h4>'+renderTable(data.notes,WL_NOTE_COLS)+'</div>';});}
function pad2(n){return (n<10?'0':'')+n;}
function initCalendars(){
  var MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var ins=document.querySelectorAll('input.dti'),k;
  for(k=0;k<ins.length;k++)(function(inp){
    var btn=document.createElement('button');
    btn.type='button';btn.className='calbtn';btn.title='Open calendar';btn.textContent='\U0001F4C5';
    var pop=document.createElement('div');
    pop.className='calpop';pop.style.display='none';
    var host=inp.parentNode;
    host.parentNode.insertBefore(btn,host.nextSibling);
    document.body.appendChild(pop);
    function base(){var v=inp.value;return v?new Date(v+'T12:00:00'):new Date();}
    function render(y,m){
      var first=new Date(y,m,1),days=new Date(y,m+1,0).getDate();
      var lead=(first.getDay()+6)%7,h='<div class="calhead"><button type="button" data-nav="-1">\u2039</button><span>'+MONTHS[m]+' '+y+'</span><button type="button" data-nav="1">\u203A</button></div>';
      h+='<table class="calgrid"><tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr><tr>';
      for(var i=0;i<lead;i++)h+='<td></td>';
      for(var d=1;d<=days;d++){
        if((lead+d-1)%7===0)h+='</tr><tr>';
        var iso=y+'-'+pad2(m+1)+'-'+pad2(d);
        h+='<td><button type="button" data-d="'+iso+'"'+(inp.value===iso?' class="sel"':'')+'>'+d+'</button></td>';
      }
      h+='</tr></table>';
      pop.innerHTML=h;
      pop.querySelectorAll('.calhead button')[0].onclick=function(){render(y,m-1);};
      pop.querySelectorAll('.calhead button')[1].onclick=function(){render(y,m+1);};
      var ds=pop.querySelectorAll('.calgrid td button');
      for(var j=0;j<ds.length;j++)ds[j].onclick=function(){inp.value=this.dataset.d;pop.style.display='none';};
    }
    btn.addEventListener('click',function(e){e.preventDefault();
      if(pop.style.display==='block'){pop.style.display='none';return;}
      var b=btn.getBoundingClientRect();pop.style.left=b.left+'px';pop.style.top=(b.bottom+6)+'px';pop.style.display='block';
      var c=base();render(c.getFullYear(),c.getMonth());
    });
    document.addEventListener('click',function(e){if(pop.style.display==='block'&&!pop.contains(e.target)&&e.target!==btn)pop.style.display='none';});
  })(ins[k]);
}
initCalendars();
</script>
</body></html>"""


def index_html() -> str:
    """Self-contained navigable single-page UI for all report views."""
    dates = _available_dates()
    latest = dates[0] if dates else ""
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
        .replace("__LATEST_DATE__", latest)
        .replace("__SNAPSHOT_ROWS__", rows)
    )


def regenerate_index() -> None:
    """Write reports/index.html from the current snapshots."""
    REPORTS_DIR.mkdir(exist_ok=True)
    try:
        (REPORTS_DIR / "index.html").write_text(index_html())
    except Exception:
        pass
