# -*- coding: utf-8 -*-
"""
派網合約選幣看板
- 抓派網 (Pionex) 永續合約公開行情，不需要 API Key
- 4H MACD 判方向，1 小時 KDJ 找進場點，4H RSI 過濾過熱/過冷，只做 4H 柱體放大的強訊號
- 指定的幣出現可進場訊號時，傳 Telegram 通知
- 依 4H ATR 算止損、止盈，並換算成你設定槓桿下的保證金盈虧
- 每 15 分鐘自動更新一次，結果開在瀏覽器裡

只需要 Python 3.8 以上，不用安裝任何套件。
這是依規則篩選的工具，不是投資建議；下單請自己在派網 App 確認後執行。
"""

import json
import os
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone, timedelta

# ===================== 可以自己改的設定 =====================
LEVERAGE = 40            # 你打算用的槓桿倍數
MARGIN_USDT = 30         # 每筆投入的保證金 (USDT)
MAX_RISK_PCT = 50        # 單筆止損最多虧掉保證金的幾 %（用來算建議最高槓桿）
GRID_STEP_PCT = 0.3      # 網格每格間距約幾 %（太小會被手續費吃掉）
GRID_PAD_PCT = 0.2       # 網格區間在進場價外多留幾 %
PENDING_MARGIN = 15      # 「等1H交叉」預先掛單建議投入的 U（沒確認過的進場，金額小一點）
ENTRY_TF, ENTRY_TF_MIN = "60M", 60   # 進場週期：1 小時
NOTIFY_COINS = "ALL"     # "ALL" = 看板上所有可進場的幣都通知；只想收部分可改成 {"BTC", "ETH"}
TESTED_COINS = {"BTC"}   # 回測過、表現合格的幣（通知裡會標示）
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7526307552")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")   # 放在 GitHub Secrets，不要寫在這裡
MIN_VOLUME_USDT = 2_000_000   # 24 小時成交額門檻，太小的幣不看
MAX_COINS = 150          # 依成交額取前幾名來分析
REFRESH_MINUTES = 15     # 幾分鐘更新一次
SL_ATR_MULT = 1.0        # 止損 = 4H ATR × 這個倍數
SL_MIN_PCT, SL_MAX_PCT = 1.0, 4.0   # 止損距離上下限 (%)
TP1_R, TP2_R = 1.5, 2.5  # 止盈 = 止損距離 × 這個倍數
# ============================================================

API = "https://api.pionex.com"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "派網選幣看板.html")
if "--out" in sys.argv:                      # 雲端版用：指定輸出位置
    OUT_FILE = os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
CLOUD = "--out" in sys.argv

# 名稱結尾是 X 但其實是加密貨幣的（其餘結尾 X 的多半是股票代幣，排除）
CRYPTO_ENDING_X = {"IMX", "ZRX", "STRAX", "BEAMX", "FLUX", "TRX", "WAXP", "MINAX", "ONDOX"}
EXCLUDE = {"USDC", "PAXG", "XAUT", "WBTC", "MBTC", "METH"}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("result"):
        raise RuntimeError(data.get("message", "API 回傳失敗"))
    return data["data"]


def is_stock_token(base):
    return base.endswith("X") and len(base) >= 4 and base not in CRYPTO_ENDING_X


def get_candidates():
    tickers = get("/api/v1/market/tickers?type=PERP")["tickers"]
    rows = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("_USDT_PERP"):
            continue
        base = sym.split("_")[0]
        if base.upper() in EXCLUDE or is_stock_token(base):
            continue
        amount = float(t["amount"])
        if amount < MIN_VOLUME_USDT:
            continue
        o, c = float(t["open"]), float(t["close"])
        rows.append({"symbol": sym, "base": base, "price": c,
                     "chg": (c - o) / o * 100 if o else 0, "amount": amount})
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows[:MAX_COINS]


def klines(symbol, interval, limit=200):
    data = get(f"/api/v1/market/klines?symbol={symbol}&interval={interval}&limit={limit}")
    ks = data["klines"]
    ks.sort(key=lambda k: k["time"])          # API 回傳新到舊，改成舊到新
    return ([int(k["time"]) for k in ks], [float(k["high"]) for k in ks],
            [float(k["low"]) for k in ks], [float(k["close"]) for k in ks])


# ----------------------- 指標 -----------------------
def ema(vals, n):
    k = 2 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd(close):
    dif = [a - b for a, b in zip(ema(close, 12), ema(close, 26))]
    dea = ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


def rsi(close, n=14):
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = close[i] - close[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(close)):
        d = close[i] - close[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def kdj(high, low, close, n=9):
    k = d = 50.0
    ks, ds, js = [], [], []
    for i in range(len(close)):
        lo, hi = min(low[max(0, i - n + 1):i + 1]), max(high[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (close[i] - lo) / (hi - lo) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k); ds.append(d); js.append(3 * k - 2 * d)
    return ks, ds, js


def atr(high, low, close, n=14):
    trs = [high[0] - low[0]]
    for i in range(1, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def boll_pos(close, n=20):
    seg = close[-n:]
    mid = sum(seg) / n
    sd = (sum((x - mid) ** 2 for x in seg) / n) ** 0.5
    up, dn = mid + 2 * sd, mid - 2 * sd
    return 50.0 if up == dn else (close[-1] - dn) / (up - dn) * 100


# ----------------------- 判斷 -----------------------
def analyse(coin):
    _, h4, l4, c4 = klines(coin["symbol"], "4H")
    t1, h15, l15, c15 = klines(coin["symbol"], ENTRY_TF)
    now_ms = int(time.time() * 1000)
    tf_ms = ENTRY_TF_MIN * 60_000
    if t1 and t1[-1] + tf_ms > now_ms:        # 最後一根還沒收盤，不算
        t1, h15, l15, c15 = t1[:-1], h15[:-1], l15[:-1], c15[:-1]
    if len(c4) < 60 or len(c15) < 30:
        return None
    bar_close_ms = t1[-1] + tf_ms
    fresh = now_ms - bar_close_ms < REFRESH_MINUTES * 60_000   # 這次才剛收盤的訊號

    dif, dea, hist = macd(c4)
    trend = "多" if dif[-1] > dea[-1] else "空"
    hist_growing = abs(hist[-1]) > abs(hist[-2])
    r = rsi(c4)
    bpos = boll_pos(c4)

    k, d, j = kdj(h15, l15, c15)
    gold = k[-2] <= d[-2] and k[-1] > d[-1]   # 最近一根收盤的 1H K 線剛交叉（和回測一致）
    dead = k[-2] >= d[-2] and k[-1] < d[-1]

    side, entry_ok, note = None, False, []
    if trend == "多":
        side = "LONG"
        entry_ok = gold and k[-1] < 60
        if r > 75: note.append("4H RSI 過熱")
        if bpos > 95: note.append("貼近布林上軌")
    else:
        side = "SHORT"
        entry_ok = dead and k[-1] > 40
        if r < 25: note.append("4H RSI 超賣")
        if bpos < 5: note.append("貼近布林下軌")

    # 等級
    if entry_ok and hist_growing and not note:
        grade, score = "可進場", 3
    elif entry_ok and not note:
        grade, score = "動能轉弱（不做）", 2
    elif hist_growing and not note:
        grade, score = "等1H交叉", 1
    else:
        grade, score = "觀望", 0

    price = c15[-1]
    a = atr(h4, l4, c4)
    sl_pct = min(max(a / price * 100 * SL_ATR_MULT, SL_MIN_PCT), SL_MAX_PCT)
    sgn = 1 if side == "LONG" else -1
    sl = price * (1 - sgn * sl_pct / 100)
    tp1 = price * (1 + sgn * sl_pct * TP1_R / 100)
    tp2 = price * (1 + sgn * sl_pct * TP2_R / 100)
    max_lev = max(1, int(MAX_RISK_PCT // sl_pct))   # 止損時最多虧 MAX_RISK_PCT% 的建議最高槓桿
    liq_warn = LEVERAGE > max_lev

    # 「等1H交叉」的建議掛單價：等價格回檔到 1H 布林中軌附近再進場
    pend = None
    if score == 1:
        mid = sum(c15[-20:]) / 20
        if side == "SHORT":
            lim = max(mid, price * 1.003)            # 至少比現價高 0.3%
            lim = min(lim, max(h15[-12:]))           # 不超過近 12 小時高點
            if lim <= price: lim = price * 1.003
        else:
            lim = min(mid, price * 0.997)            # 至少比現價低 0.3%
            lim = max(lim, min(l15[-12:]))           # 不低於近 12 小時低點
            if lim >= price: lim = price * 0.997
        pend = {"price": lim,
                "sl": lim * (1 - sgn * sl_pct / 100),
                "tp1": lim * (1 + sgn * sl_pct * TP1_R / 100),
                "tp2": lim * (1 + sgn * sl_pct * TP2_R / 100),
                "dist": (lim - price) / price * 100,
                "loss": PENDING_MARGIN * sl_pct * LEVERAGE / 100}

    return {**coin, "side": side, "grade": grade, "score": score,
            "dif": dif[-1], "dea": dea[-1], "hist_growing": hist_growing,
            "rsi": r, "boll": bpos, "k": k[-1], "d": d[-1], "j": j[-1],
            "gold": gold, "dead": dead, "note": "、".join(note),
            "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "sl_pct": sl_pct,
            "loss": MARGIN_USDT * sl_pct * LEVERAGE / 100,
            "win1": MARGIN_USDT * sl_pct * TP1_R * LEVERAGE / 100,
            "win2": MARGIN_USDT * sl_pct * TP2_R * LEVERAGE / 100,
            "liq_warn": liq_warn, "max_lev": max_lev, "fresh": fresh, "pend": pend,
            "bar_time": datetime.fromtimestamp(bar_close_ms / 1000, timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")}


# ----------------------- 報表 -----------------------
def fmt_price(p):
    if p >= 1000: return f"{p:,.1f}"
    if p >= 1: return f"{p:,.3f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0")


def fmt_amt(a):
    return f"{a / 1e8:.2f} 億" if a >= 1e8 else f"{a / 1e4:,.0f} 萬"


def grid_cell(r):
    """看板上的網格參數：可進場用現價，等1H交叉用建議掛單價"""
    pd = r.get("pend")
    if r["score"] == 1 and pd:
        g = {"side": r["side"], "entry": pd["price"], "sl": pd["sl"], "tp2": pd["tp2"]}
        tag = "掛單價到了再開"
    else:
        g = r
        tag = "現在開"
    lo, hi, n = grid_plan(g)
    return (f'{"做多" if r["side"]=="LONG" else "做空"}・{tag}<br>'
            f'區間 <b>{fmt_price(lo)} ～ {fmt_price(hi)}</b><br>'
            f'網格 <b>{n}</b> 格・止損 {fmt_price(g["sl"])}・止盈 {fmt_price(g["tp2"])}')


def row_html(r):
    chg_cls = "up" if r["chg"] >= 0 else "down"
    grade_cls = {3: "g3", 2: "g2", 1: "g1"}.get(r["score"], "g0")
    kdj_txt = f'K {r["k"]:.0f} / D {r["d"]:.0f}'
    if r["side"] == "LONG" and r["gold"]: kdj_txt += " 金叉"
    if r["side"] == "SHORT" and r["dead"]: kdj_txt += " 死叉"
    macd_txt = ("DIF>DEA" if r["dif"] > r["dea"] else "DIF<DEA") + (" 柱放大" if r["hist_growing"] else " 柱縮小")
    warn = ""
    if r["note"]: warn += f'<div class="warn">{r["note"]}</div>'
    if r["liq_warn"]: warn += f'<div class="warn">止損 {r["sl_pct"]:.1f}%，{LEVERAGE} 倍風險偏高，建議最高 {r["max_lev"]} 倍</div>'
    pd = r.get("pend")
    if pd:
        act = "限價賣出（開空）" if r["side"] == "SHORT" else "限價買入（開多）"
        warn += (f'<div class="pend"><b>建議掛單</b> {act} <b>{fmt_price(pd["price"])}</b>'
                 f'（距現價 {pd["dist"]:+.2f}%）<br>止損 {fmt_price(pd["sl"])}・止盈1 {fmt_price(pd["tp1"])}'
                 f'・止盈2 {fmt_price(pd["tp2"])}<br>{PENDING_MARGIN}U・{LEVERAGE} 倍，止損約 -{pd["loss"]:.1f}U</div>')
    return f"""
<tr>
  <td><b class="sym">{r["base"]}</b> <span class="dir {'up' if r['side']=='LONG' else 'down'}">{'做多' if r['side']=='LONG' else '做空'}</span><div class="sub">{fmt_amt(r["amount"])}</div></td>
  <td class="num">{fmt_price(r["price"])}<div class="sub {chg_cls}">{r["chg"]:+.2f}%</div></td>
  <td><span class="pill {grade_cls}">{r["grade"]}</span>{warn}</td>
  <td class="ind">{macd_txt}<div class="sub">RSI {r["rsi"]:.0f}・布林位置 {r["boll"]:.0f}%</div></td>
  <td class="ind">{kdj_txt}</td>
  <td class="num">{fmt_price(r["entry"])}</td>
  <td class="num down-c">{fmt_price(r["sl"])}<div class="sub">−{r["sl_pct"]:.2f}%・−{r["loss"]:.1f}U</div></td>
  <td class="num up-c">{fmt_price(r["tp1"])}<div class="sub">+{r["win1"]:.1f}U</div></td>
  <td class="num up-c">{fmt_price(r["tp2"])}<div class="sub">+{r["win2"]:.1f}U</div></td>
  <td class="grid">{grid_cell(r)}</td>
</tr>"""


def table_html(title, rows, side_cls):
    if not rows:
        return f'<section><h2 class="{side_cls}">{title}</h2><p class="empty">目前沒有符合條件的幣</p></section>'
    return f"""
<section>
  <h2 class="{side_cls}">{title}<span class="count">{len(rows)}</span></h2>
  <div class="scroll"><table>
    <thead><tr><th>幣種 / 方向 / 成交額</th><th>現價 / 24h</th><th>判斷</th><th>4H MACD</th>
    <th>1H KDJ</th><th>進場</th><th>止損</th><th>止盈1</th><th>止盈2</th><th>網格設定（手動）</th></tr></thead>
    <tbody>{"".join(row_html(r) for r in rows)}</tbody>
  </table></div>
</section>"""


def build_html(results, errors, btc):
    ready = sorted([r for r in results if r["score"] == 3], key=lambda r: -r["amount"])
    pending = sorted([r for r in results if r["score"] == 1], key=lambda r: -r["amount"])
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")   # 台灣時間
    btc_txt = ""
    if btc:
        cls = "up" if btc["chg"] >= 0 else "down"
        btc_txt = f'BTC {fmt_price(btc["price"])} <span class="{cls}">{btc["chg"]:+.2f}%</span>・4H 趨勢偏{"多" if btc["side"]=="LONG" else "空"}'
    err_txt = f'<p class="sub">{len(errors)} 個幣抓取失敗已略過</p>' if errors else ""

    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_MINUTES * 60}">
<title>派網選幣看板</title>
<style>
:root {{ --bg:#f5f6f4; --panel:#ffffff; --fg:#1b1f1d; --muted:#6b736e; --line:#dfe3df;
  --up:#0f8a5f; --down:#c8372d; --amber:#b7791f; --accent:#2b5c8a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111513; --panel:#181d1b; --fg:#e6ebe8; --muted:#8f9a94;
  --line:#2a322e; --up:#3ccf8e; --down:#ff6b5e; --amber:#e3a83a; --accent:#7fb2e5; color-scheme:dark; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 "Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif; padding:24px 16px 48px; }}
.wrap {{ max-width:1200px; margin:0 auto; display:flex; flex-direction:column; gap:24px; }}
header {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:8px 24px; }}
h1 {{ margin:0; font-size:22px; letter-spacing:.02em; }}
.meta {{ color:var(--muted); font-size:13px; display:flex; flex-direction:column; gap:2px; }}
.settings {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px;
  font-size:13px; color:var(--muted); display:flex; flex-wrap:wrap; gap:6px 20px; }}
.settings b {{ color:var(--fg); font-variant-numeric:tabular-nums; }}
h2 {{ font-size:16px; margin:0 0 10px; display:flex; align-items:center; gap:8px; }}
h2.long {{ color:var(--up); }} h2.short {{ color:var(--down); }} h2.pend-h {{ color:var(--accent); }}
.count {{ font-size:12px; color:var(--muted); font-weight:400; }}
.scroll {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; min-width:1180px; }}
th, td {{ padding:10px 12px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }}
th {{ font-size:12px; color:var(--muted); font-weight:500; white-space:nowrap; }}
tr:last-child td {{ border-bottom:0; }}
.num {{ font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }}
th:nth-child(n+6), th:nth-child(2) {{ text-align:right; }}
.sym {{ font-size:15px; }} .dir {{ font-size:12px; font-weight:700; margin-left:4px; }} .sub {{ font-size:12px; color:var(--muted); }}
.ind {{ font-size:13px; white-space:nowrap; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
.up-c {{ color:var(--up); }} .down-c {{ color:var(--down); }}
.up-c .sub, .down-c .sub {{ color:inherit; opacity:.8; }}
.pill {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; white-space:nowrap; border:1px solid currentColor; }}
.g3 {{ color:var(--up); background:color-mix(in srgb, var(--up) 12%, transparent); }}
.g2 {{ color:var(--accent); }} .g1 {{ color:var(--amber); }} .g0 {{ color:var(--muted); }}
.warn {{ color:var(--amber); font-size:12px; margin-top:4px; }}
.grid {{ font-size:12px; line-height:1.7; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.pend {{ font-size:12px; margin-top:6px; padding:6px 8px; border-radius:6px; background:color-mix(in srgb, var(--accent) 10%, transparent); color:var(--fg); line-height:1.6; white-space:nowrap; }}
.empty {{ color:var(--muted); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:0; }}
.legend {{ color:var(--muted); font-size:13px; line-height:1.8; }}
.legend b {{ color:var(--fg); }}
</style></head><body><div class="wrap">
<header>
  <h1>派網選幣看板</h1>
  <div class="meta"><span>更新時間 {now}・每 {REFRESH_MINUTES} 分鐘自動更新</span><span>{btc_txt}</span></div>
</header>
<div class="settings">
  <span>槓桿 <b>{LEVERAGE}x</b></span><span>每筆保證金 <b>{MARGIN_USDT} U</b></span>
  <span>成交額門檻 <b>{fmt_amt(MIN_VOLUME_USDT)}</b></span><span>分析 <b>{len(results)}</b> 個幣</span>
  <span>止損 <b>4H ATR×{SL_ATR_MULT}</b>（{SL_MIN_PCT}%–{SL_MAX_PCT}%）</span><span>止盈 <b>{TP1_R}R / {TP2_R}R</b></span>
</div>
{err_txt}
{table_html("可進場", ready, "long")}
{table_html("可預先掛單（等1H交叉）", pending, "pend-h")}
<section class="legend">
  <b>判斷規則</b><br>
  方向：4H MACD 的 DIF 在 DEA 之上做多、之下做空。<br>
  <b>可進場</b>：最近收盤的 1H K 線 KDJ 順方向交叉（做多金叉且 K&lt;60，做空死叉且 K&gt;40），4H 柱體放大，RSI 不過熱過冷。和 TradingView 回測的策略一致。<br>
  <b>動能轉弱（不做）</b>：有交叉，但 4H 柱體在縮小，策略不進場。<br>
  <b>等1H交叉</b>：4H 方向明確且動能放大，1H 還沒給交叉。可照「建議掛單」預先掛限價單：做空掛在現價上方、做多掛在現價下方（約 1H 布林中軌），掛單時一起設好止損止盈。這種進場沒經過回測，金額建議小一點。<br>
  Telegram 通知：{"所有可進場的幣" if NOTIFY_COINS == "ALL" else "、".join(sorted(NOTIFY_COINS))}；回測合格：{"、".join(sorted(TESTED_COINS))}。<br>
  進場價為目前價格；止損、止盈與盈虧是依你的槓桿和保證金換算，未扣手續費與資金費率。<br>
  這是規則篩選結果，不是投資建議；下單前請在派網確認價格並自行判斷。
</section>
</div></body></html>"""


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception as e:
        print("  Telegram 傳送失敗：", e)
        return False


def grid_plan(r):
    """依進場、止損、止盈2 推算合約網格參數"""
    # 區間只從「進場價旁邊」到「止盈2」：開單時幾乎就是滿倉，往有利方向一路分批止盈；
    # 往不利方向不會加倉，碰到止損就整個停掉（效果接近直接開單＋分批止盈）
    if r["side"] == "LONG":
        lo, hi = r["entry"] * (1 - GRID_PAD_PCT / 100), r["tp2"]
    else:
        lo, hi = r["tp2"], r["entry"] * (1 + GRID_PAD_PCT / 100)
    width_pct = (hi - lo) / r["entry"] * 100
    n = int(min(max(round(width_pct / GRID_STEP_PCT), 5), 50))
    return lo, hi, n


def signal_text(r):
    is_long = r["side"] == "LONG"
    side = "做多 🟢" if is_long else "做空 🔴"
    tested = "✅ 已回測合格" if r["base"] in TESTED_COINS else "⚠ 尚未回測，請謹慎"
    lev_line = (f"⚠ 這筆 {LEVERAGE} 倍風險偏高，建議最高 {r['max_lev']} 倍"
                if r["max_lev"] < LEVERAGE else f"槓桿 {LEVERAGE} 倍 OK（建議最高 {r['max_lev']} 倍）")
    lo, hi, n = grid_plan(r)
    return (f"【派網訊號】{r['base']} {side}\n"
            f"{tested}\n"
            f"1H K 線收盤：{r['bar_time']}\n"
            f"{lev_line}\n"
            f"\n"
            f"▍方式一：直接開單（趨勢強時優先）\n"
            f"{'開多' if is_long else '開空'}・市價・{LEVERAGE} 倍・{MARGIN_USDT}U\n"
            f"進場：{fmt_price(r['entry'])}\n"
            f"止損：{fmt_price(r['sl'])}（-{r['sl_pct']:.2f}%，約 -{r['loss']:.1f}U）\n"
            f"止盈1：{fmt_price(r['tp1'])}（全倉約 +{r['win1']:.1f}U，建議先平一半）\n"
            f"止盈2：{fmt_price(r['tp2'])}（約 +{r['win2']:.1f}U）\n"
            f"\n"
            f"▍方式二：合約網格（分批止盈版）\n"
            f"方向：{'做多' if is_long else '做空'}・{LEVERAGE} 倍・{MARGIN_USDT}U\n"
            f"價格區間：{fmt_price(lo)} ～ {fmt_price(hi)}\n"
            f"網格數：{n} 格\n"
            f"止損：{fmt_price(r['sl'])}　止盈：{fmt_price(r['tp2'])}\n"
            f"\n"
            f"下單前請在派網確認價格。")


def run_once():
    print(f"[{datetime.now():%H:%M}] 抓取行情中…")
    coins = get_candidates()
    results, errors = [], []
    for i, c in enumerate(coins, 1):
        try:
            r = analyse(c)
            if r: results.append(r)
        except Exception as e:
            errors.append((c["symbol"], str(e)))
        print(f"  {i}/{len(coins)} {c['base']}", end="\r")
        time.sleep(0.25)   # 派網限制每秒 10 次，放慢一點
    btc = next((r for r in results if r["base"] == "BTC"), None)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(build_html(results, errors, btc))
    n_long = sum(1 for r in results if r["side"] == "LONG" and r["score"] >= 2)
    n_short = sum(1 for r in results if r["side"] == "SHORT" and r["score"] >= 2)
    print(f"\n  完成：可進場做多 {n_long} 個、做空 {n_short} 個 → {OUT_FILE}")
    for r in results:
        if (NOTIFY_COINS == "ALL" or r["base"] in NOTIFY_COINS) and r["score"] == 3 and r["fresh"]:
            ok = send_telegram(signal_text(r))
            print(f"  已傳 Telegram：{r['base']} {r['side']}" if ok else "  Telegram 未傳送（沒有設定或失敗）")
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":      # 手動執行時傳一則測試訊息
        lines = [f"{r['base']}：4H 偏{'多' if r['side']=='LONG' else '空'}・{r['grade']}"
                 for r in results if r["base"] in TESTED_COINS]
        n_ok = sum(1 for r in results if r["score"] == 3)
        lines.append(f"目前看板可進場：{n_ok} 個")
        send_telegram("✅ 看板手動更新完成，通知功能正常\n" + ("\n".join(lines) or "（指定的幣不在名單內）"))


def main():
    first = True
    while True:
        try:
            run_once()
            if first and not CLOUD:
                webbrowser.open("file://" + OUT_FILE.replace("\\", "/"))
                first = False
        except Exception as e:
            print("  抓取失敗：", e, "（下一輪再試）")
            if CLOUD:
                sys.exit(1)
        if "--once" in sys.argv or CLOUD:
            break
        print(f"  {REFRESH_MINUTES} 分鐘後更新，關掉這個視窗就停止。")
        time.sleep(REFRESH_MINUTES * 60)


if __name__ == "__main__":
    main()
