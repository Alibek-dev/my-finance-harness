#!/usr/bin/env python3
"""Курс Freedom Bank (безналичный) на дату — для пересчёта валютных операций карты в тенге.

  freedom_rate.py get [YYYY-MM-DD] [--cur USD] [--side sell|buy] [--amount 10]

Сторона курса:
  sell — банк продаёт валюту: покупка/списание в USD с тенгового счёта (самый частый случай);
  buy  — банк покупает валюту: поступление USD, которое зачисляется в тенге.

Источники по приоритету:
  1. live — API банка (только для сегодняшней даты; истории банк не отдаёт);
  2. nbk  — официальный курс Нацбанка РК на дату. Это ОЦЕНКА: курс банка отличается на спред.
Только stdlib. Ничего не пишет на диск.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime

API = "https://bankffin.kz/api/exchange-rates/getRates"
NBK = "https://nationalbank.kz/rss/get_rates.cfm?fdate={d}"


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
        except Exception as e:  # сеть/формат — падаем на НБ РК
            res["live_error"] = str(e)
    if "rate" not in res:
        r = nbk(day, cur)
        if r is None:
            raise SystemExit(f"Нет курса {cur} на {day} у НБ РК")
        res.update(source="nbk", rate=r, note="официальный курс НБ РК — оценка, курс Freedom отличается на спред; "
                                              "сверь остаток счёта после записи")
    if amount is not None:
        res["amount"] = amount
        res["kzt"] = round(amount * res["rate"], 2)
    return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("day", nargs="?", default=date.today().isoformat())
    g.add_argument("--cur", default="USD")
    g.add_argument("--side", choices=["sell", "buy"], default="sell")
    g.add_argument("--amount", type=float)
    a = ap.parse_args()
    out = get(a.day, a.cur.upper(), a.side, a.amount)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
