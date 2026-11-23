import os

SYSTEM_CATEGORIES: list[str] = [
    "природа",
    "горы",
    "море",
    "город",
    "архитектура",
    "ночь",
    "закат",
    "осень",
    "снег",
    "космос",
    "кофе",
    "минимализм",
    "спорт",
    "киберпанк",
    "животные",
    "арт",
]


class Settings:
    initial_shards: list[str] = [
        s.strip() for s in os.getenv("INITIAL_SHARDS", "S1,S2,S3").split(",") if s.strip()
    ]
    virtual_nodes: int = int(os.getenv("VIRTUAL_NODES", "64"))
    minio_internal_endpoint: str = os.getenv("MINIO_INTERNAL_ENDPOINT", "http://minio:9000")
    minio_public_endpoint: str = os.getenv("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000")
    minio_access_key: str = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key: str = os.getenv("MINIO_SECRET_KEY", "minioadmin")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "pins")
    presigned_ttl_seconds: int = int(os.getenv("PRESIGNED_TTL_SECONDS", "3600"))


settings = Settings()
