"""Сервис Feed: HTTP API для API Gateway поверх Feed DB, MinIO, Redis и Recommender.

Собирает в одно приложение модули, которые уже есть в services/feed:
- feed/            — хеш-кольцо, get_conn, профили, ensure_profile;
- app/storage.py   — MinIO (загрузка и presigned URL);
- feed_service.py  — порция ленты, circuit breaker, запасной путь;
- recommender_client.py — HTTP-клиент Recommender.
Эти модули не меняются, здесь только код, который их связывает.
"""
