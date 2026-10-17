# Rarus AI

Rarus AI — настольный агент для Windows: окно, в котором работает ИИ-агент с инструментами, памятью, навыками, знаниями, расписанием и холстом иерархии агентов. Движок — [Raven](engine/README.md) (Apache-2.0, коммит `5a675b61`), оболочка окна — `pywebview`, вход, баланс и модели — через бэкенд alembox.com (пока без своего домена; техзадание, раздел 18).

*Rarus AI is a Windows desktop AI agent built on the Raven engine. English notes are at the end of this file.*

## Из чего состоит

| Каталог | Что там |
|---|---|
| `engine/` | движок (Python-пакет `raven`, команда `rarus`) и страница интерфейса `engine/ui-web` (React, Vite) |
| `app/` | оболочка `rarus_app`: окно, трей, один экземпляр, запуск движка, вход, автозапуск, обновления |
| `packaging/` | сборка программы и установщика (PyInstaller, NSIS), проверка сборки на секреты |
| `tools/dev/` | вспомогательные скрипты разработки |
| `docs/reports/` | журнал, решения исполнителя, отчёты по вехам, отчёт о контрасте |

Данные пользователя лежат в `%USERPROFILE%\.rarus` (переменная `RARUS_HOME`): чаты, память, настройки; ключ входа запечатан средствами Windows (DPAPI).

## Запуск из исходников

Нужны Python 3.12 и `uv`.

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e engine -e "app[gui]"
.venv\Scripts\python.exe -m rarus_app
```

Окно покажет страницу входа Rarus AI; после входа открывается движок на `127.0.0.1:47821`.

## Страница интерфейса

Нужен Node.js 20+.

```powershell
cd engine\ui-web
npm ci
npm run build          # бандл Vite
python build.py        # сборка dist/index.html и проверка снимка загрузки
npx vitest run         # тесты страницы и гейты scripts/gates
```

## Проверки

```powershell
.venv\Scripts\python.exe -m pytest app\tests packaging\tests
.venv\Scripts\python.exe -m pytest engine\tests      # на Windows часть тестов Raven падает и до наших изменений:
                                                     # сравнивайте с базой tools\dev\baselines
```

## Сборка программы и установщика

```powershell
powershell -File packaging\scripts\build.ps1 -Version 0.1.0
```

Шаги: страница → колесо движка → тесты → PyInstaller (`build\dist\RarusAI`) → проверка на секреты и внутренние адреса → пробный запуск → NSIS (`build\RarusSetup-<версия>.exe`) → SHA-256. Шаг NSIS пропускается, если `makensis` не установлен. Подробности: [packaging/README.md](packaging/README.md).

## Лицензия

Apache-2.0 (из Raven). Уведомления: [NOTICE](NOTICE), `engine/NOTICES.md`, `engine/LICENSES/`.

---

## English

* **Layout.** `engine/` is the Raven engine (Python package `raven`, command `rarus`) with the page in `engine/ui-web`; `app/` is the window shell (`rarus_app`); `packaging/` builds the frozen program and the NSIS installer.
* **Run from source.** `uv venv --python 3.12 .venv`, then `uv pip install --python .venv\Scripts\python.exe -e engine -e "app[gui]"`, then `.venv\Scripts\python.exe -m rarus_app`.
* **Page.** `cd engine\ui-web; npm ci; npm run build; python build.py; npx vitest run`.
* **Package.** `powershell -File packaging\scripts\build.ps1 -Version 0.1.0` (the installer step needs `makensis` on PATH).
* **Data** lives in `%USERPROFILE%\.rarus` (`RARUS_HOME`). Sign-in, balance and models go through the alembox.com agent API for now.
