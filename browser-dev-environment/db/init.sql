-- CoreApp database bootstrap (browser environment).
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
    ('Open the browser IDE at http://localhost:8080', TRUE),
    ('Edit backend/app/main.py and watch hot reload', FALSE),
    ('Commit and push, then run scripts/reset.ps1 and watch your work come back', FALSE)
ON CONFLICT DO NOTHING;
