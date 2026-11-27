# Feed: HTTP-сервис (`server/`)

Связывает в один работающий сервис модули, которые уже есть в `services/feed`,
и добавляет лайки, просмотры, создание пина, outbox и отправку событий в брокер.
Код товарищей (`feed/`, `app/`, `feed_service.py`, `recommender_client.py`,
`schemas.py`, `cache.py`) не меняется, он подключается как библиотека.

| Файл | Что делает |
|---|---|
| `main.py` | HTTP API для gateway (`/internal/*`), запуск relay и демо-данных |
| `repository.py` | лайк/просмотр с дедупликацией по `event_id`, создание пина (MinIO + БД), outbox |
| `relay.py` | outbox всех узлов → Redis Stream `events` (at-least-once) |
| `adapters.py` | `FeedService` ↔ Feed DB и Redis; номер порции ↔ курсор Recommender |
| `seed.py` | 24 демо-пина с картинками и лайки демо-пользователей (идемпотентно) |
| `schema_extra.sql` | уникальный индекс «один лайк от пользователя на пин» |
| `topology.py` | состав кольца и мастера узлов из etcd (Patroni), обновление каждые 2 с |
| `failover.py` | повтор записи при отказе мастера: перечитать etcd, backoff, окно 30 с, потом 503 |
| `migrate.py` | добавление/удаление узла кольца без остановки (перенос ключей) |
| `demo_tools.py` | демо: сходимость Feed и Recommender, повторная доставка событий, где лежит пользователь |

## API (порт 8001, заголовок `X-User-Id` ставит gateway)

| Запрос | Ответ |
|---|---|
| `GET /internal/feed?cursor=0&limit=4` | порция ленты (docs/api.md, раздел 4); `cursor` — номер порции из `next_cursor` |
| `POST /internal/pins` (multipart: `title`, `description`, `tags`, `event_id`, `image`) | `201` пин; повтор с тем же `event_id` — `200` и тот же пин; параллельный повтор — `409` |
| `POST /internal/pins/{pinId}/like` `{"event_id", "author_id"}` | `{likes_count, liked_by_me, duplicate, shard, ...}`; `404` нет пина |
| `POST /internal/pins/{pinId}/view` `{"event_id", "author_id"}` | то же без лайка |
| `GET /internal/me`, `GET /internal/users/{id}`, `GET /internal/users/{id}/pins` | профили (через `feed/profiles.py`) |
| `GET /health`, `/internal/shards`, `/internal/categories` | служебные |

Ошибки — JSON `{"detail": ...}`: `400` неверные данные (в т.ч. `event_id` не UUID),
`409` `event_id` уже использован для другого действия, `503` БД недоступна
(повторить с тем же `event_id`).

## Как устроено

**Шардирование.** Ключ всех данных пина — `author_id`: `pins`, `interactions`,
`outbox`, `pin_requests`, `author_pins` автора лежат на одном узле. Поэтому
создание пина и «лайк + событие в outbox» — каждое одна локальная транзакция.
`author_id` приходит от клиента вместе с `pinId`; если его нет или он неверный,
пин ищется обходом узлов.

**Лайк и просмотр.** `INSERT INTO interactions ... ON CONFLICT DO NOTHING` (ключ
`event_id`) и событие в outbox в одной транзакции. Повтор с тем же `event_id`
ничего не вставляет и возвращает текущее состояние. Второй лайк того же пина
тем же пользователем с новым `event_id` тоже ничего не меняет.

**Создание пина** (сценарий ТР): запись `pin_requests` со статусом `in_progress`,
файл в MinIO `pins/<pinId>/<filename>`, затем одной транзакцией `pins`,
`author_pins`, событие `pin_created` в outbox и `status = done`.

**Outbox → брокер.** Фоновый поток раз в 0,5 с забирает неотправленные строки
(`FOR UPDATE SKIP LOCKED`), делает `XADD events` и ставит `sent_at`. Сбой посередине
— строки уйдут снова, Recommender отбросит дубль по `event_id`.

**Лента.** `FeedService` (circuit breaker, таймаут 500 мс, запасной путь из
популярного) с кэшем порций в Redis (`feedsvc:portion:<userId>:...`, 60 с).
Recommender удаляет эти ключи после применения лайка или просмотра. Gateway
передаёт `cursor` целым числом, поэтому номер порции сопоставляется с курсором
Recommender в Redis (`feedsvc:cursor:<userId>:<n>`, 30 мин). Счётчик лайков и
`liked_by_me` читаются из БД при каждой выдаче.

## Тесты

```bash
cd services/feed
FEED_E2E=1 FEED_NODES=N1,N2,N3 \
FEED_DSN_N1=postgresql://feed:feed@localhost:5433/feed \
FEED_DSN_N2=postgresql://feed:feed@localhost:5434/feed \
FEED_DSN_N3=postgresql://feed:feed@localhost:5435/feed \
python -m pytest server/tests
```

## Узлы, реплики и failover

Узел кольца (N1, N2, ...) — кластер Patroni: мастер и синхронная реплика на разных машинах.
Feed не хранит адреса баз в конфигурации: при `FEED_ETCD_HOSTS` он читает из etcd

- `/feed/ring` — состав кольца `{"epoch", "ring", "scopes"}` (создаётся при первом старте из `FEED_NODES`);
- `/service/<scope>/leader` и `/service/<scope>/members/<имя>` — мастер кластера и его `conn_url` (пишет Patroni).

Отказ мастера: запись падает → Feed перечитывает etcd и повторяет с backoff до 30 с
(Patroni переключает за ~15–20 с при ttl 15). Повтор безопасен — все записи идемпотентны по
`event_id`. Без `FEED_ETCD_HOSTS` адреса берутся из `FEED_DSN_<узел>`, как раньше.

Добавление узла: поднять кластер Patroni `feed-n4`, затем `python -m server.migrate add N4`
(копирование → новое кольцо в etcd → пауза → догоняющий перенос и удаление со старых узлов).
Удаление — `remove N4`. Перенос данных — `feed/db/rebalance.py` (ключ — автор).

Тест на настоящем PostgreSQL (синхронная репликация + `pg_promote`, etcd — заглушка):
`server/tests/test_failover_migrate.py`, переменные окружения описаны в его docstring.

## Ограничения

- Redis, Recommender и хранилище картинок не реплицируются (работают на ноутбуке 1).
- При отказе ноутбука 1 etcd теряет кворум (на нём 2 из 3 участников): автоматический failover невозможен.
