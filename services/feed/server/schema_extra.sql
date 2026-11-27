-- Дополнения сервиса Feed к схеме Feed DB (feed/db/schema.sql не меняется).
-- Применяются при старте Feed на каждом узле, идемпотентно.

-- Один пользователь лайкает пин один раз: второй лайк с новым event_id
-- не увеличивает счётчик и не создаёт событие в outbox.
CREATE UNIQUE INDEX IF NOT EXISTS interactions_like_once
    ON interactions (pin_id, user_id) WHERE kind = 'like';
