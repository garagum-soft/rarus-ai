# Передача работы: Rarus AI — где остановились и что делать дальше

> **Обновление (2026-10-17, позже того же дня).** Разделы 6.1–6.5 выполнены: канвас, экран «Аккаунт», проверки дизайна, сборка PyInstaller, сверка с Raven. Итоги: `milestone-M5.md`, `milestone-M6.md`, `milestone-M7.md`, `docs/compat/`. Позже (18.10) собран и проверен установщик NSIS со встроенными шрифтами и загрузчиком WebView2 (`milestone-M6.md`, «Итоговая сборка 18.10»); чат проверен в окне на подставном API (`tools/dev/fake_llm.py`). Итог проекта — `milestone-M8.md`. Осталось: установка на чистую ВМ, живые сценарии с настоящей моделью (`docs/compat/scenarios.md`), иконка Rarus. Решения 24–42 — в `decisions-by-executor.md`. Правило про скачивание ниже владелец снял 18.10 («даю тебе все разрешения…»), но push/PR, чужие ключи и системные настройки по-прежнему только по его слову.

> **Сервер (18.10, вечер).** Сайт Rarus AI поднят на `https://rarus-ai.com` (второй процесс на VDS, порт 8120, общая база с alembox.com и sanlykatip.com; сертификат до 2027-01-16; блоки nginx на VDS и на верхней машине 192.168.2.10). Что именно сделано, как продлевать сертификат и что проверил/не проверил — `docs/release.md`, раздел «Домен rarus-ai.com», решения 48–50. Витрина со скачиванием (страница `MxRarus.jsx`), пункт «Скачать для Rarus AI» в чате sanlykatip.com, маршруты и установщик 0.1.0 выложены (решение 51). Не сделано: вход одним паролем на обоих сайтах проверяет владелец; создание репозитория GitHub и push; кнопка «обновить» в самой программе; переключение клиента на rarus-ai.com (три строки, см. `release.md`).

Дата снимка: 2026-10-17. Составил предыдущий исполнитель (Claude, сессия 8cbbab59…). Файл написан так, чтобы следующий агент прочитал его, `TECH_SPEC.md` и продолжил без вопросов владельцу.

> **Первое действие:** прочитай разделы 1–3 этого файла, затем `C:\235235\agent_orchestration\rarus_ai_spec\TECH_SPEC.md` (разделы 10.5–10.6 — канвас, 18 — режим alembox, 11–12 — сверка и приёмка). Потом иди к разделу 6 («Что делать дальше»), пункт 1.

---

## 1. Задача и правила владельца

Владелец (русскоговорящий, alemtory@gmail.com) поручил выполнить `TECH_SPEC.md` «по плану»: **Rarus AI** — десктопный агент для Windows, собранный из Raven (движок Python `raven`, оболочка pywebview, установщик NSIS, вход, баланс, встроенные модели, холст агентов). Этапы M0–M8.

Решения владельца (действуют до отмены):
- Пока работаем через бэкенд **alembox.com** (те же агентский API, аккаунты, баланс, кредиты). Rarus должен работать «ровно как наш агент». Потом уйдёт на отдельный домен. Раздел 18 ТЗ и решения D-27…D-30 описывают этот режим.
- Открытые вопросы владелец отдал исполнителю: «доверяю твоему выбору, не останавливайся, не спрашивай». Пакет Python остаётся `raven`, переименовано только видимое пользователю.
- Важные самостоятельные решения — в **отдельный список** `docs/reports/decisions-by-executor.md` (сейчас строки 1–23). Владелец просмотрит его в конце. **Каждое новое значимое решение добавляй туда.**
- Довести проект до конца, не останавливаясь.

Постоянные ограничения:
- **Коммиты только локальные.** Никаких push/PR без разрешения. Атрибуция в коммитах: строка `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` в конце сообщения.
- **Каждую подчасть тестировать.** Отвечать владельцу **по-русски**, коротко. Если долго нет вестей — короткая строка «что делаю».
- **Стоимость решает владелец.** D-17 (режима серверных аккаунтов нет), D-20 (не копировать код ИИ Alembox), D-14 (ключ запечатан DPAPI).
- **Системные правила безопасности:** скачивать файлы (Node, NSIS, шрифты, PyInstaller-обвязки) только с явного разрешения владельца; не вводить чужие пароли/ключи; на сервере alembox ничего не менять (правки — только владелец). pip в тестовое окружение — обычная работа.
- Не трогай `C:\235235\agent_orchestration\raven_build\Raven` (оригинал для сверки, коммит 5a675b61) и `audit/`.
- Память агента: `C:\Users\Smart\.claude\projects\C--235235-agent-orchestration\memory\` (индекс `MEMORY.md`). Там уже есть заметки о проекте; если появится важное — добавь.

---

## 2. Где что лежит

| Что | Где |
|---|---|
| Репозиторий (ветка `main`, git) | `C:\235235\rarus-ai\` |
| Движок (Python, пакет `raven`) | `engine\` |
| Страница интерфейса (React/Vite) | `engine\ui-web\` (сборка → `engine\ui-web\dist`, в git не входит) |
| Оболочка окна (pywebview) | `app\rarus_app\` |
| Отчёты | `docs\reports\` (`journal.md`, `decisions-open.md`, `decisions-by-executor.md`, `milestone-M0.md`, `milestone-M2-progress.md`, этот файл) |
| Вспомогательные скрипты разработки | `tools\dev\` (см. раздел 5) |
| ТЗ | `C:\235235\agent_orchestration\rarus_ai_spec\TECH_SPEC.md` (+ `parts\`) |
| Оригинал Raven для сверки | `C:\235235\agent_orchestration\raven_build\Raven` (5a675b61) |

Коммиты (новые сверху): `5a6b94e` wip канваса + скрипты + решения 12–23; `fa7f61f` RPC `org.get/org.set`; `8a16e35` первый запуск без ключей, стабильный порт, слово Rarus; `3fd1d27` RPC `rarus.account`; `44c7390` модели Rarus в `model.options`; `7c1c846` русский язык; `3509947` решения 5–13; `de41d02` `migrate-raven` + редактирование ключей в журналах; `ed1d17a` оболочка, Host guard; `ac169ce` команда `rarus`, без «звонка домой»; `ffc07b1` Rarus — модель по умолчанию; `91a03f3` провайдер и вход; дальше — M0/M2 (RARUS_HOME, вырезано лишнее).

---

## 3. Что сделано (проверено тестами)

**Движок**
- Провайдер `rarus` (`raven/providers/rarus_session.py` — ключ в `oauth/rarus.json`, DPAPI; `rarus_client.py` — whoami/models; `rarus_login.py` — вход PKCE через браузер, loopback; `rarus_provider.py`; `rarus_models.py` — встроенные `deepseek-flash`, `auto` + обновление из `/v1/models`; `rarus_account.py` — статус аккаунта, общий для оболочки и страницы).
- Каталог `RARUS_HOME` (по умолчанию `~/.rarus`) вместо `~/.raven`; команда `rarus` (+ псевдоним `raven`); проверка обновлений Raven отключена (нужен `RARUS_UPDATE_URL`).
- Защита локального сервера: Host-guard, Origin, nonce→HttpOnly cookie, токен; журналы с редактированием ключей всех форм; `rarus migrate-raven`.
- `setup.status`: вошедший аккаунт Rarus = настроенный провайдер. RPC `rarus.account`, `org.get`, `org.set` (чарт `subagents.org`: лес, без циклов, глубина ≤ 6; `raven/agent/subagent/org_store.py`). Контракт: `rpc-schema/openrpc.json`, `raven/rpc/models.py` (`METHOD_MODELS`), `ui-web/src/rpc/generated.ts` (генерируется `node scripts/gen-rpc-client.mjs`).
- Язык `ru` (конфиг, ответы агента, `ensure_language`).

**Страница**
- Русский каталог (`i18n/messages.json`, колонки en/zh/ru, ~3150 ключей `ui`), тёмная тема по умолчанию (токен-блок Rarus в `page.css`), слово «Rarus AI» в заголовке/заставке/левой панели (значок птицы `RavenMark` оставлен — нужен значок от дизайнера).
- Первый запуск без ключей: шаги «Аккаунт» (почта, баланс) → «Вид» (язык, тема) → «Запуск» (автозапуск, только внутри окна). Файлы: `features/onboard/*`, `lib/shell.ts` (мост окна). Мастер открывается, когда `setup.status` — первый запуск, ИЛИ мост окна `first_run()`, ИЛИ `?onboard=1`. Проверено в браузере (все три шага).
- Экран моделей показывает `Rarus AI` как подключённый провайдер.

**Оболочка (`app/rarus_app`)**: окно pywebview + трей (pystray, опционально), один экземпляр, движок как дочерний процесс на порту **47821** (если занят — любой свободный; нужен, чтобы localStorage страницы переживал перезапуск), стартовая страница входа, баланс, автозапуск (реестр), клиент обновлений (https + SHA-256 + откат), метка первого запуска `<RARUS_HOME>/onboarded` через мост. 62 теста оболочки.

**Решения исполнителя** — `docs/reports/decisions-by-executor.md`, строки 1–23.

---

## 4. Состояние тестов

- Python-тесты движка: на Windows у Raven есть ~940–970 **заранее падающих** тестов (права POSIX, симлинки, `claude_subscription` и т. д.). Правило: **сравнивать с оригиналом**, не с нулём. Список падающих в оригинале — `tools/dev/baselines/m2_full2.base.fail.txt` (967 строк). Новый тест не должен добавлять падений сверх базы. Последние прогоны затронутых файлов (`test_rarus_provider`, `test_cli_provider_commands`, `test_rpc_model`, `test_rpc_setup`, `test_rpc_rarus`, `test_rpc_org`, `test_rpc_schema_match`, `test_rpc_registration`, `test_i18n_russian`) зелёные, кроме 3 известных (права файлов в `test_cli_provider_commands`).
- **Полный прогон движка после последних изменений ещё не делался** (последний полный — M3). Нужно: `tools/dev/full_mine.ps1 -Tag full4`, сравнить с базой.
- Оболочка: `app\tests` — 62 passed.
- UI (vitest): 3781+ тестов, **известные падения не мои:** `Roles.test.tsx` ×2 (есть и в Raven). Гейты `scripts/gates` — 47 файлов зелёные на момент коммита `8a16e35`. Сборка `vite build` + `ui-web/build.py` (boot-snapshot golden) — зелёная на `8a16e35`.
- **После WIP-коммита `5a6b94e` type-check и часть гейтов красные** (канвас написан, но не зарегистрирован — раздел 6, пункт 1). Это нормально для WIP; после регистрации всё должно позеленеть.

---

## 5. Окружение и приёмы (важно, иначе потеряешь часы)

- Windows 10, **PowerShell 5.1 + Bash**. `uv` — `C:\Users\Smart\.local\bin\uv.exe`, каталоги UV в `C:\uvdata`.
- **В bash heredoc портит обратные слэши и кавычки.** Содержимое с `\` или сложными кавычками пиши инструментом Write/Edit, а скрипты запускай из файла.
- **Нет Node.js.** `tools/dev/node.cmd` запускает встроенный в VS Code Electron как Node (`ELECTRON_RUN_AS_NODE=1`). Junction `engine\ui-web\node_modules` → `node_modules` копии Raven (`...\raven_build\Raven\ui-web\node_modules`). Запуск: `$env:Path = "<папка с node.cmd>;" + $env:Path; node.cmd node_modules/vitest/vitest.mjs run …`; сборка: `node.cmd node_modules/vite/bin/vite.js build`, затем `python build.py` (в venv). type-check: `node.cmd node_modules/typescript/bin/tsc --noEmit -p tsconfig.json`; lint: `node.cmd node_modules/eslint/bin/eslint.js <пути>`.
- Окружения Python (были в scratchpad прошлой сессии — **могут исчезнуть**, пересоздай): `rarus_venv` = Python 3.12, `pip install -e C:\235235\rarus-ai\engine` (+ extras для тестов: pytest, pytest-asyncio, pytest-timeout, pytest-xdist, pytest-cov), плюс `pywebview`, `pystray`, `pillow`, `ruff`; `base_venv` + `raven_base` = нетронутая копия Raven (`git archive 5a675b61 | tar -x`) для сверки. Тесты **сами должны задавать `RARUS_HOME`**; `PYTHONUTF8=1` обязательно; подставной профиль `fakehome2` (USERPROFILE/HOME на пустую папку).
- Скрипты в `tools/dev/` содержат **жёсткие пути к scratchpad прошлой сессии** (`$sp = ...\scratchpad`): при первом использовании поправь переменную `$sp` и путь к `run_nowin.py` (лежит рядом). Что есть:
  - `run_nowin.py` — запускает процесс без консольного окна, пишет в лог (нужен, иначе pytest-xdist на Windows плодит окна).
  - `full_mine.ps1 -Tag X [-Files …]` — pytest по движку, лог `X.log`, маркер `X.done`; `cmp_run.ps1` — то же на Rarus и на оригинале + diff падений.
  - `add_keys.py keys.json` — добавить/заменить ключи каталога i18n (`{ключ:{en,zh,ru}}`); `keys_canvas.json` — строки канваса (уже применены).
  - `hold_signed_in.ps1` + `engine_hold.py` + `fake_site.py` — поднять настоящий движок с демо-сессией и подставным сервером аккаунта (`RARUS_SITE=http://127.0.0.1:47900`: whoami → баланс 12400); URL входа попадает в `hold.txt`. Порт движка 47821; страница открывается по `…/auth#<nonce>`, затем `/?onboard=1`.
  - `probe_*.py` — разовые проверки RPC без UI; `smoke_real_engine.py`, `window_smoke.ps1` — дымовые проверки оболочки.
  - `i18n_*.py` — перенос перевода (разовые).
- `model.options` на этой машине отвечает ~30 с (первый импорт LiteLLM, нагрузка) — страница просто ждёт; это не баг (у оригинала так же).
- Ограничение окружения: в окне браузера Claude есть эмуляция вьюпорта 1280x800 — сбрось `resize_window` preset `desktop`, когда закончишь.

---

## 6. Что делать дальше (по порядку)

### 6.1. Закончить канвас агентов (M5, ТЗ 10.5–10.6) — **остановились здесь**

Уже написано и закоммичено (WIP `5a6b94e`), каталог `engine/ui-web/src/features/canvas/`:
- `graph.ts` + `graph.test.ts` — чистая модель: цепочки, уровни, ветка, `setParent` (запрет self/цикл/глубина>6/неизвестный), `layoutTree`, `fitView`, `zoomAt`, `cardAt`, `cardsIn`, клавиатура (`step`, `treeOrder`), экспорт/импорт JSON, путь связи. **31 тест зелёный.**
- `types.ts`, `source.ts` (читает `subagents.list`, `employees.list`, `org.get`; пишет `org.set`, `employees.prompt_get/set`), `store.ts` (состояние, оптимистичное `apply` с откатом при отказе движка, отмена Ctrl+Z на 40 шагов, выбор карточек/связи, рамка, история промпта и позиции в localStorage), `CanvasPage.tsx` (экспортирует `CanvasApp`: холст SVG+HTML, панорама, колесо, перетаскивание карточки на карточку = смена руководителя, Shift+перетаскивание = рамка, клавиши, инспектор с вкладками «Основное/Промпт/Связи», панель экспорта/импорта), `styles.css` (палитра приложения H ТЗ, префикс классов `cv-`), `manifest.ts`.
- Строки `gui.cv.*`, `gui.page.canvas`, `gui.nav.canvas` уже в каталоге (43 ключа, en/zh/ru).

**Осталось зарегистрировать страницу — это «девять мест» (см. комментарий в `state/pages.ts`):**
1. `src/state/pages.ts` → в `DECLARED` добавить `{ id: 'canvasPage', bodyId: 'canvasBody', navButtons: ['canvasBtn'], escapeRank: 4, head: 'gui.page.canvas', aria: 'gui.page.canvas' }`.
2. `src/state/escapeOrder.ts` → в `CLOSERS` добавить `canvasPage: () => page.show(null)`.
3. `src/chrome/Rail.tsx` → в `NAV_ROWS` строка `{ button: 'canvasBtn', key: 'gui.nav.canvas', open: () => openCanvas(), icon: … }` (`openCanvas` = `open` из `features/canvas/store`; значок — свой SVG или существующий из `components/Icon`). Следи за гейтом `import-direction`.
4. `src/state/sources.ts` → тип `canvas: CanvasSource` в `Sources`.
5. `src/app/install.ts` → `sources.canvas = canvasSource`.
6. `src/features/manifests.ts` → импорт `manifest as canvas` и добавить в `MANIFESTS` (поправь комментарий «twenty»).
7. Эталоны (golden): `src/test/__golden__/region-app.txt`, новый `region-canvasPage.txt`, `scripts/__golden__/boot-stub.txt` и `boot-live-noserver.txt`. Как обновлять — читай `src/test/regions.test.ts` и `scripts/boot-snapshot.mjs` (обычно `vitest -u` / флаг обновления).
8. Офлайн-фикстуры: `src/rpc/fixtures/settings.ts` — ответы `org.get`/`org.set` (гейт `offline-coverage` требует ответчика на каждый вызываемый метод); `employees.*` и `subagents.list` уже есть.
9. Тесты: `features/canvas/store.test.ts` (reparent с отказом/откатом, undo, import/export, промпт с историей), `CanvasPage.test.tsx` (карточки, выбор, Delete с подтверждением, клавиши стрелок, инспектор, панель файла). Образцы стиля — `features/onboard/*.test.*`.
10. Прогнать: `vitest run` целиком, `scripts/gates` (особенно `domain-shape`, `domain-registration`, `class-namespace` — префикс `cv`, `check-css`, `i18n-keys`, `rail-nav-registry`), type-check, lint, `vite build` + `build.py`; затем проверить в браузере через `tools/dev/hold_signed_in.ps1` (открыть «Канвас агентов» в левой панели; карточки видны? перетаскивание на карточку пишет `subagents.org` в `hold_home\config.json`? отказ показывается?). Для живой проверки создай несколько записей в `subagents.org` или наймите сотрудников через `employees.hire`.

**Сознательно не сделано (занести в `decisions-by-executor.md`, если не успеешь):** метки агентов и фильтр (FR-165, критерий 6), выбор модели через `ModelPicker` (нужен RPC смены модели сотрудника; сейчас модель — только чип), переключатели инструментов/знаний (FR-166), пять видов связей (FR-167: сейчас одна — «делегирование»), режим «Запуск» (FR-168), режим «Список», аватар-кадр, 200 карточек/500 связей по частоте кадров (критерий 7 — замерить). Это части ТЗ 10.6 п. 5–7; их надо либо реализовать, либо явно зафиксировать как отложенные в отчёте.

### 6.2. Экран «Кредиты и счёт»
Данные — RPC `rarus.account` (почта, баланс, ошибка). Нужен экран/раздел (например, раздел настроек «Аккаунт»): почта, баланс, кнопки «Обновить», «Выйти» (выход делает оболочка: `window.pywebview.api.sign_out()` + вернуть окно на страницу входа; в обычном браузере — только «Выйти через окно Rarus AI»). Макеты/ТЗ: раздел 10 и 18 (списания, пополнение — через сайт alembox).

### 6.3. Остальные экраны под токены (10.3) и критерии 10.4
Контраст (автоматическая проверка по токенам, отчёт), отсутствие внешних CDN и шрифтов, «уменьшить движение», ru/en/zh помещаются. Шрифты Instrument Sans/JetBrains Mono **не скачаны** — решение 23; пока системные.

### 6.4. Упаковка (M6)
PyInstaller (можно через pip в venv), сценарий NSIS (**компиляция требует скачать NSIS — спроси разрешение владельца**), гейты `gate-no-secrets` и `gate-no-external-runtime`, README ru/en. Если скачивать нельзя — подготовь скрипты и записи в отчёт, что не собрано и почему.

### 6.5. Финал (M7–M8)
Сверка с Raven (ТЗ 11.4, сценарии 12.6), `docs/compat/parity-matrix.csv`, итоговый отчёт, обновление `journal.md`, `milestone-*.md`, полный прогон Python-тестов против базы (раздел 4), журнал решений. Затем доложить владельцу по-русски: что сделано, что отложено и почему, что нужно от него (разрешение на скачивание NSIS/шрифтов, значок Rarus, домен, модели на сервере — пункт 4 решений).

### 6.6. Хозяйство
- Закоммитить локально после каждой подчасти; не пушить.
- Обновить память агента (`memory/MEMORY.md`): статус «M5 канвас в работе/готов», ссылка на этот файл.
- Все долгие запуски — в фоне, с коротким отчётом владельцу.

---

## 7. Подводные камни, на которые уже наступили
- Heredoc/кавычки в bash — см. раздел 5. Окончания строк: файлы в рабочем дереве смешаны CRLF/LF; при правке скриптами сохраняй исходный стиль (или приводи `sed -i 's/\r$//'`); git сам предупреждает.
- `RarusError(…, 401)` → нужно `status=401` ключевым аргументом.
- Тесты, затрагивающие процессы, ищут имя процесса: распознаются `-m rarus`, `-m rarus_app --engine`, `RarusAI.exe --engine`, скрипты `raven|rarus` (`raven/utils/processes.py`).
- Файловый журнал сначала редактирует секреты, потом подставляет маркер `<redacted>` (порядок важен для тестов).
- Гейт `rail-sig-css` считает токены в CSS — не переопределяй чужие токены в блоке Rarus.
- `build.py` встраивает бандл и проверяет «boot-snapshot» по golden: любое изменение разметки заставки/каркаса ломает его — обновляй эталоны осознанно.
- Тесты мастера используют `store._showStepsForTests(store.STEPS)` для старых четырёх шагов; новые шаги — `RARUS_STEPS`.
- Страница ждёт `model.options` ~30 с — не принимай это за поломку.
- Не запускай `hold_signed_in.ps1` дважды подряд: он стирает `hold_home`; перед повтором останови прежний процесс (и подставной сервер на 47900).

---

## 8. Как проверить, что всё в порядке на старте
```
cd C:\235235\rarus-ai
git log --oneline | head -3        # ожидается 5a6b94e наверху
git status --short                 # должно быть чисто
```
Затем: пересоздать venv (раздел 5), прогнать `app\tests` (62 зелёных), `engine\tests\test_rpc_*.py` затронутых файлов, `vitest run src/features/canvas` (31 зелёный) — это быстрая проверка, что среда рабочая. После этого — раздел 6.1.
