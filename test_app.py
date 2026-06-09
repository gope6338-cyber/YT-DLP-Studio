import os
import unittest
import tempfile
import time
import subprocess
import sys
from unittest import mock

# Import functions to test
from ai_helper import parse_subtitles, chunk_transcript, time_to_seconds
from app import app, clean_filename, create_app_config, kill_process_tree

class TestYTDLPStudio(unittest.TestCase):

    def test_create_app_config_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = create_app_config()
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 5000)
        self.assertTrue(config.default_save_dir.endswith("Downloads"))
        self.assertTrue(config.cache_dir.endswith(".cache"))
        self.assertEqual(config.yt_dlp_bin, "yt-dlp")
        self.assertEqual(config.ffmpeg_bin, "ffmpeg")

    def test_create_app_config_from_environment(self):
        env = {
            "HOST": "0.0.0.0",
            "PORT": "8080",
            "DEFAULT_SAVE_DIR": "/data/downloads",
            "APP_CACHE_DIR": "/data/cache",
            "APP_TEMP_DIR": "/data/tmp",
            "YT_DLP_BIN": "/usr/local/bin/yt-dlp",
            "FFMPEG_BIN": "/usr/bin/ffmpeg",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = create_app_config()
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.default_save_dir, os.path.abspath("/data/downloads"))
        self.assertEqual(config.cache_dir, os.path.abspath("/data/cache"))
        self.assertEqual(config.temp_dir, os.path.abspath("/data/tmp"))
        self.assertEqual(config.yt_dlp_bin, "/usr/local/bin/yt-dlp")
        self.assertEqual(config.ffmpeg_bin, "/usr/bin/ffmpeg")

    def test_time_to_seconds(self):
        self.assertEqual(time_to_seconds("00:01:23.456"), 83.456)
        self.assertEqual(time_to_seconds("00:01:23,456"), 83.456)
        self.assertEqual(time_to_seconds("02:15.500"), 135.5)
        self.assertEqual(time_to_seconds("10"), 10.0)

    def test_parse_subtitles(self):
        # Create a mock VTT file
        vtt_content = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:04.500
Hello <c.yellow>World</c>!
This is a test.

00:00:04.500 --> 00:00:08.000
Second block of subtitles.
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.vtt', delete=False, encoding='utf-8') as f:
            f.write(vtt_content)
            temp_path = f.name

        try:
            entries = parse_subtitles(temp_path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]['start'], 1.0)
            self.assertEqual(entries[0]['end'], 4.5)
            self.assertEqual(entries[0]['text'], "Hello World! This is a test.")
            self.assertEqual(entries[1]['text'], "Second block of subtitles.")
        finally:
            os.remove(temp_path)

    def test_chunk_transcript(self):
        entries = [
            {'start': 0.0, 'end': 10.0, 'text': 'Intro part.'},
            {'start': 10.0, 'end': 20.0, 'text': 'First point.'},
            {'start': 20.0, 'end': 35.0, 'text': 'Second point.'},
            {'start': 35.0, 'end': 70.0, 'text': 'Longer explanation.'},
        ]
        # Chunk duration = 30 seconds
        chunks = chunk_transcript(entries, chunk_duration=30)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]['start'], 0.0)
        self.assertEqual(chunks[0]['end'], 35.0)
        self.assertEqual(chunks[0]['text'], "Intro part. First point. Second point.")
        self.assertEqual(chunks[1]['start'], 35.0)
        self.assertEqual(chunks[1]['end'], 70.0)
        self.assertEqual(chunks[1]['text'], "Longer explanation.")

    def test_clean_filename(self):
        self.assertEqual(clean_filename("Video: Title? <New> *Nice*"), "Video Title New Nice")

    def test_kill_process_tree(self):
        if sys.platform == 'win32':
            # Spawn a long-running mock process in Windows (e.g. ping localhost -n 100)
            p = subprocess.Popen(
                ["ping", "127.0.0.1", "-n", "100"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            # Give it a tiny bit of time to start
            time.sleep(0.5)
            pid = p.pid
            
            # Assert process is running
            self.assertIsNone(p.poll())
            
            # Call kill process tree
            kill_process_tree(pid)
            
            # Assert process is terminated
            time.sleep(0.5)
            self.assertIsNotNone(p.poll())
            p.stdout.close()
            p.stderr.close()
        else:
            # Skip for non-windows platforms in general testing (but app targets Windows)
            pass

    def test_kill_process_tree_posix_uses_process_group(self):
        with mock.patch("app.sys.platform", "linux"), \
             mock.patch("app.os.getpgid", return_value=123, create=True), \
             mock.patch("app.os.killpg", create=True) as mock_killpg:
            kill_process_tree(456)
        mock_killpg.assert_called_once()

    def test_api_defaults_and_errors(self):
        client = app.test_client()

        queue_response = client.get("/api/queue")
        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(queue_response.json, [])

        stream_response = client.get("/api/stream")
        self.assertEqual(stream_response.status_code, 200)
        self.assertIn("text/event-stream", stream_response.headers["Content-Type"])

        info_response = client.post("/api/info", json={})
        self.assertEqual(info_response.status_code, 400)

        enqueue_response = client.post("/api/enqueue", json={})
        self.assertEqual(enqueue_response.status_code, 400)

        clip_response = client.post("/api/clip", json={})
        self.assertEqual(clip_response.status_code, 400)

        ai_analyze_response = client.post("/api/ai/analyze", json={})
        self.assertEqual(ai_analyze_response.status_code, 400)

        ai_search_response = client.post("/api/ai/search", json={})
        self.assertEqual(ai_search_response.status_code, 400)

    def test_create_app_config_hf_inference(self):
        env = {
            "USE_HF_INFERENCE_API": "true",
            "HF_TOKEN": "mock-token-123"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = create_app_config()
        self.assertTrue(config.use_hf_inference_api)
        self.assertEqual(config.hf_token, "mock-token-123")

    @mock.patch("urllib.request.urlopen")
    def test_call_hf_api_success(self, mock_urlopen):
        from ai_helper import call_hf_api
        
        mock_response = mock.Mock()
        mock_response.read.return_value = b'{"result": "success"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        res = call_hf_api("https://mock-url.com", {"key": "val"}, token="token")
        self.assertEqual(res, {"result": "success"})
        mock_urlopen.assert_called_once()

    @mock.patch("urllib.request.urlopen")
    @mock.patch("time.sleep")
    def test_call_hf_api_retry_on_loading(self, mock_sleep, mock_urlopen):
        from ai_helper import call_hf_api
        import urllib.error
        
        err_response = mock.Mock()
        err_response.read.return_value = b'{"error": "Model currently loading", "estimated_time": 0.5}'
        mock_err = urllib.error.HTTPError(
            url="https://mock-url.com",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=err_response
        )
        
        class MockResponse:
            def read(self):
                return b'{"result": "done"}'
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
        
        mock_success_response = MockResponse()
        mock_urlopen.side_effect = [mock_err, mock_success_response]
        
        res = call_hf_api("https://mock-url.com", {"key": "val"})
        self.assertEqual(res, {"result": "done"})
        self.assertEqual(mock_sleep.call_count, 1)
        mock_sleep.assert_called_with(0.5)

    @mock.patch("ai_helper.call_hf_api")
    @mock.patch("ai_helper.APP_CONFIG")
    def test_get_embedding_hf_api(self, mock_config, mock_call_api):
        from ai_helper import get_embedding
        mock_config.use_hf_inference_api = True
        mock_config.hf_token = "tok"
        mock_call_api.return_value = [0.1, 0.2, 0.3]
        
        emb = get_embedding("hello")
        self.assertEqual(emb, [0.1, 0.2, 0.3])
        mock_call_api.assert_called_once_with(
            "https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2",
            {"inputs": "hello"},
            "tok"
        )

    @mock.patch("ai_helper.download_subtitles")
    @mock.patch("ai_helper.call_hf_api")
    @mock.patch("ai_helper.APP_CONFIG")
    def test_analyze_video_hf_api(self, mock_config, mock_call_api, mock_download):
        from ai_helper import analyze_video
        
        mock_config.use_hf_inference_api = True
        mock_config.hf_token = "tok"
        mock_config.temp_dir = tempfile.gettempdir()
        
        temp_cache_dir = tempfile.mkdtemp()
        
        mock_download.return_value = [
            {"start": 0.0, "end": 10.0, "text": "Hello world."}
        ]
        
        mock_call_api.side_effect = [
            [0.1, 0.2],
            {"labels": ["highlight insight", "filler chat"], "scores": [0.8, 0.2]},
            [{"summary_text": "A brief summary"}]
        ]
        
        with mock.patch("ai_helper.CACHE_DIR", temp_cache_dir):
            res = analyze_video("https://youtube.com/watch?v=12345678901")
            
        self.assertIn("chunks", res)
        self.assertIn("sections", res)
        self.assertEqual(len(res["sections"]), 1)
        self.assertEqual(res["sections"][0]["label"], "A brief summary")
        
        for f in os.listdir(temp_cache_dir):
            os.remove(os.path.join(temp_cache_dir, f))
        os.rmdir(temp_cache_dir)

    def test_deduplicate_entries(self):
        from ai_helper import deduplicate_entries
        entries = [
            {"start": 0.0, "end": 2.0, "text": "Hello"},
            {"start": 1.0, "end": 3.0, "text": "Hello world"},
            {"start": 2.0, "end": 4.0, "text": "Hello world this"},
            {"start": 5.0, "end": 7.0, "text": "Different sentence"}
        ]
        res = deduplicate_entries(entries)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["text"], "Hello world this")
        self.assertEqual(res[0]["start"], 0.0)
        self.assertEqual(res[0]["end"], 4.0)
        self.assertEqual(res[1]["text"], "Different sentence")
        self.assertEqual(res[1]["start"], 5.0)
        self.assertEqual(res[1]["end"], 7.0)

    @mock.patch("ai_helper.download_subtitles")
    @mock.patch("ai_helper.call_hf_api")
    @mock.patch("ai_helper.APP_CONFIG")
    def test_analyze_video_threshold_fallback(self, mock_config, mock_call_api, mock_download):
        from ai_helper import analyze_video
        
        mock_config.use_hf_inference_api = True
        mock_config.hf_token = "tok"
        mock_config.temp_dir = tempfile.gettempdir()
        
        temp_cache_dir = tempfile.mkdtemp()
        
        mock_download.return_value = [
            {"start": 0.0, "end": 10.0, "text": "Intro segment."},
            {"start": 10.0, "end": 20.0, "text": "Body segment."},
            {"start": 20.0, "end": 30.0, "text": "Outro segment."}
        ]
        
        mock_call_api.side_effect = [
            [0.1, 0.2],
            {"labels": ["highlight insight", "filler chat"], "scores": [0.1, 0.9]},
            [{"summary_text": "Fallback summary"}]
        ]
        
        with mock.patch("ai_helper.CACHE_DIR", temp_cache_dir):
            res = analyze_video("https://youtube.com/watch?v=12345678902")
            
        self.assertIn("chunks", res)
        self.assertIn("sections", res)
        self.assertEqual(len(res["sections"]), 1)
        self.assertEqual(res["sections"][0]["label"], "Fallback summary")
        
        for f in os.listdir(temp_cache_dir):
            os.remove(os.path.join(temp_cache_dir, f))
        os.rmdir(temp_cache_dir)

if __name__ == '__main__':
    unittest.main()
