# Скиллы tradermonty (сторонние)

Источник: https://github.com/tradermonty/claude-trading-skills, коммит `9540ae4` (2026-11-02), лицензия MIT (`LICENSE`).
Взяты только скиллы, которым не нужны платные API (FMP / FINVIZ / Alpaca):

| Скилл | Зачем | Данные |
|---|---|---|
| `us-stock-analysis` | разбор одной бумаги США: фундаментал, оценка, техника, отчёт | веб-поиск |
| `market-environment-analysis` | режим рынка risk-on / risk-off, индексы, ставки, сырьё | веб-поиск |
| `scenario-analyzer` | сценарии на 18 мес. по новости/событию: кто выиграет, кто проиграет | веб-поиск |
| `position-sizer` | размер позиции от риска (fixed fractional / ATR / Kelly), целые акции | stdlib-скрипт |
| `technical-analyst` | техника по скриншоту недельного графика | картинка от пользователя |

Локальные правки (при обновлении повторить):
- удалены `scripts/tests/`;
- в `position-sizer/SKILL.md` и `technical-analyst/SKILL.md` путь `skills/<скилл>/scripts/…` заменён на `~/.claude/skills/<скилл>/scripts/…`.

Симлинки: `ln -sfn "$PWD/freedom-broker/skills/vendor/tradermonty/<скилл>" ~/.claude/skills/<скилл>`.

Скиллы рассчитаны на рынок США. KZ-бумаги (KASE/AIX), облигации и правила портфеля (5%, 40/30/30, наставники) учитывает
только `fb-advisor` — он вызывает эти скиллы как инструменты, а не наоборот.
Обновление: склонировать репозиторий, сравнить `diff -r`, перенести нужное, обновить коммит выше.
