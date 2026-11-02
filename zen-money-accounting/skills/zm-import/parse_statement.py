#!/usr/bin/env python3
"""Разбор PDF-выписки банка в нормализованный JSON (stdout). Ничего не пишет на диск.

Поддержка: Kaspi Gold (Kaspi Bank), Super Card (Freedom Bank / Фридом Банк Казахстан).
Использование: parse_statement.py <file.pdf> [--summary]
Требует pdfplumber (venv проекта: zen-money-accounting/.venv).

Выход: {bank, account, currency, period:{from,to}, ops:[{date, amount, currency, type, details, status}],
        checks:{ok, by_type:{тип: {parsed, statement}}}}
Персональные данные вычищаются: IBAN → *1234, ИИН и «Вкладчик: …» удаляются.
"""
import json
import re
import sys
from collections import defaultdict

import pdfplumber

IBAN = re.compile(r"\bKZ[0-9A-Z]{18}\b")
IIN = re.compile(r"\b\d{12}\b")


def clean(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    s = IBAN.sub(lambda m: "*" + m.group(0)[-4:] if not m.group(0)[-3:].isalpha() else "*" + m.group(0)[-7:-3], s)
    s = IIN.sub("<ИИН>", s)
    s = re.sub(r"Вкладчик:.*$", "", s).strip(" .")
    return s


def num(s):
    """'- 1 200,00 ₸' / '-1,200.00 ₸' / '+3.37 $' -> float"""
    s = s.replace("\xa0", " ")
    sign = -1 if s.strip().startswith("-") else 1
    digits = re.sub(r"[^\d.,]", "", s)
    if "," in digits and "." in digits:      # 1,200.00 (Freedom)
        digits = digits.replace(",", "")
    else:                                      # 1 200,00 (Kaspi)
        digits = digits.replace(",", ".")
    return round(sign * float(digits), 2)


def iso(d):
    dd, mm, yy = d.split(".")
    return f"{'20' + yy if len(yy) == 2 else yy}-{mm}-{dd}"


# ---------------------------------------------------------------- Kaspi Gold
# префикс в строке -> полный тип (хвост «счет» / «своего счета» переносится на следующую строку)
KASPI_OPS = [("Перевод на свой счет", "Перевод на свой счет"), ("Перевод на свой", "Перевод на свой счет"),
             ("Поступление со своего счета", "Поступление со своего счета"),
             ("Поступление со", "Поступление со своего счета"),
             ("Покупка", "Покупка"), ("Перевод", "Перевод"), ("Пополнение", "Пополнение"),
             ("Снятие", "Снятие"), ("Разное", "Разное")]
KASPI_SUMMARY = {  # строка краткого содержания -> типы операций
    "Пополнения": ["Пополнение"], "Поступления со своих счетов": ["Поступление со своего счета"],
    "Переводы": ["Перевод"], "Переводы на свои счета": ["Перевод на свой счет"],
    "Покупки": ["Покупка"], "Снятия": ["Снятие"], "Разное": ["Разное"],
}
KASPI_LINE = re.compile(r"^(\d{2}\.\d{2}\.\d{2}) ([+-]) ([\d ]+,\d{2}) ₸ (.*)$")


def parse_kaspi(pdf):
    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    acc = re.search(r"Номер карты:\s*(\*\d{4})", text)
    per = re.search(r"за период с (\d{2}\.\d{2}\.\d{2}) по (\d{2}\.\d{2}\.\d{2})", text)
    ops, last = [], None
    for line in text.splitlines():
        m = KASPI_LINE.match(line)
        if m:
            date, sign, amt, rest = m.groups()
            hit = next(((pre, typ) for pre, typ in KASPI_OPS if rest.startswith(pre + " ") or rest == pre), None)
            if hit is None:
                raise ValueError(f"Неизвестный тип операции Kaspi: {line!r}")
            last = {"date": iso(date), "amount": num(sign + amt), "currency": "KZT", "type": hit[1],
                    "details": clean(rest[len(hit[0]):]), "status": "done"}
            ops.append(last)
        # хвосты переносов («счет», «своего счета») не несут информации — пропускаем
    summary = {}
    for label in KASPI_SUMMARY:
        m = re.search(rf"^{re.escape(label)} ([+-]) ([\d ]+,\d{{2}}) ₸", text, re.M)
        if m:
            summary[label] = num(m.group(1) + m.group(2))
    by = defaultdict(float)
    for o in ops:
        by[o["type"]] += o["amount"]
    checks = {}
    for label, types in KASPI_SUMMARY.items():
        parsed = round(sum(by[t] for t in types), 2)
        stated = summary.get(label)
        checks[label] = {"parsed": parsed, "statement": stated}
    bal = re.search(r"Доступно на \d{2}\.\d{2}\.\d{2}:\s*([+-]) ([\d ]+,\d{2}) ₸", text)
    return {"bank": "kaspi", "product": "Kaspi Gold", "closing_balance": num(bal.group(1) + bal.group(2)) if bal else None, "account": acc.group(1) if acc else None, "currency": "KZT",
            "period": {"from": iso(per.group(1)), "to": iso(per.group(2))} if per else None,
            "ops": ops, "checks": checks}


# ---------------------------------------------------------------- Freedom Super Card
FREEDOM_SUMMARY = ["Платеж", "Перевод", "Покупка", "Снятие", "Платеж по кредиту", "Пополнение",
                   "Сумма в обработке", "Погашение", "Овердрафт", "Другое"]
CUR = {"₸": "KZT", "$": "USD", "€": "EUR", "₽": "RUB", "¥": "CNY", "₺": "TRY", "AED": "AED"}


def parse_freedom(pdf):
    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    acc = re.search(r"Номер карты:\s*\*+(\d{4})", text)
    per = re.search(r"за период с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})", text)
    ops = []
    carry_op, carry_det = "", ""  # строка рвётся на границе страниц: начало без даты внизу страницы,
    for page in pdf.pages:        # дата/сумма и хвост — первой строкой следующей
        for table in page.extract_tables():
            for row in table:
                if not row or len(row) < 5:
                    continue
                if not row[0] or not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", row[0].strip()):
                    if not (row[0] or "").strip() and not (row[1] or "").strip() and (row[3] or row[4]):
                        carry_op, carry_det = (row[3] or "").strip(), (row[4] or "").strip()
                    continue
                date, amount, cur, op, details = (c or "" for c in row[:5])
                op = re.sub(r"\s+", " ", f"{carry_op} {op}").strip()
                details = f"{carry_det} {details}"
                carry_op, carry_det = "", ""
                pending = op == "Сумма в обработке"
                if not op:
                    raise ValueError(f"Freedom: пустой тип операции {date} {amount}")
                ops.append({"date": iso(date.strip()), "amount": num(amount), "currency": cur.strip(),
                            "type": op, "details": clean(details), "status": "pending" if pending else "done"})
    summary = {}
    for label in FREEDOM_SUMMARY:
        m = re.search(rf"^{re.escape(label)} ([+-]?[\d,]+\.\d{{2}})₸", text, re.M)
        if m:
            summary[label] = num(m.group(1))
    # Сводка банка — в тенге, валютные операции пересчитаны по курсу банка на момент операции.
    # Точная сверка возможна только по KZT; для типов с валютой показываем подразумеваемый курс.
    kzt, fx = defaultdict(float), defaultdict(lambda: defaultdict(float))
    for o in ops:
        if o["currency"] == "KZT":
            kzt[o["type"]] += o["amount"]
        else:
            fx[o["type"]][o["currency"]] += o["amount"]
    checks = {}
    for label in FREEDOM_SUMMARY:
        if label not in summary:
            continue
        parsed = round(kzt.get(label, 0.0), 2)
        stated = summary[label]
        if label == "Сумма в обработке":  # в сводке — модуль суммы
            parsed = abs(parsed)
        c = {"parsed": parsed, "statement": stated}
        if fx.get(label):
            c["fx"] = {cur: round(v, 2) for cur, v in fx[label].items()}
            rest = abs(stated - parsed) if label == "Сумма в обработке" else stated - parsed
            if len(c["fx"]) == 1:
                (cur, v), = c["fx"].items()
                c["implied_rate"] = round(abs(rest / v), 2) if v else None
            c["ok"] = None  # нельзя проверить точно — смотри implied_rate
        checks[label] = c
    bal = re.search(r"\bKZT ([\d,]+\.\d{2}) ₸", text)
    return {"bank": "freedom", "product": "Super Card", "closing_balance": num(bal.group(1)) if bal else None, "account": "*" + acc.group(1) if acc else None,
            "currency": "KZT", "period": {"from": iso(per.group(1)), "to": iso(per.group(2))} if per else None,
            "ops": ops, "checks": checks}


def detect(pdf):
    head = pdf.pages[0].extract_text() or ""
    if "Kaspi Bank" in head:
        return parse_kaspi
    if "Фридом Банк" in head or "bankffin" in head:
        return parse_freedom
    raise SystemExit("Банк не распознан — нужен новый парсер")


def main():
    path = sys.argv[1]
    with pdfplumber.open(path) as pdf:
        res = detect(pdf)(pdf)
    for c in res["checks"].values():
        if "ok" not in c:
            c["ok"] = c["statement"] is None or abs(c["parsed"] - c["statement"]) < 0.01
    res["checks_ok"] = all(c["ok"] is not False for c in res["checks"].values())
    res["count"] = len(res["ops"])
    if "--summary" in sys.argv:
        res = {k: v for k, v in res.items() if k != "ops"}
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
