# Сверка с Raven — 2026-10-18

Raven: коммит `5a675b61` (неизменённая копия). Отчёт строится скриптом `packaging/scripts/compare_raven.py`; ТЗ 11.4, шаги 1–5. Шаги 6–8 (тесты, 30 сценариев, импорт данных) — вручную, см. в конце.

## 1. RPC

В Raven есть, в Rarus нет: нет.

Добавлено в Rarus (3):
- `org.get`
- `org.set`
- `rarus.account`

Общие методы с другим описанием (2):
- `employees.hire`
- `employees.list`

## 2. Интерфейс (`ui-web/src/features/`)

Папки Raven без соответствия в Rarus: нет.

Новые папки Rarus (1):
- `canvas`

## 3. Командная строка

Команды Raven, которых нет в Rarus (12):
- `accounts`
- `accounts add`
- `accounts audit`
- `accounts init`
- `accounts list`
- `accounts passwd`
- `accounts price`
- `accounts role`
- `accounts serve`
- `accounts set`
- `accounts stop-engines`
- `accounts topup`

Команды, добавленные в Rarus (1):
- `migrate-raven`

Команды с другим набором параметров: нет.

## 4. Конфигурация

Ключи Raven, которых нет в Rarus: нет.

Ключи, добавленные в Rarus (14):
- `providers.rarus`
- `providers.rarus.apiBase`
- `providers.rarus.apiKey`
- `providers.rarus.endpointStrategy`
- `providers.rarus.endpoints`
- `providers.rarus.endpoints.[].apiBase`
- `providers.rarus.endpoints.[].apiKey`
- `providers.rarus.endpoints.[].extraHeaders`
- `providers.rarus.endpoints.[].label`
- `providers.rarus.extraHeaders`
- `providers.rarus.modelOverlay`
- `providers.rarus.modelProtocols`
- `providers.rarus.models`
- `providers.rarus.protocol`

## 5. Каталог сообщений

Ключи Raven, которых нет в Rarus: нет.

Ключей, где в en/zh только «Raven» заменено на «Rarus»: 120.

Ключи с другим текстом en/zh (кроме названия) (2):
- `gui.onb.key_s`
- `gui.panel.custom_agents`

Ключи Rarus без русского текста: нет.

Добавлено ключей в Rarus: 91.

## 6–8. Не выполнялось этим скриптом

- Тесты: полный прогон Rarus сравнивается с базой падений Raven на Windows (`tools/dev/baselines`); итог — в `docs/reports/milestone-M7.md`.
- 30 сценариев (ТЗ 12.6): требуют живой модели и человека; результаты — в `docs/compat/scenarios.md`.
- Импорт данных: `rarus migrate-raven` проверен тестами на тестовом `RAVEN_HOME`; на чужих данных не запускался.
