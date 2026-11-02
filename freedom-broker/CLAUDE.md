# freedom-broker

Данные и исходники скиллов `fb-portfolio` / `fb-invest` (Freedom Broker, только чтение).
`~/.claude/skills/fb-*` — симлинки на `skills/fb-*`; скрипты находят эту папку (`FB_HOME`) по своему реальному пути.

- `skills/` — код и SKILL.md
- `config/portfolio.ini` — правила 5% / 40-30-30 и классы тикеров
- `state/mentors/` — последний снимок портфелей наставников (перезаписывается)
- `reports/YYYY-MM/` — md-отчёты
- `journal/` — журнал, только дописывать: `runs.jsonl`, `mentor_changes.jsonl`, `trades.jsonl` (`tn.py journal-sync`), `YYYY-MM.md` (`tn.py note`)

Не коммитить и не писать в файлы API-ключи, ссылку на таблицу наставников, логин брокера. Репозиторий — только локальный или приватный.
