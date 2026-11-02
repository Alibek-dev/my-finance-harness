#!/usr/bin/env python3
"""fb-advisor: досье по каждой позиции, вердикты в журнал, чек-лист перед покупкой, счёт советам (только чтение брокера).

Команды:
  prep [--offline]                    досье портфеля -> stdout + state/advisor/dossier.json
  save <verdicts.json>                md-отчёт + journal/advice.jsonl + runs.jsonl
  check TICKER --amount 100 [...]     чек-лист перед покупкой по последнему досье
  scorecard [--offline] [--min-days]  прошлые советы против текущей цены
"""
import argparse, datetime as dt, importlib.util, io, json, math, os, re, sys, urllib.request
from contextlib import redirect_stderr

TN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "fb-portfolio", "tn.py")
spec = importlib.util.spec_from_file_location("tn", TN_PATH)
tn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tn)

STATE_DIR = os.path.join(tn.FB_HOME, "state", "advisor")
DOSSIER = os.path.join(STATE_DIR, "dossier.json")
PRIMARY = "светлое будущее"
VERDICTS = ("ДОКУПИТЬ", "ДЕРЖАТЬ", "СОКРАТИТЬ", "ВЫЙТИ", "ЗАМЕНИТЬ")
CONF_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}
FX_PAIR = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
num = tn.num


def r2(x):
    return None if x is None else round(x, 2)


def root(t):
    """NFLX/10.ITS и NFLX/100.ITS -> NFLX.ITS: для сверки сделок, если брокер переименовал бумагу."""
    t = (t or "").upper()
    return re.split(r"[/_.]", t)[0] + "." + t.rsplit(".", 1)[-1]


# ---------- сделки: себестоимость, срок владения, продажи ----------

def trade_book(trades):
    """По каждому тикеру: остаток, себестоимость остатка (средняя цена), с какой даты держу, покупки, последняя продажа.
    Себестоимость в деньгах не меняется при сплите, поэтому среднюю считаем как cost / текущее кол-во."""
    book = {}
    for t in sorted(trades, key=lambda r: r.get("date") or ""):
        tk = (t.get("ticker") or "").upper()
        if not tk or FX_PAIR.match(tk):
            continue
        b = book.setdefault(tk, {"qty": 0.0, "cost": 0.0, "since": None, "buys": 0, "last_buy": None,
                                 "last_sell": None, "last_sell_price": None, "currency": t.get("currency")})
        q, px, d = num(t.get("qty")), num(t.get("price")), (t.get("date") or "")[:10]
        if t.get("side") == "Buy":
            if b["qty"] <= 1e-9:
                b["since"], b["cost"], b["qty"] = d, 0.0, 0.0
            b["qty"] += q; b["cost"] += q * px; b["buys"] += 1; b["last_buy"] = d
        elif t.get("side") == "Sell":
            avg = b["cost"] / b["qty"] if b["qty"] > 1e-9 else 0
            b["qty"] -= q; b["cost"] -= avg * q
            b["last_sell"], b["last_sell_price"] = d, px
            if b["qty"] <= 1e-9:
                b["qty"], b["cost"] = 0.0, 0.0
    return book


def book_for(book, ticker):
    t = ticker.upper()
    if t in book:
        return book[t]
    alts = [v for k, v in book.items() if root(k) == root(t) and v["qty"] > 0]
    return alts[0] if len(alts) == 1 else None


# ---------- источники данных ----------

def nbk_usd_kzt(day):
    url = f"https://nationalbank.kz/rss/get_rates.cfm?fdate={day:%d.%m.%Y}"
    req = urllib.request.Request(url, headers={"User-Agent": "fb-advisor/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=tn.SSL_CTX) as r:
        xml = r.read().decode("utf-8", "replace")
    m = re.search(r"<title>USD</title>\s*<pubDate>[^<]*</pubDate>\s*<description>([\d.]+)</description>", xml) or \
        re.search(r"<title>USD</title>.*?<description>([\d.]+)</description>", xml, re.S)
    if not m:
        raise ValueError("NBK: курс USD не найден")
    return float(m.group(1))


def online():
    """Живые данные брокера. tn.call() при ошибке делает sys.exit — ловим и возвращаем текст ошибки."""
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            pub, priv, domain, classes, rules = tn.load_cfg(None)
            c = lambda cmd, p=None: tn.call(pub, priv, domain, cmd, p)
            d = tn.portfolio(c("getPositionJson"), classes, rules, tn.load_groups())
            trades = tn.sync_trades(c, write=True)[1]
            stops = tn.my_stops(c("getNotifyOrderJson", {"active_only": 1}))
            bonds = {b["ticker"]: b for b in tn.bond_info(c, d)}
    except SystemExit as e:
        return None, str(e.code or err.getvalue()).strip()
    return {"d": d, "trades": trades, "stops": stops, "bonds": bonds, "rules": rules}, None


def offline(reason):
    """Без API: последний снимок fb-portfolio из runs.jsonl + сделки после него + курс НБК."""
    cfg = tn.read_ini()
    rules = dict(cfg["rules"]) if cfg.has_section("rules") else {}
    classes = {k.upper(): v.strip().lower() for k, v in cfg["classes"].items()} if cfg.has_section("classes") else {}
    runs = [r for r in tn.journal_read("runs.jsonl") if r.get("skill") == "fb-portfolio" and r.get("holdings")]
    if not runs:
        sys.exit("Нет снимка с holdings в journal/runs.jsonl и нет доступа к API: " + (reason or ""))
    run = runs[-1]
    trades = tn.journal_read("trades.jsonl")
    hold = {k: dict(v) for k, v in run["holdings"].items()}
    for t in trades:  # сделки после снимка
        tk = (t.get("ticker") or "").upper()
        if (t.get("date") or "") <= run["ts"] or FX_PAIR.match(tk):
            continue
        h = hold.setdefault(tk, {"q": 0.0, "px": num(t["price"]), "m": 1, "cur": t.get("currency"), "from_trade": True})
        h["q"] += num(t["qty"]) * (1 if t["side"] == "Buy" else -1)
        if h.get("from_trade"):
            h["px"] = num(t["price"])
    hold = {k: v for k, v in hold.items() if v["q"] > 1e-9}
    try:
        kzt = nbk_usd_kzt(dt.date.fromisoformat(run["ts"][:10]))
    except Exception as e:  # noqa: BLE001 — сеть/формат; без курса KZT-бумаги не оценить
        sys.exit(f"Офлайн-режим: не получил курс НБК ({e})")
    fx = {"USD": 1.0, "KZT": 1 / kzt}
    book = trade_book(trades)
    rows = []
    for tk, h in hold.items():
        mult = num(h.get("m"), 1) or 1
        value = h["q"] * h["px"] * mult
        b = book_for(book, tk)
        cost = b["cost"] * mult if b and b["cost"] else None  # у облигаций цена сделки — % номинала
        upl = value - cost if cost else None
        rows.append({"ticker": tk, "name": None, "type": "2" if mult != 1 or re.search(r"\.\d{4}\.AIX", tk) else None, "qty": h["q"],
                     "currency": h["cur"], "price": h["px"], "avg_price": r2(cost / h["q"] / mult) if cost else None,
                     "value_usd": r2(value * fx.get(h["cur"], 1)),
                     "unrealized_pl_usd": r2(upl * fx.get(h["cur"], 1)) if upl is not None else None,
                     "unrealized_pl_pct": r2(upl / cost * 100) if cost else None,
                     "class": tn.lookup(classes, tk), "price_from_trade": bool(h.get("from_trade"))})
    cash = num(run.get("cash"))
    total = sum(r["value_usd"] for r in rows) + cash
    for r in rows:
        r["weight_pct"] = r2(r["value_usd"] / total * 100)
    rows.sort(key=lambda r: -r["value_usd"])
    d = {"as_of": run["ts"], "fx_rates_vs_usd": fx,
         "totals_usd": {"net_assets": r2(total), "cash": cash, "positions": r2(total - cash),
                        "unrealized_pl": r2(sum(r["unrealized_pl_usd"] or 0 for r in rows))},
         "positions": rows}
    return {"d": d, "trades": trades, "stops": {}, "bonds": {}, "rules": rules, "classes": classes,
            "fx_note": f"KZT по курсу НБК {kzt} на {run['ts'][:10]}"}


# ---------- досье ----------

def build_dossier(src, source, api_error=None):
    d, rules, trades = src["d"], src["rules"], src["trades"]
    lim, review_pct = num(rules.get("max_position_pct"), 5), num(rules.get("review_loss_pct"), 30)
    small_pct, grp_lim = num(rules.get("small_position_pct"), 1), num(rules.get("max_group_pct"), 10)
    targets = {k: num(rules.get(f"target_{k}"), v) for k, v in (("stock", 40), ("index", 30), ("hedge", 30))}
    total = d["totals_usd"]["net_assets"]
    mentors, groups, book = tn.load_mentors(), tn.load_groups(), trade_book(trades)
    taken = sorted({m["taken"] for v in mentors.values() for m in v if m.get("taken")})
    today = dt.date.today()

    grp_usd = {g: sum(r["value_usd"] for r in d["positions"] if tn.base_ticker(r["ticker"]) in tks)
               for g, tks in groups.items()}
    cls_usd = {}
    for r in d["positions"]:
        cls_usd[r["class"] or "unclassified"] = cls_usd.get(r["class"] or "unclassified", 0) + r["value_usd"]

    out = []
    for r in d["positions"]:
        tk, w, pct = r["ticker"], r["weight_pct"] or 0, r["unrealized_pl_pct"]
        bond = tn.is_bond(r) or r.get("type") == "2"
        ms = (mentors.get(tn.base_ticker(tk)) or mentors.get(tk.upper())
              or (mentors.get(re.sub(r"\d+$", "", tn.base_ticker(tk))) if bond else None) or [])  # FFSPC2.0929.AIX.KZ -> FFSPC
        prim = next((m for m in ms if m["portfolio"] == PRIMARY), None)
        st = src["stops"].get(tk.upper(), {})
        b = book_for(book, tk)
        themes = [g for g, tks in groups.items() if tn.base_ticker(tk) in tks]
        bi = src["bonds"].get(tk) or {}
        flags, hints = [], []
        if w > lim:
            flags.append("over_limit")
            hints.append(f"выше лимита {lim:g}% на ${total * (w - lim) / 100:.0f} — сократить до лимита или не докупать")
        elif w > lim * 0.85:
            flags.append("near_limit"); hints.append("близко к лимиту — докупать нельзя")
        if w < small_pct:
            flags.append("small"); hints.append("мелкая (<1%): на итог не влияет — довести до значимой доли или выйти")
        if pct is not None and pct <= -review_pct:
            flags.append("deep_loss")
            hints.append(f"убыток {pct:.0f}% — проверить тезис; «вернётся к средней» не аргумент")
        if not ms and not bond:
            flags.append("no_mentor"); hints.append("нет ни в одном портфеле наставников — решение только на моём тезисе")
        if any(m.get("sl") and r["price"] < m["sl"] for m in ms):
            flags.append("below_mentor_sl"); hints.append("цена ниже SL наставников")
        for g in themes:
            if grp_usd[g] / total * 100 > grp_lim * 0.85:
                flags.append("theme_full"); hints.append(f"тема «{g}» {grp_usd[g] / total * 100:.1f}% при лимите {grp_lim:g}%")
        if bi.get("matured") or (r.get("maturity") and r["maturity"] < str(today)):
            flags.append("matured"); hints.append("срок погашения прошёл — проверить выплату номинала")
        if not r["class"]:
            flags.append("unclassified")
        if b and b.get("last_sell") and (today - dt.date.fromisoformat(b["last_sell"])).days <= 31:
            flags.append("sold_recently")
        held_days = (today - dt.date.fromisoformat(b["since"])).days if b and b.get("since") else None
        out.append({
            "ticker": tk, "name": r.get("name"), "class": r["class"], "bond": bond, "currency": r["currency"],
            "qty": r["qty"], "price": r["price"], "avg_price": r.get("avg_price"),
            "value_usd": r["value_usd"], "weight_pct": w, "pl_usd": r["unrealized_pl_usd"], "pl_pct": pct,
            "held_since": b and b.get("since"), "held_days": held_days, "buys": b and b.get("buys"),
            "last_buy": b and b.get("last_buy"), "themes": themes,
            "my_tp": st.get("tp"), "my_sl": st.get("sl"),
            "mentors": [{k: m[k] for k in ("portfolio", "rec", "tp", "sl")} for m in ms],
            "mentor_primary_rec": prim and prim["rec"],
            "maturity": r.get("maturity") or bi.get("maturity"), "coupon_pct": bi.get("coupon_pct"),
            "ytm_pct": bi.get("ytm_pct"), "price_from_trade": r.get("price_from_trade", False),
            "flags": flags, "hints": hints,
            "research": "light" if bond else "full",
            "priority": round(abs(r["unrealized_pl_usd"] or 0) + r["value_usd"] * 0.3 + 50 * ("deep_loss" in flags)
                              + 30 * ("over_limit" in flags), 1),
        })
    out.sort(key=lambda x: -x["priority"])

    ws = [x["value_usd"] / total for x in out if total]
    losers = sorted([x for x in out if (x["pl_usd"] or 0) < 0], key=lambda x: x["pl_usd"])
    loss_total = sum(x["pl_usd"] for x in losers)
    return {
        "as_of": d["as_of"], "built": dt.datetime.now().isoformat(timespec="seconds"),
        "source": source, "api_error": api_error, "fx_note": src.get("fx_note"),
        "mentors_snapshot": taken[-1] if taken else None,
        "totals_usd": d["totals_usd"],
        "rules": {"max_position_pct": lim, "review_loss_pct": review_pct, "small_position_pct": small_pct,
                  "max_group_pct": grp_lim, "targets": targets},
        "summary": {
            "positions": len(out),
            "effective_positions": round(1 / sum(w * w for w in ws), 1) if ws else 0,
            "small": [x["ticker"] for x in out if "small" in x["flags"]],
            "deep_loss": [x["ticker"] for x in out if "deep_loss" in x["flags"]],
            "over_limit": [x["ticker"] for x in out if "over_limit" in x["flags"]],
            "no_mentor": [x["ticker"] for x in out if "no_mentor" in x["flags"]],
            "loss_total_usd": r2(loss_total),
            "top5_losers_share_pct": r2(sum(x["pl_usd"] for x in losers[:5]) / loss_total * 100) if loss_total else None,
            "structure": {k: {"usd": r2(v), "pct": r2(v / total * 100), "target_pct": targets.get(k),
                              "gap_usd": r2(targets[k] * total / 100 - v) if k in targets else None}
                          for k, v in sorted(cls_usd.items())},
            "themes": {g: {"usd": r2(v), "pct": r2(v / total * 100)} for g, v in grp_usd.items()},
        },
        "positions": out,
    }


def cmd_prep(a):
    src, err = (None, "--offline") if a.offline else online()
    source = "online" if src else "offline"
    if not src:
        src = offline(err)
    dos = build_dossier(src, source, err if source == "offline" else None)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(DOSSIER, "w", encoding="utf-8") as f:
        json.dump(dos, f, ensure_ascii=False, indent=1)
    dos["dossier_path"] = DOSSIER
    print(json.dumps(dos, ensure_ascii=False, indent=1))


def load_dossier():
    if not os.path.isfile(DOSSIER):
        sys.exit("Нет досье: сначала `advisor.py prep`")
    with open(DOSSIER, encoding="utf-8") as f:
        return json.load(f)


# ---------- вердикты: md-отчёт + журнал ----------

def validate(v, dos):
    errs, warns = [], []
    held = {p["ticker"] for p in dos["positions"]}
    seen = set()
    for p in v.get("positions", []):
        t = p.get("ticker")
        if t not in held:
            errs.append(f"{t}: нет в досье")
        if p.get("verdict") not in VERDICTS:
            errs.append(f"{t}: verdict должен быть одним из {', '.join(VERDICTS)}")
        if not isinstance(p.get("score"), int) or not 1 <= p["score"] <= 10:
            errs.append(f"{t}: score — целое 1..10")
        if p.get("verdict") == "ЗАМЕНИТЬ" and not p.get("replace_with"):
            errs.append(f"{t}: для ЗАМЕНИТЬ нужен replace_with")
        if not p.get("sources") and not next(x for x in dos["positions"] if x["ticker"] == t)["bond"]:
            warns.append(f"{t}: нет источников (sources)")
        seen.add(t)
    for t in held - seen:
        warns.append(f"{t}: нет вердикта")
    return errs, warns


def fmt(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ")


def render_md(v, dos):
    pos = {p["ticker"]: p for p in dos["positions"]}
    s, t = dos["summary"], dos["totals_usd"]
    L = [f"# Разбор портфеля (fb-advisor) — {v['as_of'][:16].replace('T', ' ')}", ""]
    if dos["source"] == "offline":
        L += [f"> ⚠️ Данные брокера недоступны ({dos.get('api_error') or 'офлайн'}): позиции из снимка {dos['as_of'][:16]}, "
              f"цены на ту дату; {dos.get('fx_note') or ''}", ""]
    L += [f"**Активы:** ${fmt(t['net_assets'])} · нереализованный P&L ${fmt(t['unrealized_pl'])} · "
          f"позиций {s['positions']} (эффективно {s['effective_positions']}) · мелких {len(s['small'])} · "
          f"в убытке ≥{dos['rules']['review_loss_pct']:g}%: {len(s['deep_loss'])}", ""]
    if v.get("market"):
        m = v["market"]
        L += ["## Рынок", "", f"**Режим:** {m.get('regime', '—')}. {m.get('notes', '')}".strip(), ""]
    if v.get("summary"):
        L += ["## Главное", "", v["summary"].strip(), ""]
    if v.get("portfolio_actions"):
        L += ["## Что сделать с портфелем", ""] + [f"{i}. {x}" for i, x in enumerate(v["portfolio_actions"], 1)] + [""]
    order = {k: i for i, k in enumerate(("ВЫЙТИ", "ЗАМЕНИТЬ", "СОКРАТИТЬ", "ДОКУПИТЬ", "ДЕРЖАТЬ"))}
    ps = sorted(v["positions"], key=lambda p: (order[p["verdict"]], -(pos[p["ticker"]]["value_usd"])))
    L += ["## Вердикты по позициям", "",
          "| Тикер | Вердикт | Балл | Доля % | P&L % | Тезис | Купил бы сейчас | Уверенность | Действие |",
          "|---|---|--:|--:|--:|---|---|---|---|"]
    for p in ps:
        d = pos[p["ticker"]]
        act = p.get("action", "")
        if p["verdict"] == "ЗАМЕНИТЬ":
            act = f"→ {p['replace_with']}. {act}"
        L.append(f"| {p['ticker']} | **{p['verdict']}** | {p['score']} | {fmt(d['weight_pct'])} | {fmt(d['pl_pct'], 1)} | "
                 f"{p.get('thesis_status', '—')} | {'да' if p.get('would_buy_today') else 'нет'} | "
                 f"{CONF_RU.get(p.get('confidence'), '—')} | {act.replace('|', '/')} |")
    L += ["", "<details><summary>Обоснование по каждой позиции</summary>", ""]
    for p in ps:
        d = pos[p["ticker"]]
        lv = p.get("levels") or {}
        mtxt = ", ".join("%s: %s" % (m["portfolio"], m["rec"] or "—") for m in d["mentors"]) or "нет"
        L += [f"### {p['ticker']} — {p['verdict']} ({p['score']}/10)", "",
              f"Цена {fmt(d['price'])} {d['currency']}, средняя {fmt(d['avg_price'])}, P&L {fmt(d['pl_usd'])}$ "
              f"({fmt(d['pl_pct'], 1)}%), доля {fmt(d['weight_pct'])}%, держу с {d.get('held_since') or '—'}. "
              f"Наставники: {mtxt}.", ""]
        for k, title in (("thesis", "Тезис"), ("reasons", "Почему"), ("risks", "Риски")):
            if p.get(k):
                L.append(f"**{title}:** {p[k]}")
        if lv:
            L.append("**Уровни:** " + ", ".join(f"{k} {fmt(x)}" for k, x in lv.items() if x is not None))
        if p.get("sources"):
            L.append("**Источники:** " + " · ".join(p["sources"]))
        L.append("")
    L += ["</details>", ""]
    if v.get("buy_ideas"):
        L += ["## Идеи на покупку", "", "| Тикер | Сумма $ | Вход | TP | SL | Чек-лист | Почему |", "|---|--:|--:|--:|--:|---|---|"]
        for b in v["buy_ideas"]:
            L.append(f"| {b['ticker']} | {fmt(b.get('amount_usd'))} | {fmt(b.get('entry'))} | {fmt(b.get('tp'))} | "
                     f"{fmt(b.get('sl'))} | {b.get('check', '—')} | {b.get('why', '').replace('|', '/')} |")
        L.append("")
    L += [f"Данные наставников — снимок {dos.get('mentors_snapshot') or '—'}. "
          "Вердикты — суждение по открытым данным на дату отчёта; счёт прошлым советам: `advisor.py scorecard`.", "",
          "_Не является финансовым советом._"]
    return "\n".join(L) + "\n"


def cmd_save(a):
    dos = load_dossier()
    with open(a.file, encoding="utf-8") as f:
        v = json.load(f)
    v.setdefault("as_of", dt.datetime.now().isoformat(timespec="seconds"))
    errs, warns = validate(v, dos)
    if errs:
        print(json.dumps({"errors": errs, "warnings": warns}, ensure_ascii=False, indent=1))
        sys.exit(1)
    path = tn.report_path(tn.REPORT_DIR, "advisor", v["as_of"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_md(v, dos))
    pos = {p["ticker"]: p for p in dos["positions"]}
    recs = [{"ts": v["as_of"], "ticker": p["ticker"], "verdict": p["verdict"], "score": p["score"],
             "price": pos[p["ticker"]]["price"], "currency": pos[p["ticker"]]["currency"],
             "price_asof": dos["as_of"], "source": dos["source"], "confidence": p.get("confidence"),
             "thesis_status": p.get("thesis_status"), "would_buy_today": p.get("would_buy_today"),
             "replace_with": p.get("replace_with"), "replace_price": p.get("replace_price"), "levels": p.get("levels"),
             "action": p.get("action")} for p in v["positions"]]
    tn.journal_append("advice.jsonl", *recs)
    cnt = {k: sum(1 for p in v["positions"] if p["verdict"] == k) for k in VERDICTS}
    tn.journal_append("runs.jsonl", {"ts": v["as_of"], "skill": "fb-advisor", "source": dos["source"],
                                     "net_assets": dos["totals_usd"]["net_assets"], "positions": len(pos),
                                     "verdicts": cnt, "buy_ideas": [b["ticker"] for b in v.get("buy_ideas", [])],
                                     "report_md": path})
    print(json.dumps({"report_md": path, "advice_logged": len(recs), "verdicts": cnt, "warnings": warns},
                     ensure_ascii=False, indent=1))


# ---------- чек-лист перед покупкой ----------

def cmd_check(a):
    dos = load_dossier()
    tk = a.ticker.upper()
    if "." not in tk:
        tk += ".US"
    rules, s, total = dos["rules"], dos["summary"], dos["totals_usd"]["net_assets"]
    base_after = total + (0 if a.from_cash else a.amount)
    byt = {p["ticker"]: p for p in dos["positions"]}
    sold = {}  # продажи, которые сделаю до этой покупки: TICKER или TICKER:USD
    for x in a.sell or []:
        t, _, usd = x.upper().partition(":")
        t = t if "." in t else t + ".US"
        if t not in byt:
            sys.exit(f"--sell {t}: нет в досье")
        sold[t] = min(float(usd), byt[t]["value_usd"]) if usd else byt[t]["value_usd"]
    pos = byt.get(tk)
    cls = a.cls or (pos and pos["class"])
    if not cls:
        cfg = tn.read_ini()
        cls = cfg["classes"].get(tn.base_ticker(tk)) if cfg.has_section("classes") else None
    mentors = tn.load_mentors().get(tn.base_ticker(tk), [])
    advice = [r for r in tn.journal_read("advice.jsonl") if r["ticker"] == tk]
    last_adv = advice[-1] if advice else None
    book = book_for(trade_book(tn.journal_read("trades.jsonl")), tk)
    res = []

    def add(level, text):
        res.append({"level": level, "text": text})

    age = (dt.datetime.now() - dt.datetime.fromisoformat(dos["as_of"])).days
    if age > 3:
        add("warn", f"досье от {dos['as_of'][:10]} ({age} дн.) — обнови `advisor.py prep`")
    after = (pos["value_usd"] - sold.get(tk, 0) if pos else 0) + a.amount
    w = after / base_after * 100
    add("block" if w > rules["max_position_pct"] else "ok",
        f"доля после покупки {w:.2f}% (лимит {rules['max_position_pct']:g}%)")
    if sold:
        add("ok", "учтены продажи до покупки: " + ", ".join(f"{t} ${v:.0f}" for t, v in sold.items()))
    if cls in rules["targets"]:
        st = s["structure"].get(cls, {})
        gap = (st.get("gap_usd") or 0) + sum(v for t, v in sold.items() if byt[t]["class"] == cls)
        add("ok" if gap >= a.amount * 0.5 else "warn",
            f"класс {tn.CLASS_RU.get(cls, cls)}: до цели {rules['targets'][cls]:g}% не хватает ${gap:.0f}"
            if gap > 0 else f"класс {tn.CLASS_RU.get(cls, cls)} уже выше цели на ${-gap:.0f}")
    else:
        add("warn", "класс бумаги не определён — добавь в [classes] portfolio.ini")
    for g, tks in tn.load_groups().items():
        if tn.base_ticker(tk) in tks:
            out_g = sum(v for t, v in sold.items() if tn.base_ticker(t) in tks)
            gp = (s["themes"].get(g, {}).get("usd", 0) - out_g + a.amount) / base_after * 100
            add("block" if gp > rules["max_group_pct"] else "ok", f"тема «{g}» после покупки {gp:.1f}% (лимит {rules['max_group_pct']:g}%)")
    if pos and pos["pl_pct"] is not None and pos["pl_pct"] <= -rules["review_loss_pct"]:
        ok = last_adv and last_adv["verdict"] == "ДОКУПИТЬ"
        add("ok" if ok else "warn", f"усреднение убыточной позиции ({pos['pl_pct']:.0f}%): "
            + ("последний вердикт ДОКУПИТЬ" if ok else "нужен свежий разбор с вердиктом ДОКУПИТЬ — тезис цел?"))
    if last_adv and last_adv["verdict"] in ("ВЫЙТИ", "СОКРАТИТЬ", "ЗАМЕНИТЬ"):
        add("block", f"последний вердикт {last_adv['verdict']} от {last_adv['ts'][:10]} — покупка ему противоречит")
    if book and book.get("last_sell") and (dt.date.today() - dt.date.fromisoformat(book["last_sell"])).days <= 31:
        add("warn", f"продавал {book['last_sell']} по {book['last_sell_price']} — не откупаю ли эмоционально?")
    n_after = s["positions"] - sum(1 for t, v in sold.items() if v >= byt[t]["value_usd"] - 0.01)
    if not pos and n_after >= a.max_positions:
        add("warn", f"новая бумага при {n_after} позициях (ориентир ≤{a.max_positions}) — лучше докупить имеющуюся")
    if a.amount < 50:
        add("warn", f"сумма ${a.amount:g} < $50 — комиссия ~$1.2 съест >2%")
    if mentors:
        add("ok", "наставники: " + ", ".join(f"{m['portfolio']} {m['rec'] or '—'}" for m in mentors))
    else:
        add("warn", "бумаги нет у наставников — решение только на собственном разборе")
    if a.sl is None:
        add("warn", "SL не задан — заранее реши, при какой цене тезис сломан")
    elif a.price and a.sl >= a.price:
        add("block", "SL не ниже цены входа")
    if a.price and a.tp and a.sl:
        rr = (a.tp - a.price) / (a.price - a.sl) if a.price > a.sl else None
        if rr is not None:
            add("ok" if rr >= 2 else "warn", f"риск/прибыль 1:{rr:.1f} (ориентир ≥1:2)")
    lv = [x["level"] for x in res]
    status = "BLOCK" if "block" in lv else "WARN" if "warn" in lv else "PASS"
    print(json.dumps({"ticker": tk, "amount_usd": a.amount, "class": cls, "status": status, "checks": res},
                     ensure_ascii=False, indent=1))


# ---------- счёт советам ----------

def cmd_scorecard(a):
    adv = tn.journal_read("advice.jsonl")
    now = dt.datetime.now()
    adv = [r for r in adv if (now - dt.datetime.fromisoformat(r["ts"])).days >= a.min_days]
    if not adv:
        print(json.dumps({"advice": 0, "note": f"нет советов старше {a.min_days} дн."}, ensure_ascii=False))
        return
    prices, source = {}, "online"
    tickers = sorted({r["ticker"] for r in adv} | {r["replace_with"] for r in adv if r.get("replace_with")})
    if not a.offline:
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                pub, priv, domain, _, _ = tn.load_cfg(None)
                res = tn.call(pub, priv, domain, "getStockQuotesJson", {"tickers": ",".join(tickers)})
            for q in (res.get("result") or res).get("q", []) or []:
                if q.get("c") and num(q.get("ltp"), None):
                    prices[q["c"].upper()] = num(q["ltp"])
        except SystemExit:
            source = "offline"
    if not prices:
        source = "offline"
        runs = [r for r in tn.journal_read("runs.jsonl") if r.get("holdings")]
        prices = {k: v["px"] for k, v in (runs[-1]["holdings"] if runs else {}).items()}
    good_up = {"ДОКУПИТЬ": 1, "ДЕРЖАТЬ": 1, "СОКРАТИТЬ": -1, "ВЫЙТИ": -1, "ЗАМЕНИТЬ": -1}
    rows, stat = [], {}
    for r in adv:
        px = prices.get(r["ticker"].upper())
        if not px or not r.get("price"):
            continue
        chg = (px / r["price"] - 1) * 100
        rep = None
        if r.get("replace_with") and prices.get(r["replace_with"].upper()) and r.get("replace_price"):
            rep = (prices[r["replace_with"].upper()] / r["replace_price"] - 1) * 100
        edge = chg * good_up[r["verdict"]] if rep is None else rep - chg
        hit = None if abs(edge) < 2 else edge > 0
        rows.append({"ts": r["ts"][:10], "ticker": r["ticker"], "verdict": r["verdict"], "price_then": r["price"],
                     "price_now": px, "chg_pct": r2(chg), "hit": hit})
        st = stat.setdefault(r["verdict"], {"n": 0, "hits": 0, "misses": 0, "avg_chg_pct": 0.0})
        st["n"] += 1; st["hits"] += hit is True; st["misses"] += hit is False; st["avg_chg_pct"] += chg
    for st in stat.values():
        st["avg_chg_pct"] = r2(st["avg_chg_pct"] / st["n"])
        dec = st["hits"] + st["misses"]
        st["hit_rate_pct"] = r2(st["hits"] / dec * 100) if dec else None
    print(json.dumps({"source": source, "advice": len(rows), "by_verdict": stat, "rows": rows}, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prep"); s.add_argument("--offline", action="store_true")
    s = sub.add_parser("save"); s.add_argument("file")
    s = sub.add_parser("check"); s.add_argument("ticker"); s.add_argument("--amount", type=float, required=True)
    s.add_argument("--price", type=float); s.add_argument("--tp", type=float); s.add_argument("--sl", type=float)
    s.add_argument("--class", dest="cls", choices=("stock", "index", "hedge"))
    s.add_argument("--from-cash", action="store_true"); s.add_argument("--max-positions", type=int, default=25)
    s.add_argument("--sell", action="append", metavar="TICKER[:USD]",
                   help="продажа до этой покупки (повторяемый): целиком или на сумму USD; учитывается в лимитах")
    s = sub.add_parser("scorecard"); s.add_argument("--offline", action="store_true")
    s.add_argument("--min-days", type=int, default=7)
    a = ap.parse_args()
    {"prep": cmd_prep, "save": cmd_save, "check": cmd_check, "scorecard": cmd_scorecard}[a.cmd](a)


if __name__ == "__main__":
    main()
