"""S3 SDK wiring; the batch algorithm lives in batching.py."""

import boto3
from botocore.config import Config


def s3_client(settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        region_name=settings.region,
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )
