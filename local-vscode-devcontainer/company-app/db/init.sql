-- CoreApp database bootstrap.
-- Mounted into the Postgres container at /docker-entrypoint-initdb.d/,
-- so it runs once when the database volume is first created.

CREATE TABLE IF NOT EXISTS tasks (
    id         SERIAL PRIMARY KEY,
    title      TEXT        NOT NULL,
    done       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tasks (title, done) VALUES
    ('Open the development environment', TRUE),
    ('Edit backend/app/main.py and watch hot reload', FALSE),
    ('Commit and push from inside the environment', FALSE)
ON CONFLICT DO NOTHING;
