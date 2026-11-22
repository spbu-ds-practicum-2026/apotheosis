import os


class Settings:
    initial_shards: list[str] = [
        s.strip() for s in os.getenv("INITIAL_SHARDS", "S1,S2,S3").split(",") if s.strip()
    ]
    virtual_nodes: int = int(os.getenv("VIRTUAL_NODES", "64"))


settings = Settings()
