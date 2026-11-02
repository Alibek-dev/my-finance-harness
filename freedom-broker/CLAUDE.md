# freedom-broker

Данные и исходники скиллов `fb-portfolio` / `fb-invest` / `fb-advisor` (Freedom Broker, только чтение).
`~/.claude/skills/fb-*` — симлинки на `skills/fb-*`; скрипты находят эту папку (`FB_HOME`) по своему реальному пути.

- `skills/` — код и SKILL.md
  - `fb-advisor` — разбор каждой позиции с вердиктом, чек-лист перед покупкой, счёт прошлым советам
  - `vendor/tradermonty/` — сторонние скиллы (MIT): `us-stock-analysis`, `market-environment-analysis`, `scenario-analyzer`,
    `position-sizer`, `technical-analyst`; источник и правки — `VENDOR.md`. Симлинки в `~/.claude/skills/` под своими именами
- `config/portfolio.ini` — правила 5% / 40-30-30 и классы тикеров
- `state/mentors/` — последний снимок портфелей наставников (перезаписывается)
- `state/advisor/dossier.json` — последнее досье `fb-advisor` (перезаписывается)
- `reports/YYYY-MM/` — md-отчёты
- `journal/` — журнал, только дописывать: `runs.jsonl`, `mentor_changes.jsonl`, `trades.jsonl` (`tn.py journal-sync`), `advice.jsonl` (вердикты `fb-advisor`), `YYYY-MM.md` (`tn.py note`)

Не коммитить и не писать в файлы API-ключи, ссылку на таблицу наставников, логин брокера. Репозиторий — только локальный или приватный.
