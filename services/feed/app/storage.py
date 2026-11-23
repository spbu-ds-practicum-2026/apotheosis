import time
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import settings


class ObjectStorage:
    def __init__(self) -> None:
        s3_cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        self.internal = boto3.client(
            "s3",
            endpoint_url=settings.minio_internal_endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name="us-east-1",
            config=s3_cfg,
        )
        self.public = boto3.client(
            "s3",
            endpoint_url=settings.minio_public_endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name="us-east-1",
            config=s3_cfg,
        )
        self.bucket = settings.minio_bucket

    def ensure_bucket(self, retries: int = 15, delay: float = 1.5) -> None:
        for attempt in range(retries):
            try:
                self.internal.head_bucket(Bucket=self.bucket)
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchBucket"):
                    self.internal.create_bucket(Bucket=self.bucket)
                    return
                if attempt == retries - 1:
                    raise
            except Exception:
                if attempt == retries - 1:
                    raise
            time.sleep(delay)

    def upload_file(self, object_key: str, data: bytes, content_type: str) -> None:
        self.internal.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )

    def get_presigned_url(self, object_key: str) -> str:
        return self.public.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": object_key},
            ExpiresIn=settings.presigned_ttl_seconds,
        )


storage = ObjectStorage()
