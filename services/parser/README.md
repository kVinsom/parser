# parser

Оновлення 2026-10-05: `keywords_all` повертає перевірку
`grouped_keywords_card_casefold` з логікою `AND_text_OR_sizes_OR_conditions`.
Worker вимагає всі текстові фрази, будь-який вибраний розмір і будь-який
вибраний стан. Значення користувача повністю зберігаються в контракті.

Оновлення 2026-09-26: дані лота й відповіді `/v1/items/details` мають необов’язкове
поле `seller`: ID продавця, ім’я акаунта, `rating` у шкалі 0–5 та `feedback_count`.
Для Vinted дані читаються з того самого HTML/JSON, що й фото. HTML-запис
обов’язково пов’язаний з ID лота; ID продавця не має суперечити іншим його
записам. `feedback_reputation` (0–1) множиться на 5. Відсутні або некоректні
значення залишаються невідомими, а не стають нулем. Нових запитів профілю немає.

Оновлення 2026-09-24: `newest_first` у запиті каталогу та окремий endpoint
`POST /v1/items/details` для повної галереї Vinted. Він перевіряє ID/регіон
і відсікає фото рекомендацій. `published_at` залишається порожнім, якщо
сторінка дає лише округлений вік. За уточненням від 2026-09-25 worker
використовує нові ID після початкового проходу; дата публікації необов'язкова.
Це не гарантує фактичний час публікації продавцем.
[Журнал](../../docs/WORK_LOG.md).

Оновлення 2026-09-23: приймаються кваліфіковані розміри `EU 42`, `UK 8`,
десяткова кома та еквівалентні `2XL`/`XXL`. Система EU/UK зберігається,
конвертації між ними немає; worker зіставляє значення за регіоном та
явними позначеннями джерела. Локально пройшли 167 тестів; живі запити
після оновлення дали підтверджені збіги й повідомлення Telegram.

[Усі сервіси](../README.md) · [Контракт і правила](docs/PARSING.md) · [Архітектура](docs/ARCHITECTURE.md)

Реалізований Python-сервіс: один HTTP-запит worker → один каталог через готовий
gRPC gateway → нормалізовані лоти. Підтримуються **UK/GB, FR, IT, DE**; PL вимкнений.
Підписок, оплати, персонального стану й тарифних квот немає.

Перевірка 2026-09-22: 159 локальних тестів на Windows/Python 3.12.14 і
Linux/Python 3.13.15; 384/384 лоти зі збережених реальних HTML; окремо чотири
живі каталоги по 96 лотів та HTTP-пошук `GB`, `nike`, page=2 — 96 лотів,
`next_page=3`. Це контрольні запуски, а не доказ роботи 24/7 або 60 запитів/хв.
Деталі та відомі прогалини — [VALIDATION.md](docs/VALIDATION.md).

Оптимізація HTML-адаптера: **−14,67% локальної затримки** (17,797 → 15,186 мс),
400 парних порівнянь із однаковими результатами. Окремо 12/12 живих запитів
до і 12/12 після: середній HTTP-час 2,998 → 2,948 с. Спостережувані 1,68%
для live не є доведеним ефектом оптимізації: основний час займає gateway/Vinted.
[Виміри й відтворення](docs/LATENCY.md).

## Встановлення і запуск

Рекомендовано Python 3.13; перевірена сумісність також із 3.12. Залежності
зафіксовані в `requirements.txt` і `requirements-dev.txt`. Із кореня проєкту:

```powershell
cd D:\Programer\Project\vpars
docker compose -f services/access-gateway/bypass/compose.yaml up -d --wait
cd services/parser
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools/generate_proto.py
.\.venv\Scripts\python.exe -m vpars_parser
```

У поточному робочому каталозі `.venv` та `.generated` уже створені, достатньо
останньої команди. Якщо Python встановлений без launcher `py`, замінити
`py -3.13` на шлях до `python.exe`. На Linux: `python3.13 -m venv .venv`,
далі використовувати `.venv/bin/python`.

API слухає `127.0.0.1:8002`; документація — `http://127.0.0.1:8002/docs`.
Для іншої адреси/порту:

```powershell
$env:PARSER_GATEWAY_TARGET = '127.0.0.1:50051'
.\.venv\Scripts\python.exe -m uvicorn vpars_parser.api:create_app --factory --host 127.0.0.1 --port 8002 --workers 1 --limit-concurrency 32
```

gRPC-класи генеруються **з єдиного наявного**
`../access-gateway/bypass/proto/vinted_rpc/access.proto` в `.generated`.
Уточнення 2026-09-24: спільну схему розширено `FetchItem` і `newest_first`;
окремої копії вихідної схеми не створюється. Gateway потрібно перезібрати.
Для runtime-дистрибуції потрібні `vpars_parser/`, `.generated/vinted_rpc/`
і `requirements.txt`; компілятор `grpcio-tools` потрібен лише під час підготовки.

## HTTP API

| Метод і шлях | Результат |
|---|---|
| `POST /v1/catalog/parse` | Одна сторінка, лоти, повнота, пагінація, перевірки worker |
| `POST /v1/items/details` | `source_id`, `region_id`, числовий `source_item_id` → повна галерея й доступні дані публікації |
| `GET /v1/sources/vinted/capabilities` | Перетин підтримки адаптера й поточного ListRegions |
| `GET /health/live` | Стан процесу, без звернення до gateway |
| `GET /health/ready` | Доступність ListRegions; **не** перевірка браузера чи Vinted |

Приклад запиту з PowerShell:

```powershell
$body = @{
    request_id = 'worker-001'
    source_id = 'vinted'
    region_id = 'GB'
    search_text = 'nike'
    page = 2
    timeout_seconds = 60
    filters = @{
        models = @('Air Max 90', 'Air Force 1')
        brands = @('Nike', 'Adidas')
        price = @{ maximum = '100.00'; currency = 'GBP' }
    }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8002/v1/catalog/parse -Method Post -ContentType application/json -Body $body -TimeoutSec 95
```

`search_text` — явно заданий додатковий запит джерела. Він не формується зі станів,
розмірів чи склеєних OR-моделей. Без нього завантажується ширший каталог, а моделі
й ключові слова залишаються worker. Якщо worker додає `search_text`, він має
враховувати, що це окреме обмеження пошуку поряд з іншими параметрами.

Скорочений фрагмент відповіді на реальному знеособленому fixture:

```json
{
  "request_id": "worker-001",
  "source_id": "vinted",
  "region_id": "UK",
  "items": [{
    "source_id": "vinted",
    "region_id": "UK",
    "source_namespace": "vinted:UK",
    "source_item_id": "10001",
    "title": "Stripe sun hat",
    "price": {"amount": "4.00", "currency": "GBP"},
    "item_url": "https://www.vinted.co.uk/items/10001-fixture-item",
    "photo_urls": ["https://images1.vinted.net/t/redacted/10001.webp"],
    "brand": {"raw": "Thomas & Co", "normalized": "thomas & co"},
    "condition": {"raw": "New without tags", "normalized": "new without tags"},
    "size": {"raw": "One size", "normalized": "one size"},
    "colors": null,
    "category": null,
    "availability": "unknown"
  }],
  "pagination": {"requested_page": 1, "has_more": "true", "next_page": 2, "cursor": null},
  "completeness": "complete"
}
```

Це фрагмент fixture-відповіді, не обіцянка конкретного товару за прикладом пошуку.
Повна відповідь також містить UTC `fetched_at`, `source_attributes`, `warnings`,
`applied_filters`, `residual_filters`, `filter_logic`, `timings`, `html_bytes`,
`cards_seen` і `cards_skipped`. Суми на вході й виході — **десяткові рядки**;
JSON-числа для сум відхиляються, обчислення виконуються через Decimal.

`request_id` із тіла має пріоритет над `X-Request-ID`; за відсутності генерується UUID.
Ідентифікатор повертається в тілі й HTTP-заголовку та передається в метаданих gRPC.
Некоректні параметри не містять у відповіді вихідних значень чи тексту винятків.

## Фільтри та обмеження достовірності

Між параметрами **AND**, між альтернативами одного параметра **OR**. Parser
повертає всю розібрану сторінку; остаточно відбирає лоти worker.

| Параметр | Обробка |
|---|---|
| `search_text` | `applied_filters`, передається gateway |
| `models`, `keywords` | `residual_filters`: усі слова однієї альтернативи в назві, OR між альтернативами, без урахування регістру |
| `brands` | residual: точний збіг нормалізованого бренду |
| `conditions` | residual: `new with tags`, `new without tags`, `very good`, `good`, `satisfactory` |
| `sizes` | residual: підтверджений розмір; `M / 38 / 10` → `m`, числові позначення без перерахунку систем |
| `price` | residual: включні межі `minimum`/`maximum`, явна `GBP` для UK або `EUR` для FR/IT/DE |
| `colors`, `categories` | `UnsupportedFilter`: даних у перевірених картках немає |

`values` зберігає альтернативи вводу, `normalized_values` — значення для перевірки.
`operation` та `item_field` описують перевірку. Невідоме значення атрибута означає
`missing_value=not_confirmed`, не збіг. Якщо на всій сторінці немає жодного
придатного значення для активного фільтра — `UnsupportedFilter`; якщо лише в
окремих лотах — `FilterValueUnknown`. Фільтри з невідомим мапінгом розміру
відхиляються до gateway. Німецьке `Neu` не вважається доказом відсутності бірок.

`completeness=complete` означає повноту розбору **однієї отриманої сторінки**.
`partial` позначає пошкоджені/невпізнані картки або суперечливі дублікати. Всі
непридатні картки дають `SchemaChanged`, а не успішні `items=[]`.

`has_more` — рядковий enum `"true"`, `"false"`, `"unknown"`. Наступний номер
береться лише з перевіреного посилання того самого регіону й пошуку. У Vinted v1
немає підтверджених fixtures останньої/порожньої сторінки: відсутня або вимкнена
кнопка не дає `false`, повертається `unknown`. Новизна сортування не гарантується.

## Стабільні помилки

Формат: `{"request_id":"...","error":{"code":"...","message":"...","source_status":null,"fields":[]}}`.

| HTTP | Коди |
|---|---|
| 413 | `RequestTooLarge` — тіло понад 64 КіБ |
| 422 | `InvalidRequest`, `UnsupportedSource`, `UnsupportedRegion`, `UnsupportedFilter` |
| 502 | `AccessBlocked`, `UnexpectedRedirect`, `UnexpectedContent`, `UpstreamError`, `GatewayServiceError`, `GatewayContractError`, `GatewayInvalidArgument`, `GatewayAuthenticationFailed`, `GatewayTransportError`, `ResponseTooLarge`, `SchemaChanged` |
| 503 | `ParserBusy`, `GatewayResourceExhausted`, `GatewayUnavailable`, `GatewayCancelled`, `SourceRateLimited`, `NotReady` |
| 504 | `GatewayTimeout`, `ParserTimeout` |
| 500 | `InternalError` — без внутрішнього тексту помилки |

Gateway `INVALID_ARGUMENT` після локальної валідації означає невідповідність
інтеграції, тому має HTTP 502. `RESOURCE_EXHAUSTED` зберігає свою категорію:
він може означати як зайнятий браузер, так і завелику відповідь. Автоповторів немає.

## Налаштування й ресурси

Змінні беруться з оточення процесу; `.env.example` — довідка, автоматичного читання `.env` немає.

| Змінна | Типово | Межі |
|---|---:|---|
| `PARSER_GATEWAY_TARGET` | `127.0.0.1:50051` | Для спільної Compose-мережі — `gateway:50051` |
| `PARSER_MAX_INFLIGHT` | 4 | 1–4, узгодити з gateway; картки займають максимум половину місць (мінімум одне) |
| `PARSER_BROWSER_TIMEOUT` | 60 | 1–300 с; запит може задати `timeout_seconds` |
| `PARSER_RPC_GRACE` | 15 | 15–60 с додатково до браузерного бюджету |
| `PARSER_CONTROL_TIMEOUT` | 3 | 0,1–30 с на ListRegions |
| `PARSER_PARSE_TIMEOUT` | 10 | 0,1–60 с на обробку HTML |
| `PARSER_MAX_HTML_BYTES` | 33554432 | Не більше 32 МіБ |

Загальний бюджет = control + browser + grace + parse: типово 88 с. HTTP-клієнт
має дозволяти щонайменше 95 с; для браузерного таймауту 300 с — щонайменше 335 с.
Канал gRPC повторно використовується, gzip і ліміт приймання 32 МіБ узгоджені
з gateway. Завершення сервісу очікує активні роботи й закриває канал.

Надлишкові каталоги одразу отримують `ParserBusy`; черга та повтори належать worker.
Скасування/таймаут HTTP не звільняє слот уже розпочатої роботи до її завершення.
Розбір виконується в обмеженому потоці, з утриманням слота; примусове завершення
нативного HTML-парсера всередині потоку не виконується. Перевищення бюджету розбору
стає помилкою, загальний deadline обмежує очікування клієнта. Для примусового
переривання CPU-роботи надалі потрібна ізоляція в процесі.

Ліміти належать одному процесу. Запускати один Uvicorn worker; збільшення кількості
процесів потребує загального узгодження місткості gateway. За замовчуванням HTTP
призначений для локальної/довіреної внутрішньої мережі. Метрики часу, розміру,
карток і результатів є у відповіді та журналі `vpars.parser` рівня INFO; HTML,
секрети, пошукові тексти та винятки туди не записуються.

## Перевірки

Єдина автоматизована перевірка (за замовчуванням без живих запитів):

```powershell
.\run.ps1 --qa
# Ті самі перевірки, потім локальний і живий HTTP-бенчмарк:
.\run.ps1 --qa --mode both --rounds 3
# Лише бенчмарки повних збережених сторінок і живого доступу:
.\run.ps1 --benchmark --mode both --capture-directory ../access-gateway/bypass/.artifacts/grpc-20260915T213415Z-28557d9c
```

QA генерує gRPC-класи, перевіряє код/форматування, запускає всі тести з JUnit,
а після успіху — бенчмарк. Невдалі або пропущені тести зупиняють наступні етапи.
JSON/Markdown-звіти й журнали зберігаються в окремій папці `.artifacts/qa-.../`
або `.artifacts/benchmark-.../`. Немає автоматичного запуску за розкладом.
На Linux еквівалент: `.venv/bin/python tools/qa.py` / `tools/benchmark.py`.

Бенчмарки вимірюють mean/median/p95, окремі етапи, частку повних відповідей,
помилки й невиконані запити. Пороги p95 задаються явно; локальні fixtures,
повні HTML та живий доступ розділені. [Параметри, методика й результати](docs/BENCHMARKS.md).

Окремі перевірки та контрольні запуски також залишаються доступними:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check vpars_parser tools tests
.\.venv\Scripts\python.exe tools/check_captures.py ../access-gateway/bypass/.artifacts/grpc-20260915T213415Z-28557d9c
# Наступні дві команди роблять реальні запити через готовий gateway:
.\.venv\Scripts\python.exe tools/smoke.py
.\.venv\Scripts\python.exe tools/http_smoke.py
```

`smoke.py` виконує рівно по одному запиту на чотири регіони. `http_smoke.py`
тимчасово запускає Uvicorn, перевіряє health, відмову PL і живий GB/nike/page=2,
після чого завершує свій HTTP-процес. Звіти без сирого HTML — у `.artifacts/`.
Походження реальних fixtures й межа синтетичних мутацій описані в
[PROVENANCE.md](tests/fixtures/PROVENANCE.md).

## Що ще залежить від gateway

- **Підтверджений порожній каталог:** gateway зараз повертає `UNEXPECTED_CONTENT`
  без HTML. Parser зберігає явну помилку; `complete + items=[]` недоступне.
- Ціна, стан, розмір, бренд, колір, категорія й сортування не мають полів у
  `FetchCatalog`. Наявні атрибути можна перевірити в worker, але для ефективного
  відбору на стороні сайту потрібне окреме сумісне розширення gateway.
- Колір, категорія, довідники категорійних/вікових розмірів, однозначна семантика
  всіх станів і статус доступності потребують підтверджених даних/зразків.
- Сортування за новизною та достовірний кінець каталогу потребують перевіреного
  контракту. Загальний DTO їх допускає, поточний адаптер не вигадує підтвердження.

Мінімальні запропоновані зміни залежності — [GATEWAY_GAPS.md](docs/GATEWAY_GAPS.md).
Постійний моніторинг, міжсторінкова/персональна дедуплікація, Telegram та UAH
залишаються відповідальністю інших сервісів.
