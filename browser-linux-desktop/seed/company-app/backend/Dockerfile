# CoreApp backend image.
# Source is COPYed for production-style runs, but during development the
# compose stack bind-mounts ./backend over /app so uvicorn --reload picks
# up edits immediately.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
