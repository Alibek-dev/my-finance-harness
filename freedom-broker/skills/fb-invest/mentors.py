#!/usr/bin/env python3
"""Mentors' Google Sheet portfolios vs my Freedom portfolio -> buy plan for a given amount (read-only)."""
import argparse, csv, datetime as dt, importlib.util, io, json, os, re, statistics, sys
import urllib.request, urllib.error

TN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "fb-portfolio", "tn.py")
PRIMARY = "СВЕТЛОЕ БУДУЩЕЕ"  # основной портфель; ссылка меняется каждый месяц — всё сверяем по названиям вкладок
TYPE_TO_CLASS = {"акция": "stock", "etf": "index", "фонд": "index", "хедж": "hedge"}
CLASS_RU = {"stock": "акции", "index": "индекс", "hedge": "хедж"}
ACCUMULATE = "НАКАПЛИВАТЬ"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# header cell (lower-case, row with «Buy Sell») -> field; value is exact match or prefix if it ends with '*'
COLS = {"date": "дата", "ticker": "тикер", "name": "название*", "cur_price": "текущая цена", "rec": "текущие реко*",
        "side": "buy sell", "qty": "к-во", "price": "цена", "fee": "комиссии", "type": "вид актива*",
        "period": "период*", "sl": "sl", "tp": "tp", "state": "состояние*", "shariah": "соответствие*"}

spec = importlib.util.spec_from_file_location("tn", TN_PATH)
tn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tn)
STATE_DIR = os.path.join(tn.FB_HOME, "state", "mentors")
REPORT_DIR = tn.REPORT_DIR


# ---------- Google Sheet ----------

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60, context=tn.SSL_CTX) as r:
            return r.headers.get("Content-Type", ""), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} fetching sheet — link is outdated or access revoked; ask the user for this month's link")
    except urllib.error.URLError as e:
        sys.exit(f"Network error fetching sheet: {e.reason}")


def sheet_id(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        sys.exit(f"Not a Google Sheets link: {url}")
    g = re.search(r"[#?&]gid=(\d+)", url)
    return m.group(1), g.group(1) if g else None


def list_tabs(sid):
    _, html = get(f"https://docs.google.com/spreadsheets/d/{sid}/htmlview")
    return [(n.strip(), g) for n, g in re.findall(r'items\.push\(\{name: "([^"]*)".{0,400}?gid: "(\d+)"', html)]


def fetch_csv(sid, gid):
    ctype, body = get(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}")
    if "text/csv" not in ctype:
        sys.exit("Sheet did not return CSV — link is outdated or not public; ask the user for this month's link")
    return list(csv.reader(io.StringIO(body)))


def fnum(s):
    s = (s or "").replace("\xa0", "").replace(" ", "").replace("$", "").replace("%", "").strip()
    if not s or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", s):  # broken cells rendered as dates
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def fdate(s):
    try:
        return dt.datetime.strptime(s.strip(), "%d/%m/%y").date()
    except ValueError:
        return None


def short(title):
    return re.sub(r"^портфель\s+", "", title, flags=re.I).capitalize()


def parse_sheet(rows):
    """-> (title, updated, trades) or None if the tab is not a portfolio journal."""
    h = next((i for i, r in enumerate(rows) if any(c.strip().lower() == "buy sell" for c in r)), None)
    if h is None:
        return None
    hdr = [c.strip().lower() for c in rows[h]]
    col = {}
    for f, pat in COLS.items():
        col[f] = next((i for i, c in enumerate(hdr)
                       if (c.startswith(pat[:-1]) if pat.endswith("*") else c == pat)), None)
    title = " ".join((rows[1][1] if len(rows) > 1 and len(rows[1]) > 1 else "").split())
    updated = next((m.group(1) for r in rows[:3] for c in r
                    for m in [re.search(r"обновление:\s*([\d.]+)", c)] if m), None)
    trades = []
    for r in rows[h + 1:]:
        v = lambda f: r[col[f]].strip() if col[f] is not None and col[f] < len(r) else ""
        side = v("side").capitalize()
        if side not in ("Buy", "Sell") or not v("ticker"):
            continue
        trades.append({
            "no": r[0].strip(), "date": fdate(v("date")), "ticker": v("ticker").upper(), "name": v("name"),
            "cur_price": fnum(v("cur_price")), "rec": v("rec").upper(), "side": side, "qty": fnum(v("qty")) or 0,
            "price": fnum(v("price")), "fee": fnum(v("fee")), "type": v("type"), "period": v("period"),
            "sl": fnum(v("sl")), "tp": fnum(v("tp")), "state": v("state"), "shariah": v("shariah"),
        })
    return (title, updated, trades) if trades else None


def mentor_positions(trades):
    pos = {}
    for t in trades:
        if t["state"] != "Открыта" or t["side"] != "Buy":
            continue
        p = pos.setdefault(t["ticker"], {"ticker": t["ticker"], "name": t["name"], "qty": 0, "cost": 0,
                                         "price": None, "rec": "", "type": "", "period": "", "sl": None,
                                         "tp": None, "shariah": "", "last_buy": None})
        p["qty"] += t["qty"]
        p["cost"] += t["qty"] * (t["price"] or 0)
        for k in ("rec", "type", "period", "shariah", "name"):
            p[k] = p[k] or t[k]
        for k, src in (("price", "cur_price"), ("sl", "sl"), ("tp", "tp")):
            if p[k] is None and t[src] is not None:
                p[k] = t[src]
        if t["date"] and (not p["last_buy"] or t["date"] > p["last_buy"]):
            p["last_buy"] = t["date"]
    total = sum(p["qty"] * (p["price"] or 0) for p in pos.values())
    for p in pos.values():
        p["avg"] = round(p["cost"] / p["qty"], 2) if p["qty"] else None
        p["value"] = round(p["qty"] * (p["price"] or 0), 2)
        p["weight_pct"] = round(p["value"] / total * 100, 2) if total else 0
        p["pl_pct"] = round((p["price"] / p["avg"] - 1) * 100, 1) if p["price"] and p["avg"] else None
        p["class"] = TYPE_TO_CLASS.get(p["type"].lower())
        del p["cost"]
    return pos


def load_portfolios(url, primary):
    sid, link_gid = sheet_id(url)
    tabs = [(n, g) for n, g in list_tabs(sid) if "архив" not in n.lower()] or [(None, link_gid or "0")]
    out = []
    for name, gid in tabs:
        parsed = parse_sheet(fetch_csv(sid, gid))
        if not parsed:
            continue
        title, updated, trades = parsed
        out.append({"title": title, "short": short(title), "gid": gid, "updated": updated, "trades": trades,
                    "pos": mentor_positions(trades), "primary": primary.upper() in title.upper()})
    if not any(p["primary"] for p in out):
        found = ", ".join(p["title"] for p in out) or "none"
        sys.exit(f"Primary portfolio «{primary}» not found (tabs: {found}) — wrong or outdated link; ask for this month's link")
    return sorted(out, key=lambda p: not p["primary"])


# ---------- snapshot diff ----------

def trade_key(t):
    return f"{t['no']}|{t['date']}|{t['ticker']}|{t['side']}|{t['qty']:g}"


def state_path(title):
    return os.path.join(STATE_DIR, re.sub(r"\W+", "_", title.lower()).strip("_") + ".json")


def snapshot(pf):
    return {"taken": dt.datetime.now().isoformat(timespec="seconds"),
            "positions": {k: {f: v[f] for f in ("sl", "tp", "rec", "qty")} for k, v in pf["pos"].items()},
            "trades": sorted(trade_key(t) for t in pf["trades"])}


def diff(prev, pf):
    if not prev:
        return None
    pos, pp = pf["pos"], prev["positions"]
    out = {"since": prev["taken"], "sl_tp": [], "rec": [], "opened": [], "closed": [], "new_trades": []}
    for k, v in pos.items():
        o = pp.get(k)
        if not o:
            out["opened"].append(k)
            continue
        for f in ("sl", "tp"):
            if o[f] != v[f]:
                out["sl_tp"].append({"ticker": k, "field": f.upper(), "old": o[f], "new": v[f]})
        if o["rec"] != v["rec"]:
            out["rec"].append({"ticker": k, "old": o["rec"], "new": v["rec"]})
    out["closed"] = [k for k in pp if k not in pos]
    seen = set(prev["trades"])
    out["new_trades"] = [t for t in pf["trades"] if trade_key(t) not in seen]
    return out


def change_records(ts, name, ch):
    """Flatten one portfolio diff into journal/mentor_changes.jsonl rows."""
    base_ = {"ts": ts, "portfolio": name}
    rows = [dict(base_, kind=x["field"].lower(), ticker=x["ticker"], old=x["old"], new=x["new"]) for x in ch["sl_tp"]]
    rows += [dict(base_, kind="rec", ticker=x["ticker"], old=x["old"], new=x["new"]) for x in ch["rec"]]
    rows += [dict(base_, kind="opened", ticker=k) for k in ch["opened"]]
    rows += [dict(base_, kind="closed", ticker=k) for k in ch["closed"]]
    rows += [dict(base_, kind="trade", ticker=t["ticker"], side=t["side"], qty=t["qty"], price=t["price"],
                  date=t["date"], sl=t["sl"], tp=t["tp"]) for t in ch["new_trades"]]
    return rows


# ---------- my side ----------

def base(t):
    return t.rsplit(".", 1)[0].upper() if "." in t else t.upper()


def my_stops(orders_res):
    """Active take-profit (type 6) / stop-loss (type 5) sell orders by base ticker."""
    res = orders_res.get("result", orders_res) if isinstance(orders_res, dict) else {}
    lst = ((res.get("orders") or {}).get("order") or []) if isinstance(res, dict) else []
    if isinstance(lst, dict):
        lst = [lst]
    out = {}
    for o in lst:
        if o.get("oper") not in (3, 4) or o.get("type") not in (5, 6):
            continue
        d = out.setdefault(base(o.get("instr", "")), {"tp": None, "sl": None, "tp_q": None, "sl_q": None})
        k = "tp" if o["type"] == 6 else "sl"
        d[k], d[k + "_q"] = tn.num(o.get("stop"), None), tn.num(o.get("q"), None)
    return out


def quotes(c, tickers):
    out = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 40):
        res = c("getStockQuotesJson", {"tickers": ",".join(f"{t}.US" for t in tickers[i:i + 40])})
        qs = (res.get("result") or res).get("q", []) if isinstance(res, dict) else []
        for q in qs or []:
            px = tn.num(q.get("ltp"), None)
            if q.get("c") and px:
                out[base(q["c"])] = px
    return out


def close(a, b):
    return a is not None and b is not None and abs(a - b) < 0.005 * max(abs(a), abs(b), 1)


def sl_tp_check(pfs, my_rows, stops):
    out, has_level, open_any = [], set(), set()
    for pf in pfs:
        for k, p in pf["pos"].items():
            open_any.add(k)
            mine = my_rows.get(k)
            if not mine:
                continue
            s = stops.get(k, {})
            for f in ("tp", "sl"):
                if p[f] is None:
                    continue
                has_level.add((k, f))
                have, q = s.get(f), s.get(f + "_q")
                status = ("ok" if close(have, p[f]) and (q or 0) >= mine["qty"] else
                          "qty_mismatch" if close(have, p[f]) else "differs" if have is not None else "missing")
                out.append({"ticker": k, "portfolio": pf["short"], "field": f.upper(), "mentor": p[f], "mine": have,
                            "my_order_qty": q, "my_qty": mine["qty"], "status": status})
    for k, s in stops.items():  # my stops on tickers where mentors have no level / exited everywhere
        for f in ("tp", "sl"):
            if s.get(f) is not None and (k, f) not in has_level:
                out.append({"ticker": k, "portfolio": None, "field": f.upper(), "mentor": None, "mine": s[f],
                            "my_order_qty": s.get(f + "_q"), "my_qty": (my_rows.get(k) or {}).get("qty"),
                            "status": "orphan" if k not in my_rows else
                                      "mentor_has_none" if k in open_any else "mentor_closed"})
    return out


# ---------- plan ----------

def aggregate(pfs):
    """One entry per ticker across all portfolios where it is open; primary portfolio's data wins."""
    agg = {}
    for pf in pfs:  # primary first
        for k, p in pf["pos"].items():
            a = agg.get(k)
            if not a:
                a = agg[k] = dict(p, portfolios=[], accumulate_in=[], primary=False)
            a["portfolios"].append(pf["short"])
            if p["rec"] == ACCUMULATE:
                a["accumulate_in"].append(pf["short"])
            a["primary"] = a["primary"] or pf["primary"]
            a["weight_pct"] = max(a["weight_pct"], p["weight_pct"])
            if p["last_buy"] and (not a["last_buy"] or p["last_buy"] > a["last_buy"]):
                a["last_buy"] = p["last_buy"]
            for f in ("sl", "tp", "price", "avg", "class"):
                if a[f] is None:
                    a[f] = p[f]
    for a in agg.values():
        a["rec"] = ACCUMULATE if a["accumulate_in"] else a["rec"]
    return agg


def plan(agg, my, classes, rules, amount, fee, recent_days, live, max_tickers, sold=None, from_cash=False):
    today = dt.date.today()
    sold = sold or {}
    net_after = my["totals_usd"]["net_assets"] + (0 if from_cash else amount)
    lim = tn.num(rules.get("max_position_pct"), 5)
    cap = net_after * lim / 100
    targets = {k: tn.num(rules.get(f"target_{k}"), v) for k, v in (("stock", 40), ("index", 30), ("hedge", 30))}
    cls_now = {k: v["usd"] for k, v in my["structure"].items()}
    gap = {k: targets[k] * net_after / 100 - cls_now.get(k, 0) for k in targets}
    my_rows = {base(r["ticker"]): r for r in my["positions"]}

    cands = []
    for k, p in agg.items():
        recent = p["last_buy"] and (today - p["last_buy"]).days <= recent_days
        if p["rec"] != ACCUMULATE and not recent:
            continue
        px = live.get(k) or p["price"]
        mine = my_rows.get(k)
        cls = classes.get(k) or (mine or {}).get("class") or p["class"]
        have = mine["value_usd"] if mine else 0.0
        head = cap - have
        why, score = [], 0.0
        if p["rec"] == ACCUMULATE:
            score += 3; why.append("НАКАПЛИВАТЬ: " + ", ".join(p["accumulate_in"]))
        else:
            why.append(f"{p['rec'] or '—'} (не докупают)")
        if p["primary"]:
            score += 2; why.append("есть в основном портфеле")
        if len(p["portfolios"]) > 1:
            score += 0.5; why.append(f"в {len(p['portfolios'])} портфелях")
        if recent:
            d = (today - p["last_buy"]).days
            score += 2 + (1 if d <= 14 else 0); why.append(f"их покупка {d} дн. назад")
        g = gap.get(cls, 0)
        if g > 0:
            score += min(g / max(amount, 1), 2); why.append(f"класс «{CLASS_RU.get(cls, cls)}» недобран на ${g:.0f}")
        elif g < 0:
            score -= min(-g / max(amount, 1), 2); why.append(f"класс «{CLASS_RU.get(cls, cls)}» перебран на ${-g:.0f}")
        mw = have / net_after * 100
        if p["weight_pct"] > mw:
            score += min((p["weight_pct"] - mw) / 2, 1.5); why.append(f"у них до {p['weight_pct']:.1f}%, у вас {mw:.1f}%")
        if p["avg"] and px and px < p["avg"]:
            score += 0.5; why.append(f"цена ниже их средней ${p['avg']:.2f}")
        s = sold.get(k)
        if s and s["currency"] == "USD" and s["price"]:
            cheaper = px < s["price"]
            score -= 1 if cheaper else 2
            why.append(f"вы продали {s['date'][:10]} по ${s['price']:.2f} (P&L ${s['realized_pl_usd']:+.2f}), "
                       f"сейчас {'дешевле' if cheaper else 'не дешевле'}")
        block = None
        if k not in live:
            block = "нет котировки у брокера (.US) — проверьте тикер вручную"
        elif head < px:
            block = f"упрётесь в лимит {lim:g}% (запас ${max(head, 0):.0f})"
        elif px + fee > amount:
            block = f"1 шт. дороже суммы (${px:.2f})"
        cands.append({"ticker": k, "name": p["name"], "portfolios": p["portfolios"], "price": px,
                      "price_src": "broker" if k in live else "sheet", "class": cls, "mentor_rec": p["rec"],
                      "mentor_weight_pct": p["weight_pct"], "my_weight_pct": round(mw, 2),
                      "headroom_usd": round(head, 2), "mentor_sl": p["sl"], "mentor_tp": p["tp"],
                      "shariah": p["shariah"], "score": round(score, 2), "why": why, "blocked": block,
                      "last_buy": str(p["last_buy"]) if p["last_buy"] else None})
    cands.sort(key=lambda c: (c["blocked"] is not None, -c["score"]))

    # Greedy round-robin over top candidates: whole shares, fee reserved per ticker, 5% cap respected.
    # At most one ticker per $50 so per-order fees stay small relative to the amount.
    n_max = max(1, min(max_tickers, int(amount // 50)))
    pick = [c for c in cands if not c["blocked"] and c["mentor_rec"] == ACCUMULATE][:n_max] or \
           [c for c in cands if not c["blocked"]][:n_max]
    left, basket = amount, {}
    progress = True
    while progress:
        progress = False
        for c in pick:
            n = basket.get(c["ticker"], 0)
            need = c["price"] + (fee if n == 0 else 0)
            if need <= left and (n + 1) * c["price"] <= c["headroom_usd"]:
                basket[c["ticker"]] = n + 1
                left -= need
                progress = True
    for c in [c for c in cands if not c["blocked"] and c["ticker"] not in basket
              and c["mentor_rec"] == ACCUMULATE]:  # fill the leftover (cands already sorted by score)
        if len(basket) < n_max and c["price"] + fee <= left and c["price"] <= c["headroom_usd"]:
            basket[c["ticker"]] = 1
            left -= c["price"] + fee
    by_t = {c["ticker"]: c for c in cands}
    lines = [{"ticker": t, "qty": n, "price": by_t[t]["price"], "sum": round(n * by_t[t]["price"], 2),
              "class": by_t[t]["class"], "portfolios": by_t[t]["portfolios"],
              "mentor_sl": by_t[t]["mentor_sl"], "mentor_tp": by_t[t]["mentor_tp"]}
             for t, n in basket.items()]
    spent = sum(l["sum"] for l in lines)
    return {"amount": amount, "from_cash": from_cash, "fee_per_order": fee, "net_after": round(net_after, 2), "cap_usd": round(cap, 2),
            "class_gap_usd": {k: round(v, 2) for k, v in gap.items()}, "candidates": cands,
            "basket": lines, "spent": round(spent, 2), "fees": round(fee * len(lines), 2),
            "left": round(amount - spent - fee * len(lines), 2)}


def my_sales_vs_mentors(recent, agg, last_sell, live):
    """My recent sells with what mentors do with the same ticker now."""
    out = []
    for o in recent:
        k = base(o["ticker"])
        p, ms = agg.get(k), last_sell.get(k)
        status = "holding" if p else "exited" if ms else "never"
        row = dict(o, base=k, mentor_status=status, price_now=live.get(k) if o["currency"] == "USD" else None)
        if p:
            row.update(mentor_rec=p["rec"], mentor_tp=p["tp"], mentor_sl=p["sl"], portfolios=p["portfolios"])
        elif ms:
            row.update(mentor_sold_date=str(ms["date"]), mentor_sold_price=ms["price"], portfolios=[ms["portfolio"]])
        out.append(row)
    return out


# ---------- markdown ----------

def money(x):
    return "—" if x is None else f"${x:,.2f}".replace(",", " ")


def qty(x):
    return "—" if x is None else f"{x:g}"


def render_md(o):
    pl = o["plan"]
    L = [f"# План покупки на {money(pl['amount'])} — {o['as_of'].replace('T', ' ')}", "",
         "Портфели наставников: " + "; ".join(
             f"**{p['short']}**{' (основной)' if p['primary'] else ''} — обновлён {p['updated'] or '?'}, "
             f"открытых позиций {p['open_positions']}" for p in o["portfolios"]) + ".  ",
         f"Мои чистые активы: {money(o['my_totals']['net_assets'])}, "
         + (f"покупка из кэша на счёте ({money(o['my_totals']['cash'])})" if pl["from_cash"]
            else f"после пополнения {money(pl['net_after'])}")
         + f", лимит на бумагу {money(pl['cap_usd'])}.  ",
         "Реализованный P&L: месяц {month} · с начала года {ytd}.".format(
             **{k: money(v) for k, v in o["my_realized_usd"].items()}), "", "## Изменения у наставников", ""]
    for name, ch in o["changes"].items():
        if ch is None:
            L.append(f"- **{name}**: первый запуск — снимок сохранён, изменения будут видны со следующего раза")
            continue
        items = [f"**{x['ticker']} {x['field']}: {money(x['old'])} → {money(x['new'])}**" for x in ch["sl_tp"]]
        items += [f"{x['ticker']}: {x['old'] or '—'} → {x['new'] or '—'}" for x in ch["rec"]]
        items += ["новые позиции: " + ", ".join(ch["opened"])] if ch["opened"] else []
        items += ["закрыты: " + ", ".join(ch["closed"])] if ch["closed"] else []
        items += [f"{t['date']} {t['side']} {t['ticker']} {t['qty']:g} шт. по {money(t['price'])}" for t in ch["new_trades"]]
        L.append(f"- **{name}** (с {ch['since'][:16].replace('T', ' ')}): " + ("; ".join(items) or "изменений нет"))
    L += ["", "## Их сделки за последние дни", "",
          "| Дата | Портфель | Сделка | Тикер | Кол-во | Цена | Рекомендация |", "|---|---|---|---|--:|--:|---|"]
    for t in o["recent_trades"]:
        L.append(f"| {t['date']} | {t['portfolio']} | {t['side']} | {t['ticker']} | {t['qty']:g} | "
                 f"{money(t['price'])} | {t['rec'] or '—'} |")
    if o["my_recent_sales"]:
        st = {"holding": "держат", "exited": "тоже вышли", "never": "не было"}
        L += ["", "## Мои продажи против наставников", "",
              "| Дата | Тикер | Кол-во | Цена | P&L $ | Сейчас | Ещё держу | Наставники |", "|---|---|--:|--:|--:|--:|---|---|"]
        for x in o["my_recent_sales"]:
            m = st[x["mentor_status"]]
            if x["mentor_status"] == "holding":
                m += f" ({', '.join(x['portfolios'])}; {x['mentor_rec'] or '—'}; TP {money(x['mentor_tp'])}, SL {money(x['mentor_sl'])})"
            elif x["mentor_status"] == "exited":
                m += f" ({x['mentor_sold_date']} по {money(x['mentor_sold_price'])})"
            L.append(f"| {x['date'][:10]} | {x['ticker']} | {x['qty']:g} | {x['price']:,.2f} {x['currency']} | "
                     f"{money(x['realized_pl_usd'])} | {money(x['price_now'])} | {'да' if x['still_held'] else 'нет'} | {m} |")
    L += ["", "## Что купить", ""]
    if pl["basket"]:
        L += ["| Тикер | Кол-во | Цена | Сумма | Класс | Портфели | SL наставников | TP наставников |",
              "|---|--:|--:|--:|---|---|--:|--:|"]
        for b in pl["basket"]:
            L.append(f"| {b['ticker']} | {b['qty']} | {money(b['price'])} | {money(b['sum'])} | "
                     f"{CLASS_RU.get(b['class'], b['class'])} | {', '.join(b['portfolios'])} | "
                     f"{money(b['mentor_sl'])} | {money(b['mentor_tp'])} |")
        L += ["", f"Итого {money(pl['spent'])} + комиссии ≈{money(pl['fees'])}, остаток {money(pl['left'])}.", ""]
    else:
        L += ["Подходящих покупок на эту сумму не нашлось — см. кандидатов ниже.", ""]
    L += ["### Кандидаты", "", "| Тикер | Цена | Класс | Балл | Почему | Ограничение |", "|---|--:|---|--:|---|---|"]
    for c in pl["candidates"]:
        L.append(f"| {c['ticker']} | {money(c['price'])} | {CLASS_RU.get(c['class'], c['class'] or '—')} | {c['score']} | "
                 f"{'; '.join(c['why'])} | {c['blocked'] or '—'} |")
    L += ["", "## Мои TP/SL против наставников", ""]
    labels = {"ok": "✅ совпадает", "differs": "⚠️ отличается", "missing": "❌ не выставлен",
              "qty_mismatch": "⚠️ ордер не на всё кол-во", "mentor_closed": "ℹ️ наставники вышли из бумаги",
              "mentor_has_none": "ℹ️ у наставников нет уровня",
              "orphan": "❗ бумаги уже нет — ордер висит, отмените"}
    if o["sl_tp_check"]:
        L += ["| Тикер | Портфель | Уровень | Наставники | Мой ордер | Кол-во (ордер/у меня) | Статус |",
              "|---|---|---|--:|--:|---|---|"]
        for x in o["sl_tp_check"]:
            L.append(f"| {x['ticker']} | {x['portfolio'] or '—'} | {x['field']} | {money(x['mentor'])} | {money(x['mine'])} | "
                     f"{qty(x['my_order_qty'])} / {qty(x['my_qty'])} | {labels[x['status']]} |")
    else:
        L.append("Совпадающих позиций с TP/SL нет.")
    if o["mentor_closed_i_hold"]:
        L += ["", "## Наставники вышли, а у вас ещё есть", ""]
        for x in o["mentor_closed_i_hold"]:
            L.append(f"- {x['ticker']}: последняя продажа {x['date']} по {money(x['price'])} ({x['portfolio']})")
    L += ["", "_Не является финансовым советом. Решения и ордера — на вашей стороне._", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--amount", type=float, required=True)
    ap.add_argument("--primary", default=PRIMARY, help="основной портфель (вкладка) — получает бонус в баллах")
    ap.add_argument("--fee", type=float, help="комиссия на ордер, $ (по умолчанию — медиана основного портфеля)")
    ap.add_argument("--recent-days", type=int, default=31)
    ap.add_argument("--max-tickers", type=int, default=3)
    ap.add_argument("--from-cash", action="store_true", help="сумма берётся из кэша на счёте (не пополнение)")
    ap.add_argument("--md-dir", default=os.environ.get("TRADERNET_REPORT_DIR") or REPORT_DIR)
    ap.add_argument("--no-save", action="store_true", help="не писать md и не обновлять снимки")
    a = ap.parse_args()

    pfs = load_portfolios(a.url, a.primary)
    agg = aggregate(pfs)

    pub, priv, domain, classes, rules = tn.load_cfg(None)
    c = lambda cmd, p=None: tn.call(pub, priv, domain, cmd, p)
    my = tn.portfolio(c("getPositionJson"), classes, rules)
    stops = my_stops(c("getNotifyOrderJson", {"active_only": 1}))
    trades = tn.sync_trades(c, write=not a.no_save)[1]
    sales = tn.sales(trades, my["fx_rates_vs_usd"], a.recent_days, held=[r["ticker"] for r in my["positions"]])
    sold = {}
    for o in sales["recent"]:  # newest first
        sold.setdefault(base(o["ticker"]), o)
    live = quotes(c, set(agg) | {k for k, o in sold.items() if o["currency"] == "USD"})
    fees = [t["fee"] for t in pfs[0]["trades"] if t["fee"]]
    fee = a.fee if a.fee is not None else round(statistics.median(fees), 2) if fees else 1.5

    my_rows = {base(r["ticker"]): r for r in my["positions"]}
    today = dt.date.today()
    prevs = {pf["title"]: (json.load(open(state_path(pf["title"]), encoding="utf-8"))
                           if os.path.isfile(state_path(pf["title"])) else None) for pf in pfs}
    last_sell = {}
    for pf in pfs:
        for t in pf["trades"]:
            if t["side"] == "Sell" and t["date"] and (t["ticker"] not in last_sell
                                                      or t["date"] > last_sell[t["ticker"]]["date"]):
                last_sell[t["ticker"]] = dict(t, portfolio=pf["short"])

    out = {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "portfolios": [{"title": pf["title"], "short": pf["short"], "primary": pf["primary"], "updated": pf["updated"],
                        "open_positions": len(pf["pos"]),
                        "positions": sorted(pf["pos"].values(), key=lambda p: -p["weight_pct"])} for pf in pfs],
        "my_totals": my["totals_usd"], "my_structure": my["structure"], "my_realized_usd": sales["realized_usd"],
        "my_recent_sales": my_sales_vs_mentors(sales["recent"], agg, last_sell, live),
        "changes": {pf["short"]: diff(prevs[pf["title"]], pf) for pf in pfs},
        "recent_trades": sorted((dict(t, portfolio=pf["short"]) for pf in pfs for t in pf["trades"]
                                 if t["date"] and (today - t["date"]).days <= a.recent_days),
                                key=lambda t: t["date"], reverse=True),
        "sl_tp_check": sl_tp_check(pfs, my_rows, stops),
        "mentor_closed_i_hold": [{"ticker": k, "date": str(t["date"]), "price": t["price"], "portfolio": t["portfolio"]}
                                 for k, t in last_sell.items() if k in my_rows and k not in agg],
        "plan": plan(agg, my, classes, rules, a.amount, fee, a.recent_days, live, a.max_tickers, sold, a.from_cash),
    }
    if not a.no_save:
        os.makedirs(STATE_DIR, exist_ok=True)
        for pf in pfs:
            with open(state_path(pf["title"]), "w", encoding="utf-8") as f:
                json.dump(snapshot(pf), f, ensure_ascii=False, default=str)
        path = tn.report_path(a.md_dir, "invest", out["as_of"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_md(out))
        out["report_md"] = path
        pl = out["plan"]
        tn.journal_append("mentor_changes.jsonl", *(r for name, ch in out["changes"].items() if ch
                                                   for r in change_records(out["as_of"], name, ch)))
        tn.journal_append("runs.jsonl", {
            "ts": out["as_of"], "skill": "fb-invest", "amount": a.amount, "from_cash": a.from_cash, "fee_per_order": pl["fee_per_order"],
            "net_assets": out["my_totals"]["net_assets"],
            "portfolios": {p["short"]: p["updated"] for p in out["portfolios"]},
            "changes": {k: None if v is None else sum(len(v[f]) for f in ("sl_tp", "rec", "opened", "closed", "new_trades"))
                        for k, v in out["changes"].items()},
            "basket": [{f: b[f] for f in ("ticker", "qty", "price", "sum", "mentor_sl", "mentor_tp")} for b in pl["basket"]],
            "spent": pl["spent"], "fees": pl["fees"], "left": pl["left"],
            "sl_tp_issues": sum(x["status"] in ("missing", "differs", "qty_mismatch", "orphan") for x in out["sl_tp_check"]),
            "report_md": path})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
