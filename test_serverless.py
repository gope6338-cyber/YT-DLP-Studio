import io
import os
import sys
import tempfile
import unittest
from unittest import mock

# Shared modules live alongside the Vercel function (api/_shared)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api", "_shared"))

# Hermetic env before importing the serverless backend
_TEST_ROOT = tempfile.mkdtemp(prefix="yts_serverless_test_")
os.environ.update({
    "DEFAULT_SAVE_DIR": os.path.join(_TEST_ROOT, "downloads"),
    "APP_CACHE_DIR": os.path.join(_TEST_ROOT, "cache"),
    "APP_TEMP_DIR": os.path.join(_TEST_ROOT, "tmp"),
})
os.environ.pop("USE_HF_INFERENCE_API", None)
os.environ.pop("HF_TOKEN", None)

import serverless_backend as sb


def fake_info(formats, title="Test Video", duration=120.0):
    return {
        "title": title,
        "duration": duration,
        "thumbnail": "https://example.com/t.jpg",
        "formats": formats,
        "filesize": 1234567,
        "uploader": "Chan",
        "upload_date": "20240102",
        "height": 720,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "fps": 30,
    }


PROGRESSIVE_720 = [{
    "format_id": "22", "vcodec": "avc1.64001f", "acodec": "mp4a.40.2",
    "height": 720, "ext": "mp4", "protocol": "https",
    "url": "https://media.example/progressive.mp4", "filesize": 10_000_000,
}]

DASH_SET = [
    {
        "format_id": "137", "vcodec": "avc1", "acodec": "none",
        "height": 1080, "ext": "mp4", "protocol": "https",
        "url": "https://media.example/video only.mp4".replace(" ", "-"),
        "filesize": 50_000_000,
    },
    {
        "format_id": "140", "vcodec": "none", "acodec": "mp4a",
        "height": None, "ext": "m4a", "protocol": "https",
        "url": "https://media.example/audio.m4a", "filesize": 3_000_000,
    },
    {
        "format_id": "18", "vcodec": "avc1", "acodec": "mp4a",
        "height": 360, "ext": "mp4", "protocol": "https",
        "url": "https://media/example/progressive-360.mp4", "filesize": 8_000_000,
    },
]


class FakeUpstream:
    def __init__(self, data, headers=None, status=200):
        self._buf = io.BytesIO(data)
        self.headers = headers or {}
        self.status = status

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self._buf.close()


class TestServerlessUI(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_index_injects_serverless_boot_config(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["Content-Type"])
        self.assertNotIn(b"__APP_BOOT_CONFIG__", r.data)
        self.assertIn(b'"serverless": true', r.data)

    def test_static_assets_served(self):
        for path in ("/app.js", "/style.css"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["serverless"])
        self.assertTrue(data["ffmpeg"])
        self.assertFalse(data["ai_enabled"])


class TestServerlessOnlyEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_queue_endpoints_return_501(self):
        cases = [
            ("POST", "/api/enqueue", {}),
            ("POST", "/api/cancel", {"task_id": "x"}),
            ("POST", "/api/remove", {"task_id": "x"}),
            ("GET", "/api/queue", None),
            ("GET", "/api/stream", None),
        ]
        for method, path, body in cases:
            r = self.client.open(path, method=method, json=body)
            self.assertEqual(r.status_code, 501, path)
            self.assertTrue(r.get_json()["serverless"])

    def test_validation_errors(self):
        self.assertEqual(self.client.post("/api/info", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/resolve", json={}).status_code, 400)
        self.assertEqual(self.client.get("/api/download").status_code, 400)
        self.assertEqual(self.client.post("/api/ai/analyze", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/ai/search", json={}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/resolve", json={"url": "https://x", "format": "!!"}).status_code, 400)

    def test_clip_range_validation(self):
        base = "/api/clip?url=https://youtu.be/x"
        self.assertEqual(self.client.get(base).status_code, 400)               # missing range
        self.assertEqual(self.client.get(base + "&start=10&end=5").status_code, 400)
        self.assertEqual(self.client.get(base + "&start=0&end=0").status_code, 400)
        with mock.patch.object(sb, "MAX_CLIP_SECONDS", 10):
            r = self.client.get(base + "&start=0&end=60")
        self.assertEqual(r.status_code, 400)
        self.assertIn("limited to", r.get_json()["error"])


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_resolve_progressive_stream(self):
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(PROGRESSIVE_720)):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "best"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["mode"], "stream")
        self.assertFalse(data["needs_processing"])
        self.assertEqual(data["filename"], "Test Video.mp4")
        self.assertTrue(data["proxy_url"].startswith("/api/download?"))
        self.assertIn("direct_url", data)

    def test_resolve_dash_merge(self):
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "best"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["mode"], "disk_merge")
        self.assertTrue(data["needs_processing"])
        self.assertNotIn("direct_url", data)
        self.assertEqual(data["filename"], "Test Video.mp4")

    def test_resolve_audio_mp3(self):
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "audio"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["mode"], "disk_audio")
        self.assertEqual(data["filename"], "Test Video.mp3")
        self.assertNotIn("direct_url", data)

    def test_resolve_merge_over_limit_falls_back_to_progressive(self):
        big = DASH_SET + [{
            "format_id": "22", "vcodec": "avc1", "acodec": "mp4a", "height": 720,
            "ext": "mp4", "protocol": "https",
            "url": "https://media.example/p720.mp4", "filesize": 9_000_000,
        }]
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(big)), \
             mock.patch.object(sb, "MAX_MERGE_BYTES", 1000):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "best"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["mode"], "stream")

    def test_resolve_custom_format_ids(self):
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "137+140"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["mode"], "disk_merge")

    def test_proxy_url_format_round_trips(self):
        # The proxy_url produced by /api/resolve for a merged 'best' download
        # must itself be resolvable by /api/download (regression guard).
        from urllib.parse import parse_qs, urlparse
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "best"})
        self.assertEqual(r.status_code, 200)
        query = parse_qs(urlparse(r.get_json()["proxy_url"]).query)
        round_trip_format = query["format"][0]
        self.assertEqual(round_trip_format, "best")
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)):
            media = sb.resolve_media("https://youtu.be/x", round_trip_format)
        self.assertEqual(media["mode"], "disk_merge")
        self.assertTrue(media["ydl_format"].startswith("bv*"))

    def test_resolve_missing_format(self):
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info([{"format_id": "1"}])):
            r = self.client.post("/api/resolve", json={"url": "https://youtu.be/x", "format": "999"})
        self.assertEqual(r.status_code, 404)


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_download_streams_progressive(self):
        upstream = FakeUpstream(b"0123456789", {"Content-Type": "video/mp4", "Content-Length": "10"})
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(PROGRESSIVE_720)), \
             mock.patch.object(sb, "open_upstream", return_value=upstream) as mo:
            r = self.client.get("/api/download?url=https://youtu.be/x&format=best&filename=my video.mp4")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"0123456789")
        self.assertEqual(r.headers["Content-Type"], "video/mp4")
        self.assertEqual(r.headers["Content-Length"], "10")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("my%20video.mp4", r.headers["Content-Disposition"])
        mo.assert_called_once()
        media_url = mo.call_args[0][0]
        self.assertIn("progressive.mp4", str(media_url))

    def test_download_range_passthrough(self):
        upstream = FakeUpstream(b"3456", {"Content-Type": "video/mp4", "Content-Length": "4",
                                          "Content-Range": "bytes 3-6/10"}, status=206)
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(PROGRESSIVE_720)), \
             mock.patch.object(sb, "open_upstream", return_value=upstream) as mo:
            r = self.client.get("/api/download?url=https://youtu.be/x&format=best",
                                headers={"Range": "bytes=3-6"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.headers["Content-Range"], "bytes 3-6/10")
        range_arg = mo.call_args[0][1]
        self.assertEqual(range_arg, "bytes=3-6")

    def test_download_audio_uses_tmp_processing(self):
        tmpfile = os.path.join(_TEST_ROOT, "media.mp3")
        with open(tmpfile, "wb") as f:
            f.write(b"ID3fakeaudio")
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)), \
             mock.patch.object(sb, "process_to_tmp", return_value=(tmpfile, _TEST_ROOT)) as mp:
            r = self.client.get("/api/download?url=https://youtu.be/x&format=audio")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"ID3fakeaudio")
        self.assertIn("audio/mpeg", r.headers["Content-Type"])
        self.assertTrue(r.headers["Content-Disposition"].endswith(".mp3*") or ".mp3" in r.headers["Content-Disposition"])
        mp.assert_called_once()
        self.assertTrue(mp.call_args[0][1].startswith("ba/"))


class TestClip(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_clip_exports_section(self):
        tmpfile = os.path.join(_TEST_ROOT, "clip.mp4")
        with open(tmpfile, "wb") as f:
            f.write(b"MP4DATA")
        with mock.patch.object(sb, "extract_video_info", return_value=fake_info(DASH_SET)), \
             mock.patch.object(sb, "process_to_tmp", return_value=(tmpfile, _TEST_ROOT)) as mp:
            r = self.client.get("/api/clip?url=https://youtu.be/x&start=1.5&end=5.25&label=Intro Bit&title=My Vid")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"MP4DATA")
        self.assertEqual(r.headers["Content-Type"], "video/mp4")
        self.assertIn("My Vid_Intro Bit.mp4", r.headers["Content-Disposition"])
        # download_ranges callback was passed and yields the requested section
        ranges_cb = mp.call_args[1]["download_ranges"]
        self.assertEqual(ranges_cb(None, None), [{"start_time": 1.5, "end_time": 5.25}])


class TestAI(unittest.TestCase):
    def setUp(self):
        self.client = sb.app.test_client()

    def test_ai_disabled_returns_501_with_guidance(self):
        r = self.client.post("/api/ai/analyze", json={"url": "https://youtu.be/x"})
        self.assertEqual(r.status_code, 501)
        self.assertIn("USE_HF_INFERENCE_API", r.get_json()["error"])

    def test_ai_analyze_when_enabled(self):
        with mock.patch.object(sb, "_ai_available", return_value=True), \
             mock.patch("ai_helper.analyze_video",
                        return_value={"chunks": [{"text": "hi", "embedding": [0.1]}], "sections": []}):
            r = self.client.post("/api/ai/analyze", json={"url": "https://youtu.be/x"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertNotIn("embedding", data["chunks"][0])

    def test_ai_search_when_enabled(self):
        with mock.patch.object(sb, "_ai_available", return_value=True), \
             mock.patch("ai_helper.query_transcript", return_value=[{"start": 0, "similarity": 0.9}]):
            r = self.client.post("/api/ai/search", json={"url": "https://youtu.be/x", "query": "intro"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()[0]["similarity"], 0.9)


if __name__ == "__main__":
    unittest.main()
