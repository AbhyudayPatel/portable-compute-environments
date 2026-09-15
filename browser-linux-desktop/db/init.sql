-- CoreApp database bootstrap (Linux desktop environment).
-- Kept outside the Git repo on purpose: the database container must be able
-- to initialise BEFORE the repo exists in the workspace volume. The repo
-- carries its own copy (db/init.sql) so the application remains
-- self-describing for anyone who runs it elsewhere.

CREATE TABLE IF NOT EXISTS tasks (
    id         SERIAL PRIMARY KEY,
    title      TEXT        NOT NULL,
    done       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tasks (title, done) VALUES
    ('Open the Linux desktop at http://localhost:8080', TRUE),
    ('Open VS Code from the desktop, edit backend/app/main.py, watch hot reload', FALSE),
    ('Open Chromium inside the desktop and visit http://frontend:3000', FALSE),
    ('Commit, push, reset the environment, watch pushed work come back', FALSE)
ON CONFLICT DO NOTHING;
