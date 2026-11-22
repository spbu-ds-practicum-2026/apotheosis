CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY CHECK (user_id ~ '^[A-Za-z0-9_-]{3,32}$'),
    password_hash TEXT,
    display_name  TEXT NOT NULL,
    avatar_url    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx    ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS pin_requests (
    user_id    TEXT NOT NULL,
    event_id   UUID NOT NULL,
    pin_id     UUID NOT NULL,
    status     TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'done')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, event_id)
);

CREATE TABLE IF NOT EXISTS author_pins (
    author_id  TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    pin_id     UUID NOT NULL,
    image_key  TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (author_id, pin_id)
);
CREATE INDEX IF NOT EXISTS author_pins_feed_idx
    ON author_pins (author_id, created_at DESC, pin_id DESC) INCLUDE (image_key, title);

CREATE TABLE IF NOT EXISTS pins (
    pin_id      UUID PRIMARY KEY,
    author_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags        TEXT[] NOT NULL DEFAULT '{}',
    image_key   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pins_recent_idx ON pins (created_at DESC, pin_id DESC);
CREATE INDEX IF NOT EXISTS pins_author_idx ON pins (author_id);
CREATE INDEX IF NOT EXISTS pins_tags_idx   ON pins USING GIN (tags);

CREATE TABLE IF NOT EXISTS interactions (
    event_id   UUID PRIMARY KEY,
    pin_id     UUID NOT NULL REFERENCES pins (pin_id),
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('like', 'view')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS interactions_pin_idx  ON interactions (pin_id, kind);
CREATE INDEX IF NOT EXISTS interactions_user_idx ON interactions (user_id, kind);

CREATE TABLE IF NOT EXISTS outbox (
    event_id   UUID PRIMARY KEY,
    pin_id     UUID NOT NULL,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_unsent_idx ON outbox (created_at, event_id) WHERE sent_at IS NULL;
