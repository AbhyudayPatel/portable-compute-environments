FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.10,<2" requests==2.32.3 "uvicorn[standard]==0.32.1" starlette
COPY app/ ./app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
