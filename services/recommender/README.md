# Recommender

Ранжирование (`ranking.py`), хранилище в памяти (`store.py`) и HTTP API (`main.py`)
не менялись. Для работы в общем стенде добавлены:

| Файл | Что делает |
|---|---|
| `server.py` | точка запуска (`uvicorn server:app --port 8002`): `main.app` + Recommender DB + потребитель |
| `db/schema.sql` | Recommender DB: `processed_events`, `pins` (индекс по тегам), `interactions`, `user_likes`, `interests`, `pin_stats` |
| `pg_store.py` | применение события одной транзакцией с дедупликацией по `event_id`; загрузка состояния в память при старте |
| `consumer.py` | чтение Redis Stream `events` группой `recommender`, ack после коммита, сброс кэша ленты пользователя |

Порядок обработки события: `INSERT processed_events ON CONFLICT DO NOTHING` →
только если вставка прошла, история и веса → коммит → то же событие в памяти →
`XACK`. Упал между коммитом и ack — сообщение придёт снова и будет отброшено как
дубль. Брошенные упавшим экземпляром сообщения забираются через `XAUTOCLAIM`.

Формат события из брокера — `docs/api.md`, раздел 5 (`pin_created`, `pin_liked`,
`pin_viewed`); `pg_store.parse_broker_event` переводит его в `schemas.Event`.

Отладка: `GET /users/{id}/interests` — веса интересов, `GET /internal/consumer` —
сколько событий применено, сколько отброшено как дубли.

Тесты: `REC_DB_DSN=postgresql://rec:rec@localhost:5440/rec pytest` (без
`REC_DB_DSN` тесты Recommender DB пропускаются).
