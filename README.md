# Лента картинок (apotheosis)

Учебный прототип распределённой платформы с бесконечной персональной лентой
картинок: шардированная Feed DB, рекомендации по тегам, события через брокер с
дедупликацией. Подробности — в `docs/tr.md` и `docs/api.md`.

## Запуск

```bash
docker compose up -d --build            # весь стенд
pip install -r services/desktop/requirements.txt
python services/desktop/app.py          # клиент, ходит в http://localhost:8000
```

Без Keycloak (любой токен `stub:<логин>`): `AUTH_MODE=stub docker compose up -d --build`.

| Сервис | Порт | Что делает |
|---|---|---|
| api-gateway | 8000 | вход клиента, авторизация (Keycloak), валидация |
| feed | 8001 | лента, пины, лайки и просмотры, outbox → брокер |
| recommender | 8002 | ранжирование, холодный старт, потребитель событий |
| keycloak | 8080 | сервис входа (`admin` / `admin`) |
| minio | 9000, 9001 | файлы пинов (`minioadmin` / `minioadmin`) |
| n1a..n3b | — | Feed DB: 3 узла хеш-кольца, у каждого мастер и синхронная реплика (Patroni) |
| etcd1..3 | — | лидеры кластеров Patroni и состав кольца Feed |
| recommender-db | 5440 | Recommender DB |
| redis | 6379 | кэш порций ленты и брокер (Redis Stream `events`) |

Стенд на двух ноутбуках (основной для демонстрации): `services/deploy/cluster/README.md`.
План тестирования и сценарии демонстрации: `docs/testplan.md`.

С `SEED_DEMO=1` при первом старте Feed загружает 24 демо-пина с картинками и лайки демо-пользователей,
поэтому у нового пользователя сразу есть популярная выдача.

## Структура
- `services/` — код сервисов: `api-gateway`, `feed` (HTTP-сервис в `feed/server/`),
  `recommender`, `desktop`; `deploy/` — только узлы БД и Redis для тестов Feed
- `docs/` — ТР (техрешение), архитектура, API
- `.github/` — шаблоны задач и PR

## Как работать
1. Создайте ветку от `main`.
2. Пишите код в `services/<service>/`.
3. Если меняете архитектуру — обновляйте `docs/tr.md`.
4. Делайте PR в `main` — используйте шаблон PR.

## Требования к PR (минимум)
- Понятное описание
- Обновлённые docs при архитектурных изменениях
- Запускается локально (опишите в `services/<service>/README.md`)
