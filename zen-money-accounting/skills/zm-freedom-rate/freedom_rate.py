#!/usr/bin/env python3
"""Курс Freedom Bank (безналичный) на дату — для пересчёта валютных операций карты в тенге.

  freedom_rate.py snapshot                      # сохранить текущие курсы в rates/freedom.jsonl
  freedom_rate.py get [YYYY-MM-DD] [--cur USD] [--side sell|buy] [--amount 10]

Сторона курса:
  sell — банк продаёт валюту: покупка/списание в USD с тенгового счёта (самый частый случай);
  buy  — банк покупает валюту: поступление USD, которое зачисляется в тенге.

Источники по приоритету:
  1. live      — API банка (только для сегодняшней даты; истории банк не отдаёт);
  2. snapshot  — наши снимки за эту дату (cron), берётся последний снимок дня + min/max за день;
  3. nbk       — официальный курс Нацбанка РК на дату. Это ОЦЕНКА: курс банка отличается на спред.
Только stdlib. Ничего, кроме rates/freedom.jsonl, не пишет.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

API = "https://bankffin.kz/api/exchange-rates/getRates"
NBK = "https://nationalbank.kz/rss/get_rates.cfm?fdate={d}"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # zen-money-accounting/
STORE = os.path.join(ROOT, "rates", "freedom.jsonl")


def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def num(s):
    return float(str(s).replace(" ", "").replace(",", "."))


def live():
    """{'USD': {'buy': 449.5, 'sell': 456.5}, ...} — безналичные курсы к KZT."""
    data = json.loads(http(API))["data"]
    out = {}
    for row in data.get("non_cash") or data.get("mobile") or []:
        if row["sellCode"] == "KZT":
            out[row["buyCode"]] = {"buy": num(row["buyRate"]), "sell": num(row["sellRate"])}
    if not out:
        raise SystemExit("API банка вернул пустые безналичные курсы")
    return out


def snapshot():
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "source": "bankffin.kz non_cash",
           "rates": live()}
    with open(STORE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def snapshots_for(day):
    if not os.path.exists(STORE):
        return []
    out = []
    for line in open(STORE):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec["ts"][:10] == day:
            out.append(rec)
    return out


def nbk(day, cur):
    d = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    xml = http(NBK.format(d=d))
    m = re.search(rf"<title>{cur}</title>\s*<description>([\d.]+)</description>\s*<quant>(\d+)</quant>", xml)
    if not m:
        m2 = re.search(rf"<title>{cur}</title>\s*<description>([\d.]+)</description>", xml)
        return float(m2.group(1)) if m2 else None
    return float(m.group(1)) / int(m.group(2))


def get(day, cur, side, amount):
    res = {"date": day, "currency": cur, "side": side}
    if day == date.today().isoformat():
        try:
            r = live()[cur]
            res.update(source="live", rate=r[side], buy=r["buy"], sell=r["sell"])
        except Exception as e:  # сеть/формат — падаем на снимки и НБ РК
            res["live_error"] = str(e)
    if "rate" not in res:
        snaps = [s for s in snapshots_for(day) if cur in s["rates"]]
        if snaps:
            vals = [s["rates"][cur][side] for s in snaps]
            res.update(source="snapshot", rate=snaps[-1]["rates"][cur][side], snapshot_ts=snaps[-1]["ts"],
                       day_min=min(vals), day_max=max(vals), snapshots=len(snaps))
    if "rate" not in res:
        r = nbk(day, cur)
        if r is None:
            raise SystemExit(f"Нет курса {cur} на {day} ни в снимках, ни у НБ РК")
        res.update(source="nbk", rate=r, note="официальный курс НБ РК — оценка, курс Freedom отличается на спред; "
                                              "сверь остаток счёта после записи")
    if amount is not None:
        res["amount"] = amount
        res["kzt"] = round(amount * res["rate"], 2)
    return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    g = sub.add_parser("get")
    g.add_argument("day", nargs="?", default=date.today().isoformat())
    g.add_argument("--cur", default="USD")
    g.add_argument("--side", choices=["sell", "buy"], default="sell")
    g.add_argument("--amount", type=float)
    a = ap.parse_args()
    out = snapshot() if a.cmd == "snapshot" else get(a.day, a.cur.upper(), a.side, a.amount)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
