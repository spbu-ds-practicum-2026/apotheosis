# Контракты API и форматы данных проекта «Лента картинок»

## 1. Сторонний сервис входа и поле `userId`

- **Сервис аутентификации:** Keycloak (OIDC / JWT), разворачивается в `docker-compose.yml` с автоматическим импортом конфигурации realm `apotheosis` (клиент `apotheosis-desktop`).
- **Поле `userId`:** берётся из claim `preferred_username` в JWT-токене (уникальный неизменяемый логин пользователя).
- **Передача токена:** заголовок `Authorization: Bearer <access_token>`.
- **Режим заглушки (`AUTH_MODE=stub`):** принимает любой непустой токен. Если токен вида `stub:<login>`, извлекается `userId = <login>`, иначе `userId = "demo_user"`.
- **Внутренняя передача в `Feed`:** после проверки токена `API Gateway` добавляет заголовок `X-User-Id: <userId>`.

## 2. Шардирование данных

- **Ключ шардирования пинов:** `userId` автора (`authorId`). Все публикации одного автора лежат на одном шарде.
- **Алгоритм:** согласованное хеширование (`consistent hashing`, SHA-256, 64 виртуальных узла на шард) с поддержкой динамического добавления новых шардов в кольцо и перераспределения только части ключей `userId`.

## 3. Список запросов API (API Gateway, порт `8000`)

### Аутентификация
- `POST /api/v1/auth/register` — регистрация в Keycloak (`{ "username", "password" }` -> `201`, `access_token`, `userId`; при занятом логине `409`).
- `POST /api/v1/auth/login` — вход (`{ "username", "password" }` -> `200`, `access_token`, `userId`; при ошибке `401`).
- `GET /api/v1/auth/me` — проверка токена (`Authorization: Bearer ...` -> `200 { "userId" }` или `401`).

### Лента и взаимодействия (требуют `Authorization`)
- `GET /api/v1/feed?cursor=0&limit=4` — получить порцию ленты.
- `POST /api/v1/pins/{pinId}/like` — лайк пина (`{ "event_id": "uuid", "author_id": "alice" }`).
- `POST /api/v1/pins/{pinId}/view` — просмотр пина (`{ "event_id": "uuid", "author_id": "alice" }`).
- `POST /api/v1/pins` — создание пина (`multipart/form-data`: `title`, `description`, `tags`, `event_id`, `image`; подключение MinIO — этап 24.11).

### Служебные
- `GET /health` — проверка готовности сервиса.

## 4. Формат порции ленты

~~~json
{
  "items": [
    {
      "pinId": "550e8400-e29b-41d4-a716-446655440000",
      "authorId": "alice",
      "title": "Закат в горах",
      "description": "Тёплый свет над вершинами",
      "tags": ["горы", "закат", "природа"],
      "image_url": "data:image/png;base64,...",
      "likes_count": 5,
      "liked_by_me": false,
      "created_at": "2026-11-21T12:00:00Z",
      "shard": "S2"
    }
  ],
  "next_cursor": "4",
  "has_more": true,
  "degraded": false
}
~~~

## 5. Формат события (Outbox -> Message Broker -> Recommender)

~~~json
{
  "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "event_type": "pin_liked",
  "user_id": "bob",
  "pin_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-11-21T12:05:00Z",
  "payload": {
    "author_id": "alice",
    "shard": "S2",
    "tags": ["горы", "закат", "природа"]
  }
}
~~~

Значения `event_type`: `"pin_created"`, `"pin_liked"`, `"pin_viewed"`.

## 6. Структура сервисов

- `services/api-gateway/` — FastAPI шлюз (роутинг, валидация, авторизация через Keycloak или режим `stub`).
- `services/feed/` — каркас сервиса ленты (`main.py`, `config.py`, `models.py`, `routing.py`, `repository.py`).
- `services/desktop/` — десктопное приложение (Python + Tkinter + Pillow + httpx): экраны входа/регистрации и ленты.

## 7. Категории контента (для Recommender)

- Поле `tags` содержит **фиксированные категории системы**, выбираемые автором при загрузке пина (не произвольные пользовательские хештеги).
- В десктопном клиенте категории не отображаются на карточках ленты и используются под капотом рекомендательной системой (`Recommender`).
- Базовый список категорий: `природа`, `горы`, `море`, `город`, `архитектура`, `ночь`, `закат`, `осень`, `снег`, `космос`, `кофе`, `минимализм`, `спорт`, `киберпанк`, `животные`, `арт`.
