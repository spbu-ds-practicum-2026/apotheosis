-- Recommender DB: история взаимодействий, обработанные event_id,
-- индекс пинов по тегам, веса интересов.

-- Дедупликация: событие применяется, только если его event_id вставлен сюда
-- в той же транзакции, что и изменение истории и весов.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Пины, о которых знает Recommender (из событий pin_created), и индекс по тегам.
CREATE TABLE IF NOT EXISTS pins (
    pin_id     TEXT PRIMARY KEY,
    author_id  TEXT NOT NULL,
    tags       TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS pins_tags_idx ON pins USING GIN (tags);

-- История взаимодействий (лайки и просмотры).
CREATE TABLE IF NOT EXISTS interactions (
    event_id    TEXT PRIMARY KEY REFERENCES processed_events (event_id),
    user_id     TEXT NOT NULL,
    pin_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('like', 'view')),
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS interactions_user_idx ON interactions (user_id, occurred_at);

-- Какие пины пользователь уже лайкнул: второй лайк того же пина вес не удваивает.
CREATE TABLE IF NOT EXISTS user_likes (
    user_id TEXT NOT NULL,
    pin_id  TEXT NOT NULL,
    PRIMARY KEY (user_id, pin_id)
);

-- Веса интересов пользователя по тегам.
CREATE TABLE IF NOT EXISTS interests (
    user_id TEXT NOT NULL,
    tag     TEXT NOT NULL,
    weight  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, tag)
);

-- Счётчики пина для популярности и холодного старта пина.
CREATE TABLE IF NOT EXISTS pin_stats (
    pin_id TEXT PRIMARY KEY,
    likes  INTEGER NOT NULL DEFAULT 0,
    views  INTEGER NOT NULL DEFAULT 0
);
