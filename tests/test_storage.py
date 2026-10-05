from nexo.config import Settings
from nexo.storage import s3_client


def test_https_put_uses_content_length_for_spaces(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    settings = Settings(
        "unused", "test-bucket", "https://nyc3.digitaloceanspaces.com", "nyc3", 4, 1000, 200
    )
    client = s3_client(settings)
    captured = []

    class Inspected(Exception):
        pass

    def inspect(request, **kwargs):
        captured.append(request)
        raise Inspected

    client.meta.events.register("before-send.s3.PutObject", inspect)
    try:
        client.put_object(Bucket="test-bucket", Key="file.csv", Body=b"immutable file bytes")
    except Inspected:
        pass
    assert len(captured) == 1
    request = captured[0]
    assert int(request.headers["Content-Length"]) == len(b"immutable file bytes")
    assert "chunked" not in str(request.headers.get("Content-Encoding", ""))
    assert "Transfer-Encoding" not in request.headers
