#!/usr/bin/env python3
"""Сопоставление операций выписки с операциями Дзен-мани по одному счёту. Ничего не пишет на диск.

Использование:
  match.py <statement.json> <zen_tx.json> <zen_account_id> [--days 3] [--pending-days 5]
    statement.json — вывод parse_statement.py
    zen_tx.json    — список операций из list_transactions (массив или {"data": [...]})

Сигнатура суммы операции Дзен для счёта: +income.amount, если income.accountId == счёт; −expense.amount, если expense.
Валютные операции выписки сверяются с originalAmount/originalInstrument операции Дзен.
Пара = тот же знак и сумма (±0.01), дата в пределах окна; жадно по минимальной разнице дат, один к одному.

Выход (stdout JSON): matched [{op, zen_id, date_diff}], missing [op] — нет в Дзен,
extra [zen_tx] — есть в Дзен в периоде выписки, но нет в выписке.
"""
import argparse
import json
from datetime import date


def d(s):
    return date.fromisoformat(s[:10])


def zen_amounts(tx, acc):
    """[(signed_amount, currency_or_None)] для счёта acc; currency=None — валюта счёта."""
    out = []
    for side, sign in (("income", 1), ("expense", -1)):
        part = tx.get(side)
        if part and part.get("accountId") == acc:
            out.append((sign * part["amount"], None))
            if part.get("originalInstrument"):
                out.append((sign * part["originalAmount"], part["originalInstrument"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("statement")
    ap.add_argument("zen")
    ap.add_argument("account")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--pending-days", type=int, default=5)
    ap.add_argument("--fx-min", type=float, default=300, help="нижняя граница правдоподобного курса")
    ap.add_argument("--fx-max", type=float, default=700, help="верхняя граница правдоподобного курса")
    ap.add_argument("--fx-tol", type=float, default=0.03, help="допуск отклонения от медианного курса")
    a = ap.parse_args()

    st = json.load(open(a.statement))
    zen = json.load(open(a.zen))
    zen = zen.get("data", zen) if isinstance(zen, dict) else zen
    zen = [t for t in zen if not t.get("deleted") and zen_amounts(t, a.account)]
    base_cur = st.get("currency", "KZT")

    cands = []
    for i, op in enumerate(st["ops"]):
        win = a.pending_days if op.get("status") == "pending" else a.days
        for j, t in enumerate(zen):
            dd = abs((d(op["date"]) - d(t["date"])).days)
            if dd > win:
                continue
            for amt, cur in zen_amounts(t, a.account):
                same_cur = (cur is None and op["currency"] == base_cur) or cur == op["currency"]
                if same_cur and abs(amt - op["amount"]) < 0.01:
                    cands.append((dd, i, j))
                    break
    cands.sort()
    used_op, used_zen, matched = set(), set(), []
    for dd, i, j in cands:
        if i in used_op or j in used_zen:
            continue
        used_op.add(i)
        used_zen.add(j)
        matched.append({"op": st["ops"][i], "zen_id": zen[j]["id"], "date_diff": dd})

    # --- Проход 2: валютные операции, которые синк банка записал в валюте счёта без originalAmount.
    # Курс берём медианой по всем правдоподобным парам, допуск ±fx_tol. Сначала 1:1, потом «две операции
    # выписки в один день = одна запись Дзен» (так бывает, когда операции объединены вручную).
    def zen_plain(t):
        return [amt for amt, cur in zen_amounts(t, a.account) if cur is None]

    fx_ops = [i for i, op in enumerate(st["ops"]) if i not in used_op and op["currency"] != base_cur]
    free = [j for j in range(len(zen)) if j not in used_zen]

    def rate_cands(groups):
        out = []
        for grp in groups:
            amount = sum(st["ops"][i]["amount"] for i in grp)
            od = d(st["ops"][grp[0]]["date"])
            for j in free:
                if j in used_zen:
                    continue
                dd = abs((od - d(zen[j]["date"])).days)
                if dd > a.days:
                    continue
                for amt in zen_plain(zen[j]):
                    if amt * amount > 0:
                        out.append((amt / amount, dd, grp, j))
        return out

    singles = rate_cands([(i,) for i in fx_ops])
    plaus = sorted(r for r, *_ in singles if a.fx_min <= r <= a.fx_max)
    fx_rate = plaus[len(plaus) // 2] if plaus else None
    if fx_rate:
        def take(cands, how):
            cands.sort(key=lambda c: (abs(c[0] / fx_rate - 1), c[1]))
            for r, dd, grp, j in cands:
                if abs(r / fx_rate - 1) > a.fx_tol or j in used_zen or any(i in used_op for i in grp):
                    continue
                used_zen.add(j)
                used_op.update(grp)
                for i in grp:
                    matched.append({"op": st["ops"][i], "zen_id": zen[j]["id"], "date_diff": dd,
                                    "via": how, "rate": round(r, 2)})
        take(singles, "fx")
        left = [i for i in fx_ops if i not in used_op]
        pairs = [(x, y) for n, x in enumerate(left) for y in left[n + 1:]
                 if st["ops"][x]["date"] == st["ops"][y]["date"]
                 and st["ops"][x]["currency"] == st["ops"][y]["currency"]]
        take(rate_cands(pairs), "fx-sum")

    p_from, p_to = st["period"]["from"], st["period"]["to"]
    res = {
        "matched": matched,
        "missing": [op for i, op in enumerate(st["ops"]) if i not in used_op],
        "extra": [t for j, t in enumerate(zen) if j not in used_zen and p_from <= t["date"][:10] <= p_to],
    }
    res["stats"] = {k: len(v) for k, v in res.items()}
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
