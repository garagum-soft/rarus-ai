# M7 (сверка с Raven): итог — 2026-10-17

Сверка по ТЗ 11.4. Raven — неизменённая копия коммита `5a675b61` во временной папке; Rarus — этот репозиторий. Отчёты лежат в `docs/compat/`.

## Что сверено

| Шаг ТЗ 11.4 | Чем | Итог |
|---|---|---|
| 1. RPC | `packaging/scripts/compare_raven.py`, `openrpc.json` | добавлены только `org.get`, `org.set`, `rarus.account`; ничего не убрано; у `employees.hire/list` отличается только `<RARUS_HOME>` в описании |
| 2. Страница | то же, папки `ui-web/src/features` | добавлена `canvas`, ничего не убрано |
| 3. Командная строка | то же, обход typer | убрана группа `accounts` (12 команд, решение D-17), добавлена `migrate-raven` |
| 4. Конфигурация | то же, схема `Config` | ничего не убрано; добавлено 14 ключей `providers.rarus.*` |
| 5. Сообщения | то же, `i18n/messages.json` | 91 ключ добавлен, ни один не убран; у двух ключей отличается только путь `~/.raven` → `~/.rarus`; у всех ключей Rarus есть русский текст |
| 6. Тесты | полный прогон Python на обеих копиях, прогон страницы | см. ниже |
| 7. 30 сценариев | `docs/compat/scenarios.md` | 9 не выполнены (нужна живая модель, сеть до alembox, аккаунт владельца или чужое оборудование), №21 выполнен частично, остальные покрыты тестами; руками на настоящем движке пройден канвас |
| 8. Импорт данных | `rarus migrate-raven` | проверен тестами на тестовом `RAVEN_HOME`; на данных владельца не запускался |

Матрица приложения А (62 строки) — `docs/compat/parity-matrix.csv`, строится `tools/dev/parity_matrix.py`. Отчёт скрипта — `docs/compat/2026-10-18-raven-parity.md`.

## Полный прогон Python (движок)

Обе копии прогонялись одинаково: Windows 10, 4 процесса `pytest-xdist`, 180 с на тест, пустой временный профиль.

| | Raven | Rarus |
|---|---|---|
| тестов собрано | 29 866 | 29 319 |
| пройдено | 28 553 | 28 020 |
| падений | 967 | 941 |
| ошибок (setup) | 68 | 70 |
| пропущено | 312 | 287 |

Тестов в Rarus меньше: вместе с убранным (режим аккаунтов D-17, бета-канал, Docker и установщики Raven, `experimental/`, `evolver/`, симуляции) убраны и их тесты.

Падения сверены по именам, а не по числу:

- **Новых падений было 3, все исправлены:** `test_cli_smoke::test_registered_command_set_is_pinned` (в закреплённый список не была внесена `migrate-raven`; решение 36) и два теста бюджета циклов импорта (`test_no_new_package_is_drawn_into_a_cycle`, `test_no_new_pair_of_subpackages_imports_each_other`: мой `config/update_org.py` импортировал `agent`; модуль перенесён в `agent/subagent/org_store.py`, решение 35). После правок эти тесты, `test_rpc_org` и весь `test_cli_smoke` проходят (57 пройдено, 1 падает: `test_no_new_module_is_drawn_into_a_cycle`, он падает и в Raven: 48 модулей в циклах при потолке 44, число в обеих копиях одинаковое).
- **Падения Raven, которых нет в Rarus: 29.** 20 из них — в файлах тестов, которые убраны вместе с функцией (`docker_runtime`, `appworld_sandbox`, `harness_curator_*`, `simulation_*`, `updates_beta_channel`). Ещё 9 — в файлах, которые остались (`agents_research_flow_finalize`, `cli_playbook_commands`, `constraints_export_contract` ×2, `ppt_engine_templates`, `rpc_console`, `runtime_checkpoint`, `subagent_acp` ×2): там Rarus проходит, а Raven падает; причину не выясняли; причина не выяснена.
- **Ошибки: 70 против 68.** Все ошибки — setup фикстур в `test_sandbox_debug_server` и `test_cli_agents_commands` (на Windows). На этих двух файлах отдельно обе копии дают одинаково 5 падений и 41 ошибку; два лишних в полном прогоне — на других файлах и не разобраны. Прежние полные прогоны Rarus (`m2_full2`, `m3_full`) тоже давали 70.
- Список падений Raven на Windows (база) лежит в `tools/dev/baselines/m2_full2.base.fail.txt`.

Тесты оболочки и сборки: `app/tests` и `packaging/tests` — 85 пройдено (в окружении сборки).

## Страница (vitest)

`vitest run` в `engine/ui-web`: 252 файла, **3893 теста пройдено, 2 падают** (`settings/providers/Roles.test.tsx`, оба падают и в Raven: `tools/dev/baselines/vt_base.txt`). Гейты `scripts/gates` идут в том же прогоне. `tsc --noEmit`: 0 ошибок. `eslint .`: 0 ошибок, 4 старых предупреждения `exhaustive-deps` (они же в Raven). `gen-rpc-client.mjs --check`: клиент совпадает с контрактом, 248 методов.

## Что не сделано

- 30 сценариев ТЗ 12.6 на живой модели и в собранной программе (9 в списке `scenarios.md`).
- Сравнение с Raven на чужих данных: `rarus migrate-raven` не запускался на настоящем `~/.raven` владельца.
- Падения Raven на Windows (967) не чинились: это задача Raven, а не Rarus; они сравнены, не исправлены.
