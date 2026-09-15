"""CoreApp backend API.

A deliberately small FastAPI service that persists tasks in PostgreSQL.
It exists to demonstrate the development environment end to end:

    edit code  ->  uvicorn hot-reloads  ->  browser sees the change
              ->  commit + push from inside the environment
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .db import get_connection

app = FastAPI(title="CoreApp API", version="1.0.0")

# Not strictly required when the frontend proxies /api/* to this service,
# but it keeps direct browser access (http://localhost:8000) painless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=280)


@app.get("/api/health")
def health() -> dict:
    database = "up"
    try:
        conn = get_connection(retries=1, delay=0)
        conn.close()
    except Exception:
        database = "down"
    return {"status": "ok", "service": "coreapp-api", "database": database}


@app.get("/api/tasks")
def list_tasks() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, done, created_at FROM tasks ORDER BY id;")
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title) VALUES (%s) "
                "RETURNING id, title, done, created_at;",
                (payload.title,),
            )
            task = cur.fetchone()
        conn.commit()
        return task
    finally:
        conn.close()


@app.patch("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: int) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET done = NOT done WHERE id = %s "
                "RETURNING id, title, done, created_at;",
                (task_id,),
            )
            task = cur.fetchone()
        conn.commit()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task
    finally:
        conn.close()


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s RETURNING id;", (task_id,))
            deleted = cur.fetchone()
        conn.commit()
        if deleted is None:
            raise HTTPException(status_code=404, detail="Task not found")
    finally:
        conn.close()
