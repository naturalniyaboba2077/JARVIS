"""Downloader checks use fake HTTP responses and a disposable model directory."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download_model as download


class Response:
    def __init__(self, *, status=200, data=None, body=b"", headers=None, url="https://example.invalid/signed"):
        self.status_code, self.data, self.body = status, data, body
        self.headers, self.url = headers or {}, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise download.requests.HTTPError("fake HTTP error")

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i+size]


class DownloaderTests(unittest.TestCase):
    def test_publish_resume_and_refresh(self):
        payload = b"fake GGUF test bytes" * 400_000  # Two 4 MiB ranges.
        sha = hashlib.sha256(payload).hexdigest()
        calls = []

        class Session:
            expired = False

            def get(self, url, *, headers, **kwargs):
                calls.append(headers["Range"])
                if not self.expired:
                    self.expired = True
                    return Response(status=403)
                start, end = map(int, headers["Range"].split("=")[1].split("-"))
                return Response(status=206, body=payload[start:end+1],
                                headers={"Content-Range": f"bytes {start}-{end}/{len(payload)}"})

        with tempfile.TemporaryDirectory(prefix="jarvis-download-test-") as directory, contextlib.ExitStack() as ctx:
            ctx.enter_context(patch.object(download.os, "environ", {"USERPROFILE": directory}))
            ctx.enter_context(patch("sys.argv", ["download_model.py", "--repo", "test/model",
                                                 "--file", "model.gguf", "--workers", "1"]))
            ctx.enter_context(patch.object(download.requests, "get", return_value=Response(data=[
                {"path": "model.gguf", "size": len(payload), "lfs": {"oid": sha}}])))
            head = ctx.enter_context(patch.object(download.requests, "head", return_value=Response()))
            ctx.enter_context(patch.object(download.requests, "Session", Session))
            output = ctx.enter_context(contextlib.redirect_stdout(io.StringIO()))
            download.main()
            target = Path(directory) / ".lmstudio/models/test/model/model.gguf"
            self.assertEqual(download.digest(target), sha)
            self.assertEqual(head.call_count, 2)  # Initial URL plus rejected URL refresh.
            self.assertEqual(len(calls), 3)  # One rejected and two successful ranges.
            target.unlink()  # Only our generated temporary fixture; keep verified parts.
            calls.clear()
            download.main()
            self.assertEqual(calls, [])  # All verified chunks were reused.
            self.assertEqual(download.digest(target), sha)
            download.main()  # Already published file validates without downloading.
            self.assertIn("VERIFIED existing", output.getvalue())

    def test_rejects_weight_over_limit(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-download-test-") as directory, contextlib.ExitStack() as ctx:
            ctx.enter_context(patch.object(download.os, "environ", {"USERPROFILE": directory}))
            ctx.enter_context(patch("sys.argv", ["download_model.py", "--repo", "test/model", "--file", "model.gguf"]))
            ctx.enter_context(patch.object(download.requests, "get", return_value=Response(data=[
                {"path": "model.gguf", "size": 4_000_000_001, "lfs": {"oid": "0" * 64}}])))
            with self.assertRaisesRegex(RuntimeError, "4 GB limit"):
                download.main()
            self.assertFalse((Path(directory) / ".lmstudio/models/test/model/model.gguf").exists())


if __name__ == "__main__":
    unittest.main()
