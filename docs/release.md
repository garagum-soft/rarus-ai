# Выпуск Rarus AI: GitHub, Actions и сайт sanlykatip.com

Состояние на 2026-10-18. Всё, что можно сделать без доступа к вашему серверу и к GitHub, сделано и
проверено на этой машине. Выкладка на сервер и создание репозитория — за вами (команды ниже).

## Что готово

| Что | Где | Проверено |
|---|---|---|
| CI на каждый push и PR | `.github/workflows/ci.yml` (страница, оболочка и упаковка на Windows, провайдер Rarus) | YAML разобран, шаги выполнены по отдельности здесь; на самом GitHub не запускался |
| Выпуск по тегу `vX.Y.Z` | `.github/workflows/rarus-release.yml`: сборка `build.ps1` на `windows-latest`, потом выкладка на сервер | YAML разобран; шаг версии и серверная часть выкладки прогнаны на временной папке (в том числе отказ при несовпадении SHA-256); сборка установщика — как здесь, вручную |
| Ключ `-GatesOnly` у `build.ps1` | останавливает сборку после тестов, нужен для CI | запущен здесь, 93 теста |
| Клиент обновлений | `app/rarus_app/updater.py`: адрес `https://sanlykatip.com/api/desktop/rarus/latest.json`, ключ аккаунта только на свои хосты, установщик только с доверенного хоста, SHA-256 обязателен | 78 тестов оболочки |
| Маршруты сайта | `wan-site/backend/rarus_dl.py` (`/api/desktop/rarus/latest`, `/latest.json`, `/download`), подключён в `main.py` | 7 тестов, окружение временное |
| Пункт «Rarus AI» в меню «Скачать» | `wan-site/new_site/src/mx/pages/MxChat.jsx` и три языковых файла; пункт Alembox AI остаётся как был, пока Rarus не выложен | сборка vite в отдельную папку прошла; в браузере не смотрел |

Файлы сайта лежат в рабочем дереве `C:\235235\wan-site`, **не закоммичены и не выложены**: рядом лежат
чужие неоформленные правки (`CLAUDE.md`, `agent_api.py`, `deploy/worker@3.vds.conf`, заметки).

## Шаг 1. Репозиторий на GitHub

Создайте на GitHub **приватный** пустой репозиторий (без README), например `alemtory-png/rarus-ai`, и выполните:

```bash
cd C:\235235\rarus-ai
git remote add origin https://github.com/alemtory-png/rarus-ai.git
git push -u origin main
```

Приватный — потому что установщик отдаётся только вошедшим, а у публичного репозитория были бы видны
история и Actions-артефакты. Лицензия движка Raven — Apache 2.0, `NOTICE` в корне это учитывает.

## Шаг 2. Серверная часть (по правилам `wan-site/CLAUDE.md`: `scp` + `sudo cp` + перезапуск)

1. Папка выдачи (одна, новая, рядом с папкой Alembox AI):
   `mkdir -p /opt/wan-site/data/desktop/rarus` (путь — `$WAN_DATA/desktop/rarus`; пользователь, под которым
   зайдёт Actions, должен иметь право писать в неё).
2. Бэкенд: положить `backend/rarus_dl.py` и обновлённый `backend/main.py` (две строки), выкладывать `mv`, не `cp`.
   Перезапуск — только `/opt/wan-site/restart_backend.sh`.
3. Проверка с авторизацией: без токена `GET /api/desktop/rarus/latest` обязан дать 401, с токеном —
   `{"available": false}`, пока файла нет.
4. Тесты на сервере: `ssh wan "cd /opt/wan-site/app/backend && /opt/wan-site/venv/bin/python -m pytest tests/test_rarus_dl.py -q"`.
5. Страница: положить `new_site/src/mx/pages/MxChat.jsx` и `new_site/src/mx/lang/{en,ru,tk}.js`, затем
   `flock -w 600 /opt/wan-site/templates.lock npm run build` в `new_site` (на VDS нельзя `npm install`).

## Шаг 3. Секреты репозитория

Settings → Secrets and variables → Actions → New repository secret. Названия те же, что у Alembox AI:

| Секрет | Что |
|---|---|
| `DEPLOY_SSH_KEY` | закрытый ключ отдельного пользователя, которому разрешена запись только в `DEPLOY_PATH` |
| `DEPLOY_KNOWN_HOSTS` | строка сервера из `known_hosts` (ключ хоста проверяется строго) |
| `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PORT` | адрес, пользователь, порт (по документации сайта порт 39) |
| `DEPLOY_PATH` | `/opt/wan-site/data/desktop/rarus` |

Ключ и значения вводите вы: я их не вижу и не должен.

## Шаг 4. Выпуск

1. Поднять `__version__` в `app/rarus_app/__init__.py` (сейчас `0.1.0`), закоммитить.
2. `git tag v0.1.0 && git push origin v0.1.0` — версия тега обязана совпасть с `__version__`, иначе сборка остановится.
3. Actions соберёт установщик (около 30–40 минут), проверит SHA-256 после передачи и на сервере и
   подменит `RarusAI-Setup.exe` атомарно, раньше `version.txt`; предыдущий кладётся рядом как `.bak`.
4. Вручную без тега: Actions → Rarus Release → Run workflow → `publish`.

## Первая выкладка без GitHub (текущий установщик 0.1.0)

Если хотите положить уже собранный файл не дожидаясь Actions
(`build\RarusSetup-0.1.0.exe`, SHA-256 в `build\RarusSetup-0.1.0.sha256`):

```bash
SHA=$(sha256sum build/RarusSetup-0.1.0.exe | cut -d' ' -f1)
scp build/RarusSetup-0.1.0.exe wan:/opt/wan-site/data/desktop/rarus/RarusAI-Setup.exe.uploading
ssh wan "cd /opt/wan-site/data/desktop/rarus && chmod 644 RarusAI-Setup.exe.uploading \
  && mv -f RarusAI-Setup.exe.uploading RarusAI-Setup.exe && echo $SHA > sha256.txt && echo 0.1.0 > version.txt"
```

## Домен rarus-ai.com: второй процесс на том же сервере, база общая

Слово владельца: «сервер Rarus AI запущен на том же сервере, где был alembox.com, на другом порту, но
регистрационная база у всех одна: alembox.com, sanlykatip.com и rarus-ai.com».

**Как устроено (готово локально, на сервер не выложено).** Тот же код сайта запускается вторым процессом
`uvicorn rarus_main:app` на `127.0.0.1:8120` (основной — `main:app` на 8000). Оба открывают один файл базы
(`WAN_DB_PATH`, SQLite в режиме WAL: так уже работают два воркера основного сайта) и читают один `.env` с
`WAN_JWT_SECRET`. Поэтому один аккаунт, один пароль, одни ключи программы (`sk-ab-…`) и один кошелёк на всех
трёх адресах; зарегистрированный на одном входит на другом, токен одного действителен на другом.
Второй процесс пропускает только нужное Rarus (вход, регистрация, «кто я», тарифы, профиль, Google, сброс
пароля, `/api/agent/`, `/api/desktop/rarus/`); всё остальное — 404, даже для админа. Рестарт Rarus не трогает
alembox.com. Уборщик задач остаётся у основного процесса.

| Файл (в `C:\235235\wan-site`) | Что |
|---|---|
| `backend/rarus_main.py` | второй процесс: тот же `main.app` за списком путей; сам подключает маршруты установщика, если `main.py` на сервере их ещё не знает |
| `backend/tests/test_rarus_main.py` | 6 тестов: общая база в обе стороны, токен и ключ действуют на обоих адресах, остальное 404 |
| `deploy/restart_rarus_backend.py` | запуск и перезапуск на порту 8120 как у основного (`.env` читается буквально); отказывается, если порт занят чужим процессом |
| `deploy/rarus-backend.service` | юнит systemd (ставит владелец с sudo), потолок памяти 2 ГБ |
| `deploy/nginx-rarus-ai.conf` | блоки nginx для `rarus-ai.com` и `www`: страница та же, `/api/` на 8120, редирект с 80 |
| `backend/password_reset.py` | адрес в письме о сбросе пароля — из `WAN_PUBLIC_URL` (у Rarus-процесса `https://rarus-ai.com`) |
| `backend/rarus_dl.py` | адрес скачивания в описи — из `WAN_RARUS_DOWNLOAD_URL` (у Rarus-процесса `rarus-ai.com`) |

Проверено: процесс запущен через uvicorn на локальном порту (health 200, закрытые пути 404, регистрация
работает); 14 тестов нового и 93 теста затронутых модулей на чистой копии последнего коммита сайта.

**Что нужно сделать вам (я не могу): шесть шагов**

1. **DNS у регистратора:** A-записи `rarus-ai.com` и `www.rarus-ai.com` на `95.85.125.50`.
2. **Верхний nginx (`192.168.2.10`)**, который владеет 80/443 этого адреса (там же `garagumgames.com`): отдавать
   нам имя `rarus-ai.com` — порт 443 сквозным проксированием по SNI, порт 80 по `Host`. Требования — в
   `wan-site/deploy/nginx-upstream-proxy.conf`. Без этого снаружи до VDS ничего не дойдёт.
3. **Сертификат на VDS** для `rarus-ai.com` и `www` тем же способом, что для alembox.com (DNS-проверка вручную,
   `deploy/acme-hook.sh`, см. `VDS.md`).
4. **Бэкап базы** перед первым запуском: `sqlite3 /opt/wan-site/data/wan.sqlite3 ".backup '/opt/backups/wan-<дата>.sqlite3'"`
   (второй процесс при старте выполняет те же идемпотентные миграции, что и основной).
5. **Положить файлы** (`scp` + `mv`, не `cp`): `backend/rarus_main.py`, `rarus_dl.py`, `password_reset.py`,
   `deploy/restart_rarus_backend.py`, `deploy/rarus-backend.service`, `deploy/nginx-rarus-ai.conf`; запустить
   `/opt/wan-site/venv/bin/python /opt/wan-site/app/deploy/restart_rarus_backend.py` (печатает, поднялся ли
   процесс и под systemd ли он). Основной сайт перезапускать не нужно. Затем с sudo: nginx-блок в
   `sites-enabled`, `nginx -t`, `systemctl reload nginx`, по желанию юнит (команда напечатана скриптом).
6. **Проверка снаружи:** `https://rarus-ai.com/api/health` отвечает `ok`; `/api/admin/users` — 404;
   регистрация на rarus-ai.com и вход тем же паролем на alembox.com работают.

**Что программа Rarus делает сейчас:** она по-прежнему входит через `alembox.com` и спрашивает обновления на
`sanlykatip.com` (клиент уже доверяет и `rarus-ai.com`). Переключить на новый домен — три строки:
`DEFAULT_SITE` в `engine/raven/providers/rarus_session.py`, `DEFAULT_MANIFEST_URL` в
`app/rarus_app/updater.py` и список в тестах; делать это стоит, когда пункт 6 пройден, иначе установщик будет
ходить на адрес, который ещё не отвечает. Google-вход на `rarus-ai.com` потребует добавить этот адрес в
разрешённые источники клиента Google (у вас в консоли Google).

**Что стоит знать про общую базу:** кэши и счётчики в памяти (например, ограничения частоты) у каждого процесса
свои; запись в базу идёт через SQLite с ожиданием до 30 секунд, как у двух воркеров основного сайта.
Незакоммиченные правки соседней сессии в `wan-site/backend/agent_api.py` (колонка `effort` без миграции)
роняют 16 тестов `test_agent_api.py` в рабочем дереве — на чистой копии коммита и с моими правками все проходят.

## Что не сделано

* **Кнопки «Проверить и установить обновление» в самой программе нет.** Клиент обновлений есть и
  проверяет сайт (`controller.update_check`), но страница его не вызывает, а установка скачанного файла
  (`/UPDATE`) не подключена. Пока новую версию человек ставит, скачав её из меню «Скачать».
* Workflow не запускались на GitHub (у меня нет доступа): первый прогон может найти поправки, например в версиях действий.
* Установщик без подписи: SmartScreen покажет предупреждение.
* Сервер и GitHub не менялись: ничего не отправлено и не выложено.
