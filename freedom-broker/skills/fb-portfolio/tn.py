#!/usr/bin/env python3
"""Tradernet / Freedom Broker read-only client (API v3, stdlib only)."""
import argparse, configparser, datetime as dt, glob, hashlib, hmac, json, os, re, sys, time
import ssl, urllib.request, urllib.error

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

READ_ONLY = {
    "getPositionJson", "getNotifyOrderJson", "getOrdersHistory", "getTradesHistory",
    "getStockQuotesJson", "getBrokerReport", "GetAllUserTexInfo", "getOPQ",
    "getMarketStatus", "getSecurityInfo", "getHloc", "getNews", "getPlannedCorpActions",
    "getAlertsList", "getClientCpsHistory", "GetListTariffs", "getSecuritySessions",
}
# Data root (config, state, reports, journal): skills/<skill>/<script> -> two levels up; ~/.claude/skills/* are symlinks here
FB_HOME = os.path.expanduser(os.environ.get("FB_HOME") or
                             os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
REPORT_DIR = os.path.join(FB_HOME, "reports")  # + /YYYY-MM/
JOURNAL_DIR = os.path.join(FB_HOME, "journal")
CLASS_RU = {"stock": "акции", "index": "индекс", "hedge": "хедж", None: "—"}
CFG_PATHS = [os.path.join(FB_HOME, "config", "portfolio.ini"), "~/.claude/skills/fb-portfolio/portfolio.ini",
             "~/.config/tradernet/portfolio.ini"]
MENTORS_DIR = os.path.join(FB_HOME, "state", "mentors")
# How position values are computed; logged into runs.jsonl so comparisons know whether snapshots are comparable.
# The broker's market_value for stocks/ETFs is often stale (stuck for weeks), so qty * mkt_price is used instead.
VALUATION = "qty*mkt_price; bonds: market_value"


def read_ini(path=None):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    paths = [path] if path else CFG_PATHS
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            cfg.read(p, encoding="utf-8")
            break
    else:
        if path:
            sys.exit(f"Config not found: {path}")
    return cfg


def load_groups(path=None):
    """[groups] theme = TICKER, TICKER  -> {theme: [base tickers]}; limit is [rules] max_group_pct."""
    cfg = read_ini(path)
    if not cfg.has_section("groups"):
        return {}
    return {k: [x.strip().upper() for x in v.split(",") if x.strip()] for k, v in cfg["groups"].items()}


def load_cfg(path):
    cfg = read_ini(path)
    pub = os.environ.get("TRADERNET_PUBLIC_KEY")
    priv = os.environ.get("TRADERNET_PRIVATE_KEY")
    domain = os.environ.get("TRADERNET_DOMAIN") or "tradernet.global"
    if not pub or not priv:
        sys.exit("No API keys: export TRADERNET_PUBLIC_KEY / TRADERNET_PRIVATE_KEY in ~/.zprofile or ~/.zshrc and run via `zsh -lic`")
    classes = {k.upper(): v.strip().lower() for k, v in cfg["classes"].items()} if cfg.has_section("classes") else {}
    rules = dict(cfg["rules"]) if cfg.has_section("rules") else {}
    return pub, priv, domain, classes, rules


def call(pub, priv, domain, cmd, params=None):
    if cmd not in READ_ONLY:
        sys.exit(f"Command {cmd} is not in the read-only allowlist")
    payload = json.dumps(params or {}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(priv.encode(), (payload + ts).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"https://{domain}/api/{cmd}", data=payload.encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "tn-readonly/1.0",
                 "Accept": "application/json", "X-NtApi-PublicKey": pub,
                 "X-NtApi-Timestamp": ts, "X-NtApi-Sig": sig})
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {domain}: {e.read()[:300]!r}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error to {domain}: {e.reason}")
    if isinstance(res, dict) and (res.get("errMsg") or res.get("error")):
        sys.exit(f"API error: {res.get('errMsg') or res.get('error')} (code {res.get('code')})")
    return res


def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def base_ticker(t):
    """HSBK.KZ -> HSBK, FFSPC2.0929.AIX.KZ -> FFSPC2 (same rule as [classes] lookup)."""
    return (t or "").split(".")[0].upper()


def lookup(m, t):
    return m.get((t or "").upper()) or m.get(base_ticker(t))


def is_bond(r):
    return str(r.get("type") if "type" in r else r.get("t")) == "2"


def portfolio(res, classes, rules, groups=None):
    ps = (res.get("result") or res).get("ps", {})
    acc, pos = ps.get("acc", []) or [], ps.get("pos", []) or []
    rates = {a.get("curr"): num(a.get("currval"), 1) for a in acc}
    for p in pos:
        rates.setdefault(p.get("curr"), num(p.get("currval"), 1))
    usd = rates.get("USD") or 1.0
    to_usd = lambda v, c: v * (rates.get(c, 1.0) / usd)

    cash = [{"currency": a.get("curr"), "amount": num(a.get("s")),
             "usd": round(to_usd(num(a.get("s")), a.get("curr")), 2)} for a in acc]
    rows, gaps = [], []
    for p in pos:
        q, px, cur = num(p.get("q")), num(p.get("mkt_price")), p.get("curr")
        mv_b = num(p.get("market_value"), None) if p.get("market_value") not in (None, "") else None
        avg = num(p.get("bal_price_a"), None) if p.get("bal_price_a") not in (None, "") else None
        # profit_close = broker's unrealized P&L (computed from profit_price, which is a price, not P&L)
        pc = num(p.get("profit_close"), None) if p.get("profit_close") not in (None, "") else None
        bond = is_bond(p)
        if bond:  # price is % of face; market_value = qty * face * price / 100 (checked)
            face = num(p.get("face_val_a")) or 100.0
            mv = mv_b if mv_b is not None else q * face * px / 100
            cost = avg * q * face / 100 if avg else None
            upl = pc if pc is not None else (mv - cost if cost else None)
            pct = upl / cost * 100 if cost and upl is not None else None
        else:  # market_value is often stale for stocks/ETFs -> value, P&L and P&L % all from mkt_price
            face, mv = None, q * px
            upl = (px - avg) * q if avg else pc
            pct = (px / avg - 1) * 100 if avg else None
        t = p.get("i", "")
        row = {
            "ticker": t, "name": p.get("name") or p.get("name2"), "type": p.get("t"),
            "qty": q, "currency": cur, "price": px, "avg_price": avg,
            "value": round(mv, 2), "value_usd": round(to_usd(mv, cur), 2),
            "unrealized_pl": None if upl is None else round(upl, 2),
            "unrealized_pl_usd": None if upl is None else round(to_usd(upl, cur), 2),
            "unrealized_pl_pct": None if pct is None else round(pct, 2),
            "class": lookup(classes, t),
        }
        if bond:
            row.update(face=face, maturity=(p.get("maturity_d") or "")[:10] or None,
                       accrued_usd=round(to_usd(num(p.get("accruedint_a")) * q, cur), 2))
        if mv_b is not None and mv and abs(mv_b / mv - 1) > 0.01:
            gaps.append({"ticker": t, "value_usd": row["value_usd"], "value_broker_usd": round(to_usd(mv_b, cur), 2),
                         "gap_pct": round((mv_b / mv - 1) * 100, 2)})
        rows.append(row)
    pos_usd = sum(r["value_usd"] for r in rows)
    cash_usd = sum(c["usd"] for c in cash)
    total = pos_usd + cash_usd
    upl_usd = sum(r["unrealized_pl_usd"] or 0 for r in rows)
    cost_usd = pos_usd - upl_usd
    max_pos = num(rules.get("max_position_pct"), 5)
    small_pct = num(rules.get("small_position_pct"), 1)
    targets = {k: num(rules.get(f"target_{k}"), v) for k, v in (("stock", 40), ("index", 30), ("hedge", 30))}
    for r in rows:
        r["weight_pct"] = round(r["value_usd"] / total * 100, 2) if total else 0
    rows.sort(key=lambda r: -r["value_usd"])
    by_class = {}
    for r in rows:
        k = r["class"] or "unclassified"
        by_class[k] = by_class.get(k, 0) + r["value_usd"]
    structure = {k: {"usd": round(v, 2), "pct_of_positions": round(v / pos_usd * 100, 2) if pos_usd else 0,
                     "pct_of_net_assets": round(v / total * 100, 2) if total else 0,
                     "target_pct": targets.get(k)} for k, v in by_class.items()}

    max_grp = num(rules.get("max_group_pct"), 10)
    grp = []
    for name, tickers in (groups or {}).items():
        held = [r for r in rows if r["ticker"].upper() in tickers or base_ticker(r["ticker"]) in tickers]
        v = sum(r["value_usd"] for r in held)
        w = v / total * 100 if total else 0
        grp.append({"name": name, "tickers": [r["ticker"] for r in held], "usd": round(v, 2), "pct": round(w, 2),
                    "over_usd": round(v - total * max_grp / 100, 2) if w > max_grp else None,
                    "status": "over" if w > max_grp else "near" if w > max_grp * 0.85 else "ok"})
    grp.sort(key=lambda g: -g["usd"])
    by_cur = {}
    for r in rows:
        by_cur[r["currency"]] = by_cur.get(r["currency"], 0) + r["value_usd"]
    for c in cash:
        by_cur[c["currency"]] = by_cur.get(c["currency"], 0) + c["usd"]
    currency = sorted(({"currency": k, "usd": round(v, 2), "pct": round(v / total * 100, 2) if total else 0}
                       for k, v in by_cur.items() if round(v, 2)), key=lambda x: -x["usd"])
    return {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "fx_rates_vs_usd": {c: round(r / usd, 6) for c, r in rates.items() if c},
        "totals_usd": {"net_assets": round(total, 2), "positions": round(pos_usd, 2), "cash": round(cash_usd, 2),
                       "unrealized_pl": round(upl_usd, 2), "cost_basis": round(cost_usd, 2),
                       "unrealized_pl_pct": round(upl_usd / cost_usd * 100, 2) if cost_usd else None},
        "cash": cash, "positions": rows, "structure": structure,
        "rules": {"base": "net_assets", "max_position_pct": max_pos,
                  "over_limit": [r["ticker"] for r in rows if r["weight_pct"] > max_pos],
                  "near_limit": [r["ticker"] for r in rows if max_pos * 0.85 < r["weight_pct"] <= max_pos],
                  "unclassified": [r["ticker"] for r in rows if not r["class"]],
                  "small_position_pct": small_pct,
                  "small": [r["ticker"] for r in rows if r["weight_pct"] < small_pct]},
        "concentration": {"max_group_pct": max_grp, "groups": grp},
        "currency_exposure": currency,
        "data_checks": {"valuation": VALUATION, "broker_value_gaps": sorted(gaps, key=lambda g: g["gap_pct"]),
                        "broker_value_gap_usd": round(sum(g["value_broker_usd"] - g["value_usd"] for g in gaps), 2)},
    }


# ---------- extras for the portfolio summary ----------

def commissions(trades, fx):
    """Broker commissions on all my trades (incl. FX conversions), USD at today's rates."""
    today = dt.date.today()
    tot = {"month": 0.0, "ytd": 0.0, "all": 0.0}
    for t in trades:
        v = t["commission"] * fx.get(t["commission_currency"] or "USD", 1.0)
        d = t["date"] or ""
        tot["all"] += v
        tot["ytd"] += v if d.startswith(f"{today:%Y}") else 0
        tot["month"] += v if d.startswith(f"{today:%Y-%m}") else 0
    return {k: round(v, 2) for k, v in tot.items()}


def pnl_summary(d, trades):
    t, r = d["totals_usd"], d["sales"]["realized_usd"]
    return {"unrealized_usd": t["unrealized_pl"], "unrealized_pct": t["unrealized_pl_pct"],
            "realized_all_usd": r["all"], "total_usd": round(r["all"] + t["unrealized_pl"], 2),
            "commissions_usd": commissions(trades, d["fx_rates_vs_usd"])}


def coupon_rate(q):
    """Annual coupon, %. `cpn` means different things by exchange (KASE: annual %, AIX: payment per bond per
    period of `cpp` days) — pick the reading that reproduces the accrued interest `acd`."""
    cpn, cpp, fv, acd = num(q.get("cpn")), num(q.get("cpp")), num(q.get("fv")), num(q.get("acd"))
    ncp = num(q.get("ncp"), None)
    if not (cpn and cpp and fv and acd) or ncp is None or not 0 < cpp - ncp <= cpp:
        return None
    el = cpp - ncp
    annual, per_period = cpn, cpn / fv * 100 * 365 / cpp
    return round(annual if abs(fv * annual / 100 * el / 365 - acd) <= abs(cpn * el / cpp - acd) else per_period, 2)


def bond_info(c, d):
    bonds = [r for r in d["positions"] if is_bond(r)]
    if not bonds:
        return []
    res = c("getStockQuotesJson", {"tickers": ",".join(r["ticker"] for r in bonds)})
    qs = (res.get("result") or res).get("q", []) if isinstance(res, dict) else []
    by = {q.get("c"): q for q in qs or [] if isinstance(q, dict)}
    today, out = dt.date.today(), []
    for r in bonds:
        q, mat = by.get(r["ticker"], {}), r.get("maturity")
        days = (dt.date.fromisoformat(mat) - today).days if mat else None
        out.append({"ticker": r["ticker"], "name": r["name"], "currency": r["currency"], "qty": r["qty"],
                    "face": r["face"], "price_pct": r["price"], "value_usd": r["value_usd"], "maturity": mat,
                    "days_to_maturity": days, "matured": days is not None and days < 0,
                    "next_coupon": (q.get("ncd") or "")[:10] or None, "coupon_pct": coupon_rate(q),
                    "ytm_pct": num(q.get("yld")) or None, "accrued_usd": r["accrued_usd"]})
    return out


def load_mentors():
    """Last mentor snapshots (state/mentors/*.json, written by fb-invest) -> {BASE: [{portfolio, rec, sl, tp}]}."""
    out = {}
    for f in sorted(glob.glob(os.path.join(MENTORS_DIR, "*.json"))):
        name = re.sub(r"^портфель_", "", os.path.splitext(os.path.basename(f))[0]).replace("_", " ")
        try:
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        for t, v in (st.get("positions") or {}).items():
            out.setdefault(t.upper(), []).append({"portfolio": name, "rec": v.get("rec"), "sl": v.get("sl"),
                                                  "tp": v.get("tp"), "taken": st.get("taken")})
    return out


def my_stops(orders_res):
    """Active take-profit (type 6) / stop-loss (type 5) sell orders by full ticker."""
    res = orders_res.get("result", orders_res) if isinstance(orders_res, dict) else {}
    lst = ((res.get("orders") or {}).get("order") or []) if isinstance(res, dict) else []
    out = {}
    for o in [lst] if isinstance(lst, dict) else lst:
        if o.get("oper") in (3, 4) and o.get("type") in (5, 6):
            out.setdefault((o.get("instr") or "").upper(), {})["tp" if o["type"] == 6 else "sl"] = num(o.get("stop"), None)
    return out


def review(d, rules, mentors, stops):
    """Positions down more than review_loss_pct vs avg price, with my stops and mentors' levels."""
    lim = num(rules.get("review_loss_pct"), 30)
    out = []
    for r in sorted(d["positions"], key=lambda r: r["unrealized_pl_pct"] or 0):
        if r["unrealized_pl_pct"] is None or r["unrealized_pl_pct"] > -lim:
            continue
        m = mentors.get(base_ticker(r["ticker"])) or mentors.get(r["ticker"].upper()) or []
        s = stops.get(r["ticker"].upper(), {})
        out.append({"ticker": r["ticker"], "price": r["price"], "value_usd": r["value_usd"],
                    "pl_usd": r["unrealized_pl_usd"], "pl_pct": r["unrealized_pl_pct"],
                    "my_tp": s.get("tp"), "my_sl": s.get("sl"), "mentors": m,
                    "below_mentor_sl": any(x["sl"] and r["price"] < x["sl"] for x in m)})
    return {"loss_pct": lim, "positions": out}


def prev_run(as_of):
    """Last fb-portfolio run from an earlier day (else the last one at all) — the baseline for «since last time»."""
    runs = [r for r in journal_read("runs.jsonl") if r.get("skill") == "fb-portfolio" and r.get("ts") != as_of]
    older = [r for r in runs if r["ts"][:10] < as_of[:10]]
    return (older or runs or [None])[-1]


def holdings(d):
    return {r["ticker"]: {"q": r["qty"], "px": r["price"], "m": r["face"] / 100 if is_bond(r) else 1,
                          "cur": r["currency"]} for r in d["positions"]}


def compare(d, prev):
    if not prev:
        return None
    t = d["totals_usd"]
    delta = lambda now, was: {"from": was, "to": now, "delta": None if was is None else round(now - was, 2)}
    out = {"since": prev["ts"], "comparable": prev.get("valuation") == VALUATION,
           "net_assets": delta(t["net_assets"], prev.get("net_assets")),
           "unrealized_pl": delta(t["unrealized_pl"], prev.get("unrealized_pl")),
           "cash": delta(t["cash"], prev.get("cash")),
           "realized_all": delta(d["sales"]["realized_usd"]["all"], (prev.get("realized_pl") or {}).get("all")),
           "structure_pp": {k: round(v["pct_of_net_assets"] - (prev.get("structure_pct") or {}).get(k, 0), 2)
                            for k, v in d["structure"].items()}}
    ph = prev.get("holdings")
    if ph:
        now, fx = {r["ticker"]: r for r in d["positions"]}, d["fx_rates_vs_usd"]
        out["opened"] = [k for k in now if k not in ph]
        out["closed"] = [k for k in ph if k not in now]
        out["qty_changed"] = [{"ticker": k, "from": ph[k]["q"], "to": r["qty"]} for k, r in now.items()
                              if k in ph and ph[k]["q"] != r["qty"]]
        reval = sum(min(ph[k]["q"], r["qty"]) * (r["price"] - ph[k]["px"]) * ph[k]["m"] * fx.get(r["currency"], 1.0)
                    for k, r in now.items() if k in ph)
        out["revaluation_usd"] = round(reval, 2)
        if out["net_assets"]["delta"] is not None:
            out["other_usd"] = round(out["net_assets"]["delta"] - reval, 2)
    return out


def fmt(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ")


def sfmt(x, d=2):
    return "—" if x is None else f"{x:+,.{d}f}".replace(",", " ")


def smoney(x):
    return "—" if x is None else ("-" if x < 0 else "+") + "$" + fmt(abs(x))


def qfmt(q):
    return fmt(q, 0 if float(q).is_integer() else 4)


def pos_table(rows, lim, numbered=False):
    L = [("| # " if numbered else "") + "| Тикер | Название | Кол-во | Цена | Вал. | Стоимость $ | Доля % | P&L $ | P&L % | Класс |",
         ("|--:" if numbered else "") + "|---|---|--:|--:|---|--:|--:|--:|--:|---|"]
    for i, r in enumerate(rows, 1):
        w = f"**{r['weight_pct']:.2f}**" if r["weight_pct"] > lim else f"{r['weight_pct']:.2f}"
        px = fmt(r["price"], 4 if r["price"] < 1 else 2) + ("%" if is_bond(r) else "")
        L.append((f"| {i} " if numbered else "") +
                 f"| {r['ticker']} | {(r['name'] or '').replace('|', '/')} | {qfmt(r['qty'])} | {px} | {r['currency']} "
                 f"| {fmt(r['value_usd'])} | {w} | {fmt(r['unrealized_pl_usd'])} | {sfmt(r['unrealized_pl_pct'], 1)} "
                 f"| {CLASS_RU.get(r['class'], r['class'])} |")
    return L


def render_md(d, top=10):
    t, rl = d["totals_usd"], d["rules"]
    net, lim = t["net_assets"], rl["max_position_pct"]
    L = [f"# Портфель Freedom Broker — {d['as_of'].replace('T', ' ')}", "",
         f"**Чистые активы:** ${fmt(net)} · позиции ${fmt(t['positions'])} · кэш ${fmt(t['cash'])}  ",
         "**Кэш:** " + (", ".join(f"{fmt(c['amount'])} {c['currency']} (${fmt(c['usd'])})" for c in d["cash"] if c["amount"])
                       or "нет") + "  ",
         f"**Нереализованный P&L:** {smoney(t['unrealized_pl'])}"
         + (f" ({t['unrealized_pl_pct']:+.1f}% к вложенным ${fmt(t['cost_basis'])})" if t.get("unrealized_pl_pct") is not None else "")
         + "  "]
    s, pn = d.get("sales"), d.get("pnl")
    if s:
        r = s["realized_usd"]
        L.append(f"**Реализованный P&L (продажи)¹:** месяц ${fmt(r['month'])} · с начала года ${fmt(r['ytd'])} "
                 f"· всего ${fmt(r['all'])}  ")
    if pn:
        cm = pn["commissions_usd"]
        L.append(f"**Итого P&L:** {smoney(pn['total_usd'])} (реализованный за всё время {smoney(pn['realized_all_usd'])} "
                 f"и нереализованный {smoney(pn['unrealized_usd'])})  ")
        L.append(f"**Комиссии брокера:** месяц ${fmt(cm['month'])} · с начала года ${fmt(cm['ytd'])} · всего ${fmt(cm['all'])} "
                 "(показаны отдельно, из P&L не вычитались)")
    if s:
        L += ["", "¹ Реализованный P&L — поле `profit` брокера в валюте сделки; сделки в KZT пересчитаны по сегодняшнему "
                  "курсу, поэтому сумма приблизительная. Конвертации валют не учитываются."]

    v = d.get("vs_prev")
    if v:
        na = v["net_assets"]
        L += ["", f"## С прошлого снимка ({v['since'][:16].replace('T', ' ')})", ""]
        line = f"- **Чистые активы:** ${fmt(na['from'])} → ${fmt(na['to'])} ({smoney(na['delta'])})"
        if v.get("revaluation_usd") is not None:
            line += (f": переоценка позиций ≈ {smoney(v['revaluation_usd'])}, прочее ≈ {smoney(v.get('other_usd'))} "
                     "(пополнения/выводы, сделки, купоны, комиссии, курс валют)")
        L.append(line)
        L.append(f"- **Нереализованный P&L:** {smoney(v['unrealized_pl']['delta'])} · **кэш:** {smoney(v['cash']['delta'])} "
                 f"· **реализованный (всего):** {smoney(v['realized_all']['delta'])}")
        L.append("- **Структура, п.п.:** " + ", ".join(f"{CLASS_RU.get(k, k) if k != 'unclassified' else 'без класса'} {pp:+.1f}"
                                                     for k, pp in v["structure_pp"].items()))
        if "opened" in v:
            ch = [f"новые: {', '.join(v['opened'])}" if v["opened"] else "",
                  f"закрыты: {', '.join(v['closed'])}" if v["closed"] else "",
                  "изменилось кол-во: " + ", ".join(f"{x['ticker']} {qfmt(x['from'])}→{qfmt(x['to'])}" for x in v["qty_changed"])
                  if v["qty_changed"] else ""]
            L.append("- **Позиции:** " + ("; ".join(x for x in ch if x) or "без изменений"))
        else:
            L.append("- Состав позиций сравнить нельзя: в прошлом снимке он не сохранялся.")
        if not v["comparable"]:
            L.append("- ⚠️ Прошлый снимок считался по `market_value` брокера (часто устаревшему), сейчас — по кол-ву × цене; "
                     "часть разницы — из-за смены метода оценки.")

    rows = d["positions"]
    L += ["", f"## Топ-{min(top, len(rows))} позиций", ""] + pos_table(rows[:top], lim, numbered=True)
    rest = rows[top:]
    if rest:
        rv = sum(r["value_usd"] for r in rest)
        L += ["", f"Остальные {len(rest)} позиций — ${fmt(rv)} ({rv / net * 100:.1f}% активов).", "",
              "<details><summary>Все позиции по классам</summary>", ""]
        order = sorted(d["structure"].items(), key=lambda kv: -kv[1]["usd"])
        for k, sv in order:
            grp = [r for r in rows if (r["class"] or "unclassified") == k]
            L += [f"**{CLASS_RU.get(k, k) if k != 'unclassified' else 'без класса'}** — ${fmt(sv['usd'])} "
                  f"({sv['pct_of_net_assets']:.1f}%), {len(grp)} поз.", ""] + pos_table(grp, lim) + [""]
        L.append("</details>")

    small_pct = rl.get("small_position_pct", 1)
    small = [r for r in rows if r["weight_pct"] < small_pct]
    if small:
        sv = sum(r["value_usd"] for r in small)
        L += ["", f"## Мелкие позиции (< {small_pct:g}% активов): {len(small)} шт., ${fmt(sv)}", "",
              ", ".join(f"{r['ticker']} ${fmt(r['value_usd'])} ({sfmt(r['unrealized_pl_pct'], 1)}%)" for r in small)]

    L += ["", "## Структура", "", "| Класс | Факт $ | Факт % | Цель % | Отклонение $ |", "|---|--:|--:|--:|--:|"]
    for k, sv in sorted(d["structure"].items(), key=lambda kv: -kv[1]["usd"]):
        tgt = sv["target_pct"]
        dev = "—" if tgt is None else smoney(sv["usd"] - net * tgt / 100)
        L.append(f"| {CLASS_RU.get(k, k) if k != 'unclassified' else 'без класса'} | {fmt(sv['usd'])} "
                 f"| {sv['pct_of_net_assets']:.2f} | {'—' if tgt is None else f'{tgt:g}'} | {dev} |")
    cap = net * lim / 100
    by_t = {r["ticker"]: r for r in rows}
    L += ["", f"## Правило {lim:g}% от чистых активов (лимит ≈ ${fmt(cap)})", ""]
    L.append("- **Выше лимита:** " + (", ".join(f"{x} — {by_t[x]['weight_pct']:.2f}% (+${fmt(by_t[x]['value_usd'] - cap)})"
                                              for x in rl["over_limit"]) or "нет"))
    L.append(f"- **Близко к лимиту (>{lim * 0.85:g}%):** " + (", ".join(f"{x} — {by_t[x]['weight_pct']:.2f}%"
                                                                      for x in rl["near_limit"]) or "нет"))

    cn = d.get("concentration") or {}
    if cn.get("groups"):
        g_lim = cn["max_group_pct"]
        st = {"over": "**выше лимита**", "near": "близко", "ok": "ок"}
        L += ["", f"## Концентрация по темам (лимит {g_lim:g}% от чистых активов)", "",
              "| Тема | Бумаги | $ | % | Статус |", "|---|---|--:|--:|---|"]
        for g in cn["groups"]:
            L.append(f"| {g['name']} | {', '.join(g['tickers']) or '—'} | {fmt(g['usd'])} | {g['pct']:.2f} | "
                     + st[g["status"]] + (f" (+${fmt(g['over_usd'])})" if g["over_usd"] else "") + " |")
    if d.get("currency_exposure"):
        L += ["", "## Валюты (позиции + кэш)", "", "| Валюта | $ | % |", "|---|--:|--:|"]
        L += [f"| {c['currency']} | {fmt(c['usd'])} | {c['pct']:.2f} |" for c in d["currency_exposure"]]

    bonds = d.get("bonds")
    if bonds:
        L += ["", "## Облигации", "",
              "| Тикер | Вал. | Кол-во | Номинал | Цена, % ном. | Стоимость $ | Погашение | Дней | Купон, % год. | YTM % | След. купон | НКД $ |",
              "|---|---|--:|--:|--:|--:|---|--:|--:|--:|---|--:|"]
        for b in bonds:
            days = "**погашена**" if b["matured"] else ("—" if b["days_to_maturity"] is None else str(b["days_to_maturity"]))
            L.append(f"| {b['ticker']} | {b['currency']} | {qfmt(b['qty'])} | {fmt(b['face'], 0)} | {fmt(b['price_pct'], 2)} "
                     f"| {fmt(b['value_usd'])} | {b['maturity'] or '—'} | {days} | {fmt(b['coupon_pct'])} | {fmt(b['ytm_pct'])} "
                     f"| {b['next_coupon'] or '—'} | {fmt(b['accrued_usd'])} |")
        for b in bonds:
            if b["matured"]:
                L.append(f"\n⚠️ {b['ticker']}: срок погашения {b['maturity']} прошёл, но бумага ещё в позициях "
                         f"(${fmt(b['value_usd'])}) — проверьте, пришла ли выплата номинала.")
        L += ["", "Стоимость облигаций — без НКД. Купон определён по полям котировки (`cpn`/`cpp`/`acd`), это оценка."]

    rv = d.get("review")
    if rv and rv["positions"]:
        mdate = next((m["taken"][:10] for p in rv["positions"] for m in p["mentors"] if m.get("taken")), None)
        L += ["", f"## На пересмотр (убыток ≥ {rv['loss_pct']:g}%)", "",
              "| Тикер | Цена | Стоимость $ | P&L $ | P&L % | Мои TP / SL | Наставники: рек., TP / SL |", "|---|--:|--:|--:|--:|---|---|"]
        for p in rv["positions"]:
            mine = f"{fmt(p['my_tp'])} / {fmt(p['my_sl'])}" if p["my_tp"] or p["my_sl"] else "нет"
            ms = "; ".join(f"{m['portfolio']}: {m['rec'] or '—'}, {fmt(m['tp'])} / {fmt(m['sl'])}" for m in p["mentors"]) or "нет у наставников"
            if p["below_mentor_sl"]:
                ms += " ⚠️ цена ниже SL наставника"
            L.append(f"| {p['ticker']} | {fmt(p['price'], 4 if p['price'] < 1 else 2)} | {fmt(p['value_usd'])} "
                     f"| {fmt(p['pl_usd'])} | {p['pl_pct']:+.1f} | {mine} | {ms} |")
        tot = sum(p["pl_usd"] or 0 for p in rv["positions"])
        L += ["", f"В сумме {smoney(tot)} нереализованного P&L, {len(rv['positions'])} позиций."
              + (f" Данные наставников — снимок `state/mentors/` от {mdate}." if mdate else "")]

    dc = d.get("data_checks")
    if dc:
        L += ["", "## Проверка данных", "",
              "- Стоимость акций/ETF = кол-во × текущая цена (`mkt_price`); облигаций — `market_value` брокера "
              "(цена — % от номинала). P&L и P&L % считаются от той же цены.",
              f"- Доли и лимиты — от чистых активов (позиции + кэш, ${fmt(net)})."]
        g = dc["broker_value_gaps"]
        if g:
            L.append(f"- У {len(g)} бумаг `market_value` брокера расходится с кол-вом × ценой больше чем на 1% "
                     f"(в сумме {smoney(dc['broker_value_gap_usd'])}) — брокер, похоже, отдаёт устаревшую оценку: "
                     + ", ".join(f"{x['ticker']} {x['gap_pct']:+.1f}%" for x in g) + ".")

    if s and s["recent"]:
        L += ["", f"## Мои продажи за {s['recent_days']} дн.", "",
              "| Дата | Тикер | Кол-во | Цена | Вал. | Сумма | P&L $ | Ещё держу |", "|---|---|--:|--:|---|--:|--:|---|"]
        for o in s["recent"]:
            L.append(f"| {o['date'][:10]} | {o['ticker']} | {qfmt(o['qty'])} "
                     f"| {fmt(o['price'])} | {o['currency']} | {fmt(o['sum'])} | {fmt(o['realized_pl_usd'])} "
                     f"| {'да' if o['still_held'] else 'нет'} |")
    if rl["unclassified"]:
        L += ["", "## Без класса", "", "Добавьте в `[classes]` файла `portfolio.ini`: " + ", ".join(rl["unclassified"])]
    L += ["", "_Не является финансовым советом._", ""]
    return "\n".join(L)


def report_path(out_dir, prefix, as_of):
    """reports/YYYY-MM/<prefix>_YYYY-MM-DD_HH-MM.md (month subfolder only for the default dir)."""
    out_dir = os.path.expanduser(out_dir)
    if os.path.realpath(out_dir) == os.path.realpath(REPORT_DIR):
        out_dir = os.path.join(out_dir, as_of[:7])
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{prefix}_{as_of[:16].replace(':', '-').replace('T', '_')}.md")


def save_md(d, out_dir):
    path = report_path(out_dir, "portfolio", d["as_of"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_md(d))
    return path


# ---------- journal (append-only JSONL, never keys / sheet links / login) ----------

def journal_append(name, *records):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    with open(os.path.join(JOURNAL_DIR, name), "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def journal_read(name):
    p = os.path.join(JOURNAL_DIR, name)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def log_portfolio_run(d):
    t = d["totals_usd"]
    journal_append("runs.jsonl", {
        "ts": d["as_of"], "skill": "fb-portfolio", "net_assets": t["net_assets"], "cash": t["cash"],
        "unrealized_pl": t["unrealized_pl"], "positions": len(d["positions"]),
        "structure_pct": {k: v["pct_of_net_assets"] for k, v in d["structure"].items()},
        "over_limit": d["rules"]["over_limit"],
        "realized_pl": (d.get("sales") or {}).get("realized_usd"), "report_md": d.get("report_md"),
        "valuation": VALUATION, "total_pl": (d.get("pnl") or {}).get("total_usd"),
        "groups_over": [g["name"] for g in d.get("concentration", {}).get("groups", []) if g["status"] == "over"],
        "holdings": holdings(d)})


def trade_row(t):
    side = {"1": "Buy", "2": "Sell"}.get(str(t.get("type")), str(t.get("type")))
    return {"id": str(t.get("id")), "order_id": str(t.get("order_id")), "date": t.get("date"), "side": side,
            "ticker": t.get("instr_nm"), "qty": num(t.get("q")), "price": num(t.get("p")),
            "sum": num(t.get("summ") or t.get("v")), "currency": t.get("curr_c"),
            "commission": num(t.get("commission")), "commission_currency": t.get("commission_currency"),
            "realized_pl": num(t.get("profit"))}


def sync_trades(c, since=None, write=True):
    """Broker trades not yet in journal/trades.jsonl (dedup by trade id) -> (summary, all trades).
    write=False fetches without appending (dry runs)."""
    have = journal_read("trades.jsonl")
    seen = {r["id"] for r in have}
    if not since:
        last = max((r["date"] for r in have), default=None)
        since = str(dt.date.fromisoformat(last[:10]) - dt.timedelta(days=7)) if last else "2020-01-01"
    res = c("getTradesHistory", {"beginDate": since, "endDate": str(dt.date.today())})
    lst = ((res.get("trades") or {}).get("trade") or []) if isinstance(res, dict) else []
    lst = [lst] if isinstance(lst, dict) else lst
    new = sorted((trade_row(t) for t in lst if str(t.get("id")) not in seen), key=lambda r: r["date"] or "")
    if write:
        journal_append("trades.jsonl", *new)
    return ({"since": since, "fetched": len(lst), "added": len(new), "total": len(have) + len(new), "new": new},
            have + new)


def journal_sync(c, since=None):
    return sync_trades(c, since)[0]


def sales(trades, fx, days=31, held=()):
    """My sell orders (fills merged by order_id, FX conversions skipped) + realized P&L in USD.
    realized_pl is the broker's `profit`, in trade currency; converted at today's rate."""
    usd = lambda v, cur: v * fx.get(cur, 1.0)
    held = {h.upper() for h in held}
    orders = {}
    for t in trades:
        if t["side"] != "Sell" or "/" in (t["ticker"] or ""):
            continue
        o = orders.setdefault(t["order_id"], {"date": t["date"], "ticker": t["ticker"], "currency": t["currency"],
                                              "qty": 0.0, "sum": 0.0, "realized_pl": 0.0, "commission_usd": 0.0})
        o["qty"] += t["qty"]; o["sum"] += t["sum"]; o["realized_pl"] += t["realized_pl"]
        o["commission_usd"] += usd(t["commission"], t["commission_currency"] or "USD")
        o["date"] = max(o["date"] or "", t["date"] or "")
    rows = []
    for o in orders.values():
        o.update(price=round(o["sum"] / o["qty"], 4) if o["qty"] else None, sum=round(o["sum"], 2),
                 realized_pl=round(o["realized_pl"], 2), commission_usd=round(o["commission_usd"], 2),
                 realized_pl_usd=round(usd(o["realized_pl"], o["currency"]), 2),
                 still_held=o["ticker"].upper() in held)
        rows.append(o)
    rows.sort(key=lambda o: o["date"], reverse=True)
    today = dt.date.today()
    since = str(today - dt.timedelta(days=days))
    tot = lambda pre: round(sum(o["realized_pl_usd"] for o in rows if o["date"].startswith(pre)), 2)
    return {"realized_usd": {"month": tot(f"{today:%Y-%m}"), "ytd": tot(f"{today:%Y}"), "all": tot("")},
            "recent_days": days, "recent": [o for o in rows if o["date"][:10] >= since]}


def journal_note(text, tags=None):
    now = dt.datetime.now()
    path = os.path.join(JOURNAL_DIR, f"{now:%Y-%m}.md")
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    head = "" if os.path.isfile(path) else f"# Журнал решений — {now:%Y-%m}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{head}\n## {now:%Y-%m-%d %H:%M}" + (f" · {', '.join(tags)}" if tags else "") + f"\n\n{text.strip()}\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--domain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("portfolio"); s.add_argument("--raw", action="store_true")
    s.add_argument("--md-dir", default=os.environ.get("TRADERNET_REPORT_DIR") or REPORT_DIR)
    s.add_argument("--no-md", action="store_true")
    s = sub.add_parser("orders"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("trades"); s.add_argument("--from", dest="f", default=str(dt.date.today() - dt.timedelta(days=30))); s.add_argument("--to", default=str(dt.date.today())); s.add_argument("--ticker")
    s = sub.add_parser("orders-history"); s.add_argument("--from", dest="f", default=str(dt.date.today() - dt.timedelta(days=30))); s.add_argument("--to", default=str(dt.date.today()))
    s = sub.add_parser("quotes"); s.add_argument("tickers", nargs="+")
    s = sub.add_parser("report"); s.add_argument("--from", dest="f", required=True); s.add_argument("--to", required=True); s.add_argument("--part", default="account_at_end")
    s = sub.add_parser("raw"); s.add_argument("api_cmd"); s.add_argument("params", nargs="?", default="{}")
    s = sub.add_parser("journal-sync"); s.add_argument("--from", dest="f")
    s = sub.add_parser("note"); s.add_argument("text"); s.add_argument("--tag", action="append")
    a = ap.parse_args()
    if a.cmd == "note":  # no API keys needed
        print(json.dumps({"journal_md": journal_note(a.text, a.tag)}, ensure_ascii=False))
        return
    pub, priv, domain, classes, rules = load_cfg(a.config)
    domain = a.domain or domain
    c = lambda cmd, p=None: call(pub, priv, domain, cmd, p)

    if a.cmd == "portfolio":
        res = c("getPositionJson")
        out = res if a.raw else portfolio(res, classes, rules, load_groups(a.config))
        if not a.raw:
            trades = sync_trades(c, write=not a.no_md)[1]
            out["sales"] = sales(trades, out["fx_rates_vs_usd"], held=[r["ticker"] for r in out["positions"]])
            out["pnl"] = pnl_summary(out, trades)
            out["bonds"] = bond_info(c, out)
            out["review"] = review(out, rules, load_mentors(), my_stops(c("getNotifyOrderJson", {"active_only": 1})))
            out["vs_prev"] = compare(out, prev_run(out["as_of"]))
        if not a.raw and not a.no_md:
            out["report_md"] = save_md(out, a.md_dir)
            log_portfolio_run(out)
    elif a.cmd == "orders":
        out = c("getNotifyOrderJson", {"active_only": 0 if a.all else 1})
    elif a.cmd == "trades":
        p = {"beginDate": a.f, "endDate": a.to}
        if a.ticker: p["nt_ticker"] = a.ticker
        out = c("getTradesHistory", p)
    elif a.cmd == "orders-history":
        out = c("getOrdersHistory", {"from": f"{a.f}T00:00:00", "till": f"{a.to}T23:59:59"})
    elif a.cmd == "quotes":
        res = c("getStockQuotesJson", {"tickers": ",".join(a.tickers)})
        qs = (res.get("result") or res).get("q", res) if isinstance(res, dict) else res
        out = [{"ticker": q.get("c"), "name": q.get("name"), "last": q.get("ltp"), "chg": q.get("chg"),
                "chg_pct": q.get("pcp"), "bid": q.get("bbp"), "ask": q.get("bap")} for q in qs] if isinstance(qs, list) else res
    elif a.cmd == "report":
        out = c("getBrokerReport", {"date_start": a.f, "date_end": a.to, "time_period": "23:59:59", "format": "json", "type": a.part})
    elif a.cmd == "journal-sync":
        out = journal_sync(c, a.f)
    elif a.cmd == "raw":
        out = c(a.api_cmd, json.loads(a.params))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
