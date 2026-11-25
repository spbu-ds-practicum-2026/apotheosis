FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# весь сервис: server/ (HTTP API) использует feed/, app/, feed_service.py и др.
COPY . .
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8001"]
