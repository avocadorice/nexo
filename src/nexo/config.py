"""Small environment boundary shared by Python batch and simulator commands."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    bucket: str
    endpoint: str | None
    region: str
    partitions: int
    batch_max_rows: int
    schedule_max_batches: int

    @classmethod
    def from_env(cls):
        settings = cls(
            os.environ["DATABASE_URL"],
            os.getenv("S3_BUCKET", "nexo-files"),
            os.getenv("S3_ENDPOINT_URL"),
            os.getenv("AWS_REGION", "us-east-1"),
            int(os.getenv("PARTITION_COUNT", "4")),
            int(os.getenv("BATCH_MAX_ROWS", "1000")),
            int(os.getenv("SCHEDULE_MAX_BATCHES", "200")),
        )
        if not 1 <= settings.partitions <= 256:
            raise ValueError("PARTITION_COUNT must be 1..256")
        if not 1 <= settings.batch_max_rows <= 10000:
            raise ValueError("BATCH_MAX_ROWS must be 1..10000")
        if not 1 <= settings.schedule_max_batches <= 10000:
            raise ValueError("SCHEDULE_MAX_BATCHES must be 1..10000")
        return settings
