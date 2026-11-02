---
name: fb-portfolio
description: Получает актуальные данные портфеля Freedom Broker (Tradernet API) — позиции, кэш, P&L, ордера, сделки, котировки — проверяет правила 5% на бумагу и структуру 40/30/30 и сохраняет отчёт в .md. Используй, когда пользователь спрашивает про свой портфель, позиции, баланс, сделки или ордера во Freedom/Tradernet.
---

# Портфель Freedom Broker через Tradernet API (локально, macOS)

Скилл работает ТОЛЬКО на локальной машине пользователя (Claude Code, macOS, zsh) и ТОЛЬКО ЧИТАЕТ данные. Никогда не выставляй, не меняй и не отменяй ордера и не выводи деньги, даже если пользователь просит, — скрипт блокирует всё, что не входит в allowlist команд чтения.

`~/.claude/skills/fb-portfolio` — симлинк на `skills/fb-portfolio/` в папке данных `FB_HOME` (по умолчанию `~/projects/PET PROJECTS/freedom-broker`, определяется по реальному пути скрипта; переопределяется переменной `FB_HOME`):

```
FB_HOME/
├── skills/fb-portfolio/tn.py   # клиент API (только stdlib). Не переписывай без нужды — он отлажен
├── config/portfolio.ini        # правила и классы тикеров
├── state/mentors/              # снимки наставников (fb-invest)
├── reports/YYYY-MM/            # md-отчёты
└── journal/                    # журнал операций, см. п.5
```

## 1. Ключи и запуск

Ключи лежат в `~/.zprofile` (или `~/.zshrc`) как `TRADERNET_PUBLIC_KEY`, `TRADERNET_PRIVATE_KEY`, опционально `TRADERNET_DOMAIN` (по умолчанию `tradernet.global`; альтернативы `freedom24.com`, `tradernet.kz`).

КАЖДЫЙ запуск — через login + interactive zsh (`-lic` подгружает и `~/.zprofile`, и `~/.zshrc`). `env -u TERM_SESSION_ID` убирает строку «Restored session…» от Apple Terminal, `2>/dev/null` — шум плагинов, так что stdout — чистый JSON:

```bash
env -u TERM_SESSION_ID zsh -lic 'python3 ~/.claude/skills/fb-portfolio/tn.py portfolio' 2>/dev/null
```

Если вывод пустой — повтори без `2>/dev/null`, чтобы увидеть ошибку.

Проверка наличия ключей — только факт, без значений:

```bash
env -u TERM_SESSION_ID zsh -lic '[ -n "$TRADERNET_PUBLIC_KEY" ] && [ -n "$TRADERNET_PRIVATE_KEY" ] && echo keys-ok || echo keys-missing' 2>/dev/null
```

Если `keys-missing` — попроси пользователя добавить `export TRADERNET_PUBLIC_KEY="..."` / `export TRADERNET_PRIVATE_KEY="..."` в `~/.zprofile` или `~/.zshrc` (ключи создаются в кабинете Tradernet: Профиль/Настройки → «API-ключи», по возможности только чтение). Сами эти файлы не редактируй.

Безопасность: никогда не выводи значения ключей (никаких `echo $TRADERNET_PRIVATE_KEY`, `env`, `printenv`, `cat`/`grep` по `~/.zshrc`/`~/.zprofile`). Не записывай ключи в файлы, коммиты, логи или ответы. Если пользователь вставил ключ в чат — посоветуй перевыпустить его.

Авторизация (для справки) — API v3: POST `https://<domain>/api/<cmd>`, тело = компактный JSON, заголовки `X-NtApi-PublicKey`, `X-NtApi-Timestamp` (unix-секунды), `X-NtApi-Sig = hex(HMAC_SHA256(private_key, body + timestamp))`, плюс свой `User-Agent` (без него Cloudflare отвечает 403 `error code: 1010`).

## 2. Команды

Все — в обёртке из п.1 (`… tn.py <команда>`):

- `portfolio` — сводка в JSON + **сохраняет отчёт в Markdown** (путь — в поле `report_md`). Папка по умолчанию `FB_HOME/reports/YYYY-MM/`, файл `portfolio_YYYY-MM-DD_HH-MM.md`; переопределяется `--md-dir <папка>` или `TRADERNET_REPORT_DIR`; `--no-md` — не сохранять (и не писать в журнал). Каждый сохранённый запуск дописывает строку в `journal/runs.jsonl` и сам делает `journal-sync` (новые сделки → `trades.jsonl`), чтобы учесть мои продажи. Дополнительно читает активные стоп-ордера (`getNotifyOrderJson`) и котировки облигаций (`getStockQuotesJson`). `portfolio --raw` — исходный ответ `getPositionJson` (md не пишется).
- `orders` (`--all` — включая исполненные/отменённые за сегодня) — `getNotifyOrderJson`.
- `orders-history --from YYYY-MM-DD --to YYYY-MM-DD` — `getOrdersHistory`.
- `trades --from … --to … [--ticker HSBK.KZ]` — сделки, `getTradesHistory` (по умолчанию 30 дней).
- `quotes AAPL.US GLD.US` — котировки, `getStockQuotesJson`.
- `report --from … --to … --part account_at_end|trades|commissions|cash_flows|in_outs|corporate_actions` — брокерский отчёт.
- `journal-sync [--from YYYY-MM-DD]` — дописывает в `journal/trades.jsonl` новые сделки брокера (без дублей по id; по умолчанию — с последней записанной даты минус 7 дней). Возвращает `added` и список `new`.
- `note "текст" [--tag FXI --tag tp-sl]` — запись в журнал решений `journal/YYYY-MM.md` (ключи не нужны).
- `raw <cmd> '<json>'` — другая команда из allowlist. Внутри `zsh -lic '…'` одинарные кавычки не вложить — используй `"` снаружи: `… zsh -lic "python3 … tn.py raw getMarketStatus '{\"market\":\"*\"}'"`.

Общие флаги (ПЕРЕД командой): `--domain freedom24.com`, `--config <путь к portfolio.ini>`.

Поля JSON `portfolio`: `totals_usd` (`net_assets`, `positions`, `cash`, `unrealized_pl`, `cost_basis`, `unrealized_pl_pct`), `cash[]`, `positions[]` (`ticker`, `qty`, `price`, `avg_price`, `value_usd`, `weight_pct`, `unrealized_pl` в валюте бумаги, `unrealized_pl_usd`, `unrealized_pl_pct`, `class`; у облигаций ещё `face`, `maturity`, `accrued_usd`), `structure`, `rules` (`base` = `net_assets`, `over_limit`, `near_limit`, `unclassified`, `small_position_pct`, `small`), `concentration` (`max_group_pct`, `groups[]`: `name`, `tickers`, `usd`, `pct`, `status` over|near|ok, `over_usd`), `currency_exposure[]` (позиции + кэш по валютам), `sales` (`realized_usd`: `month` / `ytd` / `all` — реализованный P&L по моим продажам; `recent` — продажи за 31 дн., части одного ордера склеены: `ticker`, `qty`, `price`, `sum`, `realized_pl_usd`, `still_held`), `pnl` (`total_usd` = реализованный за всё время + нереализованный, `commissions_usd`: `month`/`ytd`/`all`), `bonds[]` (`maturity`, `days_to_maturity`, `matured`, `coupon_pct`, `ytm_pct`, `next_coupon`, `accrued_usd`), `review` (`loss_pct`, `positions[]`: убыток ≥ `review_loss_pct`, `my_tp`/`my_sl` из активных стоп-ордеров, `mentors[]` из `state/mentors/` — `portfolio`, `rec`, `tp`, `sl`, `below_mentor_sl`), `vs_prev` (сравнение с последним запуском `fb-portfolio` из `runs.jsonl` за более ранний день: `net_assets`/`unrealized_pl`/`cash`/`realized_all` → `from`/`to`/`delta`, `structure_pp`; если в прошлом снимке есть `holdings` — `opened`, `closed`, `qty_changed`, `revaluation_usd` (переоценка общих позиций) и `other_usd` (остальное: пополнения/выводы, сделки, купоны, комиссии, курс); `comparable: false` — прошлый снимок считался старым методом), `data_checks` (`valuation`, `broker_value_gaps[]`, `broker_value_gap_usd`), `report_md`.

Оценка позиций (проверено 2026-10): `market_value` брокера у акций и ETF часто устаревает (у DBB держался неделями, расхождение до 12%), поэтому стоимость, P&L и P&L % считаются как кол-во × `mkt_price` от средней цены. У облигаций (`t = 2`) цена — % от номинала, стоимость берётся из `market_value` (= кол-во × номинал × цена / 100), P&L — `profit_close` брокера. Расхождения больше 1% попадают в `data_checks`. Купон облигации: `cpn` на KASE — годовая ставка %, на AIX — выплата за период `cpp` дней; `coupon_rate()` выбирает вариант, при котором совпадает НКД (`acd`). При `matured: true` бумага висит после даты погашения — нужно проверить, пришла ли выплата номинала.

Продажи: конвертации валют (`KZT/USD`, `EUR/USD`) не считаются; `realized_pl` — поле `profit` брокера в валюте сделки, KZT пересчитан в USD по сегодняшнему курсу (приблизительно), комиссии отдельно.

Разметка полей `getPositionJson` (проверено): `profit_close` — нереализованный P&L брокера, посчитанный от `profit_price` (это цена, а не P&L); `market_value` у акций и ETF бывает устаревшим, см. выше; дневного P&L в ответе нет — не выдумывай его (ближайшая замена — `vs_prev`).

Ошибки:
- HTTP 403 `error code: 1010` — Cloudflare режет запрос; проверь, что в `tn.py` отправляется `User-Agent`.
- `API error` / HTTP 401/403 с авторизацией — проверь ключи и попробуй `--domain freedom24.com`, затем `tradernet.kz`; если заработал другой — предложи добавить `export TRADERNET_DOMAIN=...`.
- `CERTIFICATE_VERIFY_FAILED` — предложи `/Applications/Python\ 3.*/Install\ Certificates.command` или `pip3 install certifi` (скрипт подхватит `certifi`).
- Поля пустые или структура другая — запусти `portfolio --raw`, разберись по фактическим полям и сообщи пользователю.

## 3. Правила портфеля — `portfolio.ini`

`FB_HOME/config/portfolio.ini` подхватывается автоматически:

```ini
[rules]
max_position_pct = 5
target_stock = 40
target_index = 30
target_hedge = 30
max_group_pct = 10        ; лимит на тему из [groups]
small_position_pct = 1    ; ниже — блок «мелкие позиции»
review_loss_pct = 30      ; убыток к средней ≥ — блок «на пересмотр»

[groups]
; тема = тикеры без суффикса; бумага может быть в нескольких темах
золото = GLD, IAU

[classes]
; тикер (с суффиксом биржи или без) = stock | index | hedge
GLD = hedge
FXI = index
HSBK = stock
```

Без файла — значения по умолчанию (5%, 40/30/30, темы не проверяются), все тикеры в `unclassified`. Классы из файла приоритетнее твоих догадок. Все доли и лимиты — от чистых активов (позиции + кэш).

## 4. Как подавать результат

Отвечай по-русски, кратко, опираясь на JSON:
1. Чистые активы в USD, позиции, кэш (USD и KZT отдельно), нереализованный P&L в $ и % к вложенному (`cost_basis`), реализованный P&L по продажам (месяц / год), итого P&L (`pnl.total_usd`), комиссии (месяц / год), дата/время. Пометь, что реализованный P&L по сделкам в KZT приблизителен (пересчёт по сегодняшнему курсу).
2. С прошлого снимка (`vs_prev`), если есть: изменение чистых активов, переоценка против прочего, новые/закрытые позиции, сдвиг структуры. Если `comparable: false` — предупреди про смену метода оценки.
3. Топ-10 позиций по доле: тикер, кол-во, цена, стоимость $, доля %, P&L %. Остальные — одной строкой «ещё N позиций, $X». Полная таблица есть в md-отчёте (под спойлером, по классам).
4. Структура: акции / индексные фонды / хедж — факт против целей 40/30/30 и отклонение в $.
5. Правило 5% от чистых активов: позиции выше лимита (с превышением в $) и близкие к нему (>4.25%).
6. Концентрация по темам (`concentration`) — что выше/близко к лимиту; валютная доля (`currency_exposure`), особенно KZT.
7. Мелкие позиции (`rules.small`) — одной строкой.
8. Облигации: погашение, купон, НКД; `matured: true` выделяй как требующее проверки.
9. На пересмотр (`review`): позиции с убытком ≥ порога, есть ли мои TP/SL и что у наставников (рекомендация, TP/SL); `below_mentor_sl` выделяй.
10. Если `data_checks.broker_value_gaps` не пуст — одной фразой: оценка по кол-ву × цене, брокерская `market_value` устарела у N бумаг.
11. Если `rules.unclassified` не пуст — классифицируй сам (ETF на индекс/сектор → index; золото, металлы, облигации, bond ETF → hedge; отдельные компании и single-stock ETF → stock), покажи решение и предложи добавить строки в `[classes]`; после согласия допиши их в `portfolio.ini` и перезапусти `portfolio`, чтобы md-отчёт был с классами.
12. Если `sales.recent` не пуст — коротко: что продано за месяц, с каким P&L, и что кэш от продаж ещё не вложен (если кэш заметный).
13. В конце — ссылка на сохранённый md-файл (`report_md`).

Если пользователь просит разбор или рекомендации — опирайся на эти данные, но не давай уверенных инвестиционных рекомендаций и напомни, что это не финансовый совет.

## 5. Журнал операций — `FB_HOME/journal/`

Только добавление, ничего не переписывай и не удаляй:

| Файл | Кто пишет | Что |
|---|---|---|
| `runs.jsonl` | `tn.py portfolio`, `mentors.py` | каждый запуск: активы, структура, предложенная корзина, путь к отчёту |
| `mentor_changes.jsonl` | `mentors.py` | изменения у наставников: TP/SL, рекомендации, открытия/закрытия, их сделки |
| `trades.jsonl` | `tn.py journal-sync`, а также `portfolio` и `mentors.py` автоматически | мои реальные сделки из брокера |
| `YYYY-MM.md` | `tn.py note` | решения и причины своими словами |

- Когда пользователь говорит, что купил/продал или поправил TP/SL, — запусти `journal-sync`, покажи добавленные сделки и запиши `note`: что сделал, отличие от последней корзины в `runs.jsonl` и причину (спроси коротко, если не назвал). Теги — тикеры.
- На вопросы «что я покупал», «как менялись TP/SL у X», «план против факта» — отвечай по файлам журнала (`trades.jsonl` против `basket` в `runs.jsonl`).
- В журнал нельзя писать ключи, ссылку на таблицу наставников и логин брокера.
