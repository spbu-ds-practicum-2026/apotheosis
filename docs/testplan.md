# План тестирования «Ленты картинок»

## 1. Цель и объём

Показать, что система выполняет DoD из ТР и что заявленные решения задач распределённых систем работают:
доставка событий at-least-once, идемпотентность и дедупликация, обнаружение отказов и деградация,
single-master репликация с переключением на реплику (Patroni), consistent hashing с переносом ключей
при смене состава узлов, eventual consistency между Feed и Recommender.

Каждый сценарий проверяется вручную на стенде (демонстрация) и, где возможно, автоматическим тестом.

## 2. Окружение

| Режим | Как запустить |
|---|---|
| Одна машина | `AUTH_MODE=stub docker compose up -d --build` из корня репозитория |
| Две машины (основной для демо) | `services/deploy/cluster`: на ноутбуке 2 `docker compose -f pc2.yml --env-file cluster.env up -d --build`, на ноутбуке 1 то же с `pc1.yml` |
| Клиент | `API_URL=http://<IP>:8000 python services/desktop/app.py` |

Обозначения в командах: `DC` — `docker compose` (на двух машинах — `docker compose -f pc1.yml --env-file cluster.env` или `pc2.yml`);
`API=http://<IP>:8000`; `H='Authorization: Bearer stub:<логин>'` (режим `AUTH_MODE=stub`).

Полезные команды:
- состояние кластеров Patroni: `DC exec n1a patronictl -c /etc/patroni.yml list` (аналогично для feed-n2, feed-n3);
- топология глазами Feed: `curl -s http://<IP>:8001/health | python3 -m json.tool` (поле `topology`);
- где лежат данные пользователя: `DC exec feed python -m server.demo_tools where <логин>`;
- сходимость Feed и Recommender: `DC exec feed python -m server.demo_tools consistency`;
- счётчики потребителя событий: `curl -s http://<IP ноутбука 1>:8002/internal/consumer`.

Автотесты:
- `services/feed`: `pytest` (юнит) и `FEED_E2E=1 ... pytest` на настоящем PostgreSQL (см. `services/feed/server/README.md`), в т. ч. `server/tests/test_failover_migrate.py`;
- `services/recommender`: `REC_DB_DSN=... pytest`.

## 3. Сценарии

### T1. Регистрация и вход — DoD 1
- **Шаги:** в клиенте зарегистрировать нового пользователя и войти; выйти и войти снова.
- **Ожидаемый результат:** вход успешен, в шапке клиента виден логин; запрос без токена (`curl $API/api/v1/feed`) получает 401.
- **Авто:** `services/feed/tests/e2e` (регистрация, занятый логин, неверный пароль).

### T2. Загрузка пина — DoD 2
- **Шаги:** «Загрузить пин», выбрать картинку, заполнить название и теги; открыть свой профиль.
- **Ожидаемый результат:** пин создан (201), картинка открывается по presigned URL, пин есть в «публикациях» профиля. Файл не той природы (не изображение) отклоняется с понятной ошибкой.
- **Авто:** `server/tests/test_repository.py::test_create_pin_all_on_author_shard`.

### T3. Бесконечная лента — DoD 3
- **Шаги:** листать ленту колесом/стрелками 3+ порции.
- **Ожидаемый результат:** порции подгружаются сами, без повторов пинов в одной прокрутке, `has_more = true`.

### T4. Новый пользователь видит популярное — DoD 4
- **Шаги:** войти новым логином, открыть ленту.
- **Ожидаемый результат:** первые пины — с наибольшим числом лайков (популярное); `degraded = false`.
- **Авто:** проверка в `services/recommender/tests`.

### T5. Лайк влияет на ленту — DoD 5
- **Шаги:** лайкнуть 2–3 пина с одним тегом (например, «горы»), нажать «Обновить».
- **Ожидаемый результат:** вверху ленты пины с этим тегом; `curl http://<IP1>:8002/users/<логин>/interests` показывает вес тега ≥ 1,0.
- **Авто:** `services/recommender/tests/test_pg_store.py::test_load_into_store_matches_in_memory_apply`.

### T6. Чужой профиль — DoD 6
- **Шаги:** нажать на `@автора` под пином.
- **Ожидаемый результат:** открыт профиль автора с его публикациями (постранично).
- **Авто:** `tests/e2e/test_e2e.py::test_publications_are_paged_newest_first_without_gaps_or_repeats`.

### T7. Идемпотентность лайка — защита от повторной обработки
- **Проблема:** клиент не получил ответ (обрыв сети) и повторил запрос.
- **Шаги:**
  ```bash
  EV=$(python3 -c 'import uuid;print(uuid.uuid4())')
  curl -s -X POST $API/api/v1/pins/<pinId>/like -H "$H" -H 'Content-Type: application/json' -d "{\"event_id\":\"$EV\",\"author_id\":\"<автор>\"}"
  # тот же запрос ещё раз
  ```
- **Ожидаемый результат:** второй ответ `duplicate: true`, `likes_count` не вырос; в outbox одно событие.
- **Гарантия:** эффект применяется ровно один раз (event_id — первичный ключ лайка, лайк и outbox — одна транзакция).
- **Авто:** `server/tests/test_repository.py::test_like_idempotent_and_once_per_user`.

### T8. Идемпотентность создания пина
- **Шаги:** два одновременных запроса создания пина с одним event_id.
- **Ожидаемый результат:** создан один пин, оба ответа содержат один pinId (или второй — 409 «обрабатывается»).
- **Авто:** `server/tests/test_repository.py::test_create_pin_parallel_same_event_one_pin`.

### T9. Повторная доставка события — at-least-once + дедупликация
- **Проблема:** брокер или отправитель outbox доставили событие дважды; потребитель упал между коммитом и ack.
- **Шаги:**
  1. `DC stop recommender`; поставить несколько лайков (события копятся в Redis Stream);
  2. `DC start recommender` — Recommender обрабатывает накопленное;
  3. `DC exec feed python -m server.demo_tools replay 5` — отправить последние 5 событий повторно.
- **Ожидаемый результат:** после шага 2 `applied` вырос на число лайков; после шага 3 вырос `duplicates` на 5, веса интересов не изменились.
- **Гарантия:** каждое событие доставляется хотя бы раз и применяется ровно один раз.
- **Авто:** `services/recommender/tests/test_pg_store.py::test_duplicate_event_not_applied_twice`.

### T10. Отказ Recommender — обнаружение отказа и деградация
- **Шаги:** `DC stop recommender`; листать ленту; через минуту `DC start recommender`.
- **Ожидаемый результат:** лента продолжает отдаваться (`degraded: true`, популярное); после серии ошибок circuit breaker открыт (`/health` Feed: `recommender_breaker: open`) и запросы не ждут таймаута 500 мс; после старта персонализация возвращается.
- **Авто:** `services/feed/tests/test_feed_service.py`.

### T11. Отказ мастера узла Feed DB — single-master репликация с failover
- **Проблема:** мастер узла кольца недоступен.
- **Шаги:**
  1. `DC exec n1a patronictl -c /etc/patroni.yml list` — узнать мастера кластера feed-n1 (например, n1a);
  2. в клиенте открыть ленту; запустить постановку лайков;
  3. `DC stop n1a` (контейнер мастера);
  4. сразу лайкнуть пин автора с узла N1 (`demo_tools where <логин>`).
- **Ожидаемый результат:** через ~15–20 с Patroni повышает n1b (`patronictl list` показывает Leader n1b), Feed перечитывает лидера из etcd; лайк, отправленный во время переключения, проходит (запрос дольше обычного), повтор с тем же event_id — дубль; все лайки, подтверждённые до отказа, на месте.
  После `DC start n1a` он возвращается репликой (pg_rewind).
- **Гарантия:** подтверждённая запись не теряется (синхронная реплика), двух мастеров одновременно нет (leader lock в etcd).
- **Авто:** `server/tests/test_failover_migrate.py::test_master_failure_like_survives_and_nothing_acknowledged_is_lost` (настоящая синхронная репликация и pg_promote, etcd — заглушка).

### T12. Отказ физической машины
- **Шаги:** на работающем стенде выключить ноутбук 2 (или `docker compose -f pc2.yml --env-file cluster.env stop`).
- **Ожидаемый результат:** etcd сохраняет кворум (2 из 3 на ноутбуке 1); для узлов, чьи мастера были на ноутбуке 2, Patroni повышает реплики на ноутбуке 1; клиент на ноутбуке 1 продолжает листать и лайкать.

### T13. Добавление и удаление узла кольца — consistent hashing
- **Шаги:**
  1. поднять кластер N4: на ноутбуке 2 `... --profile n4 up -d n4a`, на ноутбуке 1 `... --profile n4 up -d n4b`;
  2. в клиенте непрерывно листать и лайкать;
  3. `DC exec feed python -m server.migrate add N4`;
  4. `DC exec feed python -m server.migrate status`;
  5. `DC exec feed python -m server.migrate remove N4`.
- **Ожидаемый результат:** утилита печатает маршруты переноса только вида `Nx -> N4` (около 1/4 ключей), после переноса все пользователи на своих узлах нового кольца; лайки во время переноса проходят; после `remove` маршруты только `N4 -> Nx`.
- **Гарантия:** переезжают только ключи с дуг добавленного/удалённого узла; данные не теряются и не дублируются.
- **Авто:** `server/tests/test_failover_migrate.py::test_add_and_remove_node_online_while_likes_flow`, `tests/e2e/test_e2e.py::test_rebalance_moves_only_changed_keys_and_everything_stays_reachable`.

### T14. Eventual consistency Feed ↔ Recommender
- **Шаги:** `DC stop recommender`; поставить 5 лайков; `DC exec feed python -m server.demo_tools consistency` — «ещё не сошлось»; `DC start recommender`; через несколько секунд повторить.
- **Ожидаемый результат:** после прекращения операций число лайков в Feed DB и в Recommender DB совпадает, outbox пуст.

### T15. Кэш порций ленты
- **Шаги:** открыть ленту; лайкнуть пин; «Обновить».
- **Ожидаемый результат:** после применения лайка Recommender удаляет кэш порций пользователя (`redis-cli KEYS 'feedsvc:portion:<логин>:*'` пусто), следующая порция строится с новыми весами.

## 4. Матрица покрытия

| Что проверяем | Сценарии |
|---|---|
| DoD: вход, пин, лента, популярное, лайк, профиль | T1–T6 |
| Повторная отправка, at-least-once | T9, T14 |
| Идемпотентность и дедупликация | T7, T8, T9 |
| Обнаружение отказа и реакция | T10, T11 |
| Single-master репликация с failover | T11, T12 |
| Consistent hashing с переносом данных | T13 |
| Eventual consistency | T14 |
