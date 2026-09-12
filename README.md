# FunPay Flipper

Telegram-бот и исследовательские модели для перепродажи цифровых товаров.
Текущий этап — ASSIST: подтверждённый прибыльный маршрут ещё не доказан,
автоматическая оплата без валидатора окончательной суммы блокируется.

## Воспроизводимое окружение

Используйте **CPython 3.13.5** (версия записана в `.python-version`).
Команды выполняются из корня этого репозитория. Системный Python должен быть
этой версии; зависимости из соседнего PlusActivator не используются.

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe run_flipper_checks.py
```

Linux:

```sh
python3.13 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python run_flipper_checks.py
```

`requirements.in` содержит прямые зависимости, `requirements.txt` — точные
версии всех транзитивных зависимостей и хеши разрешённых дистрибутивов.
HTTPX SOCKS и Telegram SOCKS включены. Для обычной установки uv не требуется.
Обновление lock-файла выполняется отдельно и проверяется тестами:

```sh
python -m pip install uv==0.8.22
python -m uv pip compile requirements.in --universal --python-version 3.13 --generate-hashes --output-file requirements.txt --no-emit-index-url
```

Официальный тестовый запуск — `run_flipper_checks.py`: он включает семь
корневых наборов, тесты капитала/выхода и исследовательских моделей, использует
временные базы и блокирует внешние socket-соединения в основном процессе.
Дочерние процессы проверяют только локальную SQLite. Это защита тестового
запуска, а не системная сетевая песочница. Не используйте произвольный
`unittest discover` с рабочими секретами: старые модули создают singleton БД
при импорте.

## Запуск бота

Скопируйте `.env.example` в `.env` и задайте параметры (или передайте их через переменные окружения):

```powershell
# Вариант 1: Через файл .env (рекомендуется)
Copy-Item .env.example .env
# Заполните FLIPPER_BOT_TOKEN, ADMIN_IDS и при необходимости FUNPAY_GOLDEN_KEY

# Вариант 2: Переменные окружения в терминале
$env:FLIPPER_BOT_TOKEN = '<локальный токен Telegram>'
$env:ADMIN_IDS = '<ваш числовой Telegram ID>'
$env:FUNPAY_GOLDEN_KEY = '<локальная сессия FunPay>'
# Необязательно: отдельные маршруты соединения
# $env:FUNPAY_PROXY = 'http://127.0.0.1:8080'
# $env:TELEGRAM_PROXY = 'http://127.0.0.1:8080'
```

Запуск:
- Windows (батник): `run_flipper_bot.bat` (проверяет `.venv`, подхватывает `.env` или переменные окружения).
- Напрямую через Python: `.\.venv\Scripts\python.exe start_flipper_bot.py`

Этот cleanup не меняет торговые формулы, лимиты или правила допуска.
Режим, капитал и разрешение сделки проверяйте по `auto_flipper/PILOT_RUNBOOK.md`;
запуск процесса сам по себе не означает готовность к реальной закупке.

## Что хранится в Git

Код, тесты, текущая документация и исследования. Рабочие базы, их резервные
копии, ZIP-архивы, Python-кеши и старые пакеты передачи со скриншотами остаются
локально и исключены через `.gitignore`. Репозиторий не является резервной
копией рабочего баланса или сессии. Тесты создают собственные базы с нуля.

Начало для продолжения работы: `auto_flipper/GEMINI_FIRST_REAL_TRADE.md`.
Пути в старых документах относятся к первоначальному каталогу; при работе
с этой копией считайте корнем текущий репозиторий.
