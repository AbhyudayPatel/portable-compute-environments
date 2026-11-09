FROM python:3.12-slim
RUN pip install --no-cache-dir "mcp>=1.10,<2"
COPY mcpcli.py /mcpcli.py
ENTRYPOINT ["python", "/mcpcli.py"]
