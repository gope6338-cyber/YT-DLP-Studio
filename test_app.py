import dataclasses
import shutil
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import app as app_module
import ai_helper
from ai_helper import build_sections, chunk_transcript, parse_subtitles, time_to_seconds
from app import (
    _ffmpeg_clip_args,
    _format_expression,
    app,
    clean_filename,
    kill_process_tree,
)
from config import create_app_config, resolve_save_dir


def make_config(**overrides):
    """A real AppConfig with selected fields replaced (frozen dataclass)."""
    config = dataclasses.replace(create_app_config({}), **overrides)
    if "default_save_dir" in overrides and "allowed_save_dirs" not in overrides:
        config = dataclasses.replace(config, allowed_save_dirs=(config.default_save_dir,))
    return config


class TestConfig(unittest.TestCase):

    def test_create_app_config_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = create_app_config()
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 5000)
        self.assertTrue(config.default_save_dir.endswith("Downloads"))
        self.assertTrue(config.cache_dir.endswith(".cache"))
        self.assertEqual(config.yt_dlp_bin, "yt-dlp")
        self.assertEqual(config.ffmpeg_bin, "ffmpeg")
        self.assertEqual(config.app_token, "")
        self.assertEqual(config.clip_cut_mode, "precise")

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

    def test_create_app_config_hf_inference(self):
        env = {"USE_HF_INFERENCE_API": "true", "HF_TOKEN": "mock-token-123"}
        with mock.patch.dict(os.environ, env, clear=True):
            config = create_app_config()
        self.assertTrue(config.use_hf_inference_api)
        self.assertEqual(config.hf_token, "mock-token-123")

    def test_secret_key_is_derived_from_app_token(self):
        env = {"APP_TOKEN": "s3cret"}
        with mock.patch.dict(os.environ, env, clear=True):
            config = create_app_config()
        self.assertTrue(config.secret_key)
        self.assertNotEqual(config.secret_key, "s3cret")


class TestSaveDirContainment(unittest.TestCase):
    """C1: downloads must stay inside the configured roots."""

    def test_paths_inside_the_root_are_allowed(self):
        config = make_config(default_save_dir="/data/downloads")
        self.assertEqual(
            resolve_save_dir("/data/downloads", config), os.path.abspath("/data/downloads"))
        self.assertEqual(
            resolve_save_dir("/data/downloads/music", config),
            os.path.abspath("/data/downloads/music"))

    def test_paths_outside_the_root_are_rejected(self):
        config = make_config(default_save_dir="/data/downloads")
        for candidate in ("/etc", "/data", "/data/downloads-evil", "/data/other", "../escape"):
            self.assertIsNone(resolve_save_dir(candidate, config), candidate)

    def test_prefix_attack_is_rejected(self):
        # "/data/downloads" is a real directory; "/data/downloadsX" is not a child.
        config = make_config(default_save_dir="/data/downloads")
        self.assertIsNone(resolve_save_dir("/data/downloadsX", config))

    def test_empty_value_falls_back_to_default(self):
        config = make_config(default_save_dir="/data/downloads")
        self.assertEqual(resolve_save_dir(None, config), os.path.abspath("/data/downloads"))

    def test_extra_roots_are_honoured(self):
        config = make_config(default_save_dir="/data/downloads",
                             allowed_save_dirs=("/data/downloads", "/mnt/nas"))
        self.assertEqual(resolve_save_dir("/mnt/nas/clips", config), os.path.abspath("/mnt/nas/clips"))


class TestFormatSelection(unittest.TestCase):
    """H2: a bare format id must not produce a silent file."""

    def test_default_is_a_merged_expression(self):
        self.assertEqual(_format_expression(None), "bv*+ba/b")
        self.assertEqual(_format_expression(""), "bv*+ba/b")
        self.assertEqual(_format_expression("best"), "bv*+ba/b")

    def test_audio_shortcut(self):
        self.assertEqual(_format_expression("audio"), "ba")

    def test_bare_format_id_is_merged_with_audio(self):
        self.assertEqual(_format_expression("137"), "137+ba/b")
        self.assertEqual(_format_expression("248"), "248+ba/b")

    def test_existing_expressions_pass_through(self):
        self.assertEqual(_format_expression("bv*+ba/b"), "bv*+ba/b")
        self.assertEqual(_format_expression("bestvideo[height<=720]+bestaudio"),
                         "bestvideo[height<=720]+bestaudio")

    def test_unsafe_characters_are_rejected(self):
        for bad in ("137; rm -rf /", "137`whoami`", "137\n-o", "x" * 200):
            with self.assertRaises(ValueError, msg=bad):
                _format_expression(bad)


class TestClipExport(unittest.TestCase):

    def test_precise_mode_re_encodes(self):
        args = _ffmpeg_clip_args("in.mp4", "out.mp4", 12.5, 30.0, "precise")
        self.assertIn("libx264", args)
        self.assertIn("-ss", args)
        self.assertEqual(args[args.index("-ss") + 1], "12.500")
        self.assertEqual(args[args.index("-to") + 1], "30.000")

    def test_fast_mode_stream_copies(self):
        args = _ffmpeg_clip_args("in.mp4", "out.mp4", 1, 2, "fast")
        self.assertIn("copy", args)
        self.assertNotIn("libx264", args)

    def test_ffmpeg_never_prompts_for_input(self):
        self.assertIn("-nostdin", _ffmpeg_clip_args("in.mp4", "out.mp4", 0, 1, "precise"))


class TestSubtitleHelpers(unittest.TestCase):

    def test_time_to_seconds(self):
        self.assertEqual(time_to_seconds("00:01:23.456"), 83.456)
        self.assertEqual(time_to_seconds("00:01:23,456"), 83.456)
        self.assertEqual(time_to_seconds("02:15.500"), 135.5)
        self.assertEqual(time_to_seconds("10"), 10.0)

    def test_parse_subtitles(self):
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
        chunks = chunk_transcript(entries, chunk_duration=30)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]['start'], 0.0)
        self.assertEqual(chunks[0]['end'], 35.0)
        self.assertEqual(chunks[0]['text'], "Intro part. First point. Second point.")
        self.assertEqual(chunks[1]['end'], 70.0)

    def test_deduplicate_entries(self):
        entries = [
            {"start": 0.0, "end": 2.0, "text": "Hello"},
            {"start": 1.0, "end": 3.0, "text": "Hello world"},
            {"start": 2.0, "end": 4.0, "text": "Hello world this"},
            {"start": 5.0, "end": 7.0, "text": "Different sentence"}
        ]
        res = ai_helper.deduplicate_entries(entries)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["text"], "Hello world this")
        self.assertEqual(res[0]["start"], 0.0)
        self.assertEqual(res[0]["end"], 4.0)


class TestSectionBuilding(unittest.TestCase):
    """Step 3 of the pipeline: scored chunks -> clip ranges."""

    def _chunks(self, count, importance=1.0, span=60.0):
        chunks = []
        for i in range(count):
            chunks.append({
                'id': i,
                'start': i * span,
                'end': (i + 1) * span,
                'text': f'chunk {i}',
                'importance': importance,
            })
        return chunks

    def test_adjacent_important_chunks_merge_into_one_clip(self):
        config = make_config(ai_importance_threshold=0.4, ai_min_section_seconds=0,
                             ai_max_section_seconds=600, ai_max_sections=10)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(self._chunks(3))
        self.assertEqual(len(sections), 1)
        # 1s of padding is added on each side so the cut is not abrupt
        self.assertAlmostEqual(sections[0]['start'], 0.0)
        self.assertAlmostEqual(sections[0]['end'], 181.0)

    def test_low_scoring_chunks_are_dropped(self):
        chunks = self._chunks(4)
        chunks[1]['importance'] = 0.05
        chunks[2]['importance'] = 0.05
        chunks[3]['importance'] = 0.9
        config = make_config(ai_importance_threshold=0.4, ai_min_section_seconds=0,
                             ai_max_section_seconds=600, ai_max_sections=10)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(chunks)
        self.assertEqual([s['start'] for s in sections], [0.0, 179.0])

    def test_fallback_keeps_best_chunks_when_nothing_clears_the_bar(self):
        chunks = self._chunks(10, importance=0.1)
        chunks[7]['importance'] = 0.3
        config = make_config(ai_importance_threshold=0.9, ai_min_section_seconds=0,
                             ai_max_section_seconds=600, ai_max_sections=10)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(chunks)
        self.assertTrue(sections)
        # The single above-average chunk must end up inside one of the clips.
        self.assertTrue(any(s['start'] <= 7 * 60.0 <= s['end'] for s in sections),
                        f"sections={sections}")

    def test_sections_are_capped_and_sorted(self):
        chunks = self._chunks(12)
        config = make_config(ai_importance_threshold=0.4, ai_min_section_seconds=0,
                             ai_max_section_seconds=60, ai_max_sections=3)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(chunks)
        self.assertLessEqual(len(sections), 3)
        self.assertEqual(sections, sorted(sections, key=lambda s: s['start']))

    def test_short_sections_are_padded_to_the_minimum(self):
        config = make_config(ai_importance_threshold=0.4, ai_min_section_seconds=30,
                             ai_max_section_seconds=600, ai_max_sections=10)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(self._chunks(1, span=10.0))
        self.assertGreaterEqual(sections[0]['end'] - sections[0]['start'], 30.0)

    def test_sections_never_run_past_the_media(self):
        config = make_config(ai_importance_threshold=0.4, ai_min_section_seconds=0,
                             ai_max_section_seconds=600, ai_max_sections=10)
        with mock.patch.object(ai_helper, 'APP_CONFIG', config):
            sections = build_sections(self._chunks(4, span=60.0), media_duration=150.0)
        self.assertLessEqual(sections[-1]['end'], 150.0)


class TestAnalyzeVideo(unittest.TestCase):

    @staticmethod
    def _fake_hf_api(score=0.9, summary="The good bit"):
        """Answers NLI and summarisation calls based on the model in the URL."""
        def _call(url, payload, token=None, retries=5, delay=5):
            if "mnli" in url:
                return {"labels": ["highlight insight", "filler chat"],
                        "scores": [score, round(1.0 - score, 2)]}
            return [{"summary_text": summary}]
        return _call

    def _run(self, entries, api_responses):
        temp_cache = tempfile.mkdtemp()
        config = make_config(use_hf_inference_api=True, hf_token="tok",
                             temp_dir=tempfile.gettempdir())
        try:
            with mock.patch.object(ai_helper, 'APP_CONFIG', config), \
                 mock.patch.object(ai_helper, 'CACHE_DIR', temp_cache), \
                 mock.patch.object(ai_helper, 'download_subtitles',
                                   return_value=(entries, "en")), \
                 mock.patch.object(ai_helper, 'call_hf_api', side_effect=api_responses) as api:
                result = ai_helper.analyze_video("https://youtu.be/abcdefghijk")
            return result, api
        finally:
            for f in os.listdir(temp_cache):
                os.remove(os.path.join(temp_cache, f))
            os.rmdir(temp_cache)

    def test_pipeline_produces_scored_chunks_and_clip_sections(self):
        entries = [{"start": float(i * 10), "end": float(i * 10 + 10),
                    "text": f"sentence {i}"} for i in range(12)]
        result, _ = self._run(entries, self._fake_hf_api())
        self.assertIn("chunks", result)
        self.assertIn("sections", result)
        self.assertNotIn("error", result)
        self.assertEqual(len(result["sections"]), 1)
        self.assertEqual(result["sections"][0]["label"], "The good bit")
        self.assertEqual(result["transcript_entries"], 12)
        # embeddings are gone: chunks carry text and a score, nothing more
        self.assertEqual(sorted(result["chunks"][0]), ["end", "id", "importance", "start", "text"])

    def test_missing_subtitles_is_reported_not_crashed(self):
        result, _ = self._run([], [])
        self.assertIn("error", result)
        self.assertEqual(result["sections"], [])

    def test_cache_is_reused_on_the_second_call(self):
        entries = [{"start": 0.0, "end": 10.0, "text": "hello"}]
        temp_cache = tempfile.mkdtemp()
        config = make_config(use_hf_inference_api=True, hf_token="tok",
                             temp_dir=tempfile.gettempdir())
        try:
            with mock.patch.object(ai_helper, 'APP_CONFIG', config), \
                 mock.patch.object(ai_helper, 'CACHE_DIR', temp_cache), \
                 mock.patch.object(ai_helper, 'download_subtitles',
                                   return_value=(entries, "en")), \
                 mock.patch.object(ai_helper, 'call_hf_api',
                                   side_effect=self._fake_hf_api(summary="Cached")) as api:
                ai_helper.analyze_video("https://youtu.be/abcdefghijk")
                calls_first = api.call_count
                # The second run must be served entirely from the cache file.
                result = ai_helper.analyze_video("https://youtu.be/abcdefghijk")
            self.assertGreater(calls_first, 0)
            self.assertEqual(api.call_count, calls_first)
            self.assertEqual(result["sections"][0]["label"], "Cached")
            self.assertTrue(os.listdir(temp_cache))
        finally:
            for f in os.listdir(temp_cache):
                os.remove(os.path.join(temp_cache, f))
            os.rmdir(temp_cache)

    def test_progress_callback_reaches_one_hundred(self):
        entries = [{"start": 0.0, "end": 10.0, "text": "hello"}]
        seen = []
        temp_cache = tempfile.mkdtemp()
        config = make_config(use_hf_inference_api=True, hf_token="tok",
                             temp_dir=tempfile.gettempdir())
        try:
            with mock.patch.object(ai_helper, 'APP_CONFIG', config), \
                 mock.patch.object(ai_helper, 'CACHE_DIR', temp_cache), \
                 mock.patch.object(ai_helper, 'download_subtitles',
                                   return_value=(entries, "en")), \
                 mock.patch.object(ai_helper, 'call_hf_api',
                                   side_effect=self._fake_hf_api()):
                ai_helper.analyze_video("https://youtu.be/abcdefghijk",
                                        progress_cb=lambda p, m: seen.append(p))
        finally:
            for f in os.listdir(temp_cache):
                os.remove(os.path.join(temp_cache, f))
            os.rmdir(temp_cache)
        self.assertEqual(seen[-1], 100)
        self.assertEqual(seen, sorted(seen))


class TestHfApiRetry(unittest.TestCase):

    @mock.patch("urllib.request.urlopen")
    def test_call_hf_api_success(self, mock_urlopen):
        mock_response = mock.Mock()
        mock_response.read.return_value = b'{"result": "success"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = ai_helper.call_hf_api("https://mock-url.com", {"key": "val"}, token="token")
        self.assertEqual(res, {"result": "success"})
        mock_urlopen.assert_called_once()

    @mock.patch("urllib.request.urlopen")
    @mock.patch("time.sleep")
    def test_call_hf_api_retry_on_loading(self, mock_sleep, mock_urlopen):
        import urllib.error

        err_response = mock.Mock()
        err_response.read.return_value = b'{"error": "Model currently loading", "estimated_time": 0.5}'
        mock_err = urllib.error.HTTPError(
            url="https://mock-url.com", code=503, msg="Service Unavailable",
            hdrs={}, fp=err_response)

        class MockResponse:
            def read(self):
                return b'{"result": "done"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_urlopen.side_effect = [mock_err, MockResponse()]
        res = ai_helper.call_hf_api("https://mock-url.com", {"key": "val"})
        self.assertEqual(res, {"result": "done"})
        mock_sleep.assert_called_with(0.5)


class TestMisc(unittest.TestCase):

    def test_clean_filename(self):
        self.assertEqual(clean_filename("Video: Title? <New> *Nice*"), "Video Title New Nice")
        self.assertNotIn("/", clean_filename("a/b/c"))
        self.assertTrue(clean_filename("..."))  # never empty

    def test_kill_process_tree_posix_uses_process_group(self):
        with mock.patch("app.sys.platform", "linux"), \
             mock.patch("app.os.getpgid", return_value=123, create=True), \
             mock.patch("app.os.killpg", create=True) as mock_killpg:
            kill_process_tree(456)
        mock_killpg.assert_called_once()

    def test_kill_process_tree_windows_uses_taskkill(self):
        if sys.platform != 'win32':
            self.skipTest("windows specific")
        self.skipTest("covered manually")

    def test_parse_ytdlp_progress(self):
        from app import parse_ytdlp_progress
        pct, speed, eta = parse_ytdlp_progress(
            "[download]  12.3% of ~45.20MiB at  3.12MiB/s ETA 00:15")
        self.assertAlmostEqual(pct, 12.3)
        self.assertEqual(speed, "3.12MiB/s")
        self.assertEqual(eta, "00:15")

    def test_note_output_file_records_paths(self):
        from app import note_output_file
        files = []
        note_output_file('[download] Destination: /data/a.mp4', files)
        note_output_file('[Merger] Merging formats into "/data/a.mkv"', files)
        note_output_file('[info] nothing here', files)
        self.assertEqual(files, ["/data/a.mp4", "/data/a.mkv"])

    def test_public_task_hides_private_keys(self):
        from app import _public_task
        task = {"id": "x", "process": object(), "result": {"big": True}, "status": "done"}
        self.assertEqual(_public_task(task), {"id": "x", "status": "done"})


class TestApi(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_validation_errors(self):
        self.assertEqual(self.client.post("/api/info", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/enqueue", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/clip", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/ai/analyze", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/remove", json={}).status_code, 400)

    def test_queue_and_stream(self):
        queue_response = self.client.get("/api/queue")
        self.assertEqual(queue_response.status_code, 200)
        self.assertIsInstance(queue_response.json, list)

        stream_response = self.client.get("/api/stream", buffered=False)
        self.assertEqual(stream_response.status_code, 200)
        self.assertIn("text/event-stream", stream_response.headers["Content-Type"])

    def test_enqueue_rejects_paths_outside_the_root(self):
        response = self.client.post("/api/enqueue", json={
            "url": "https://example.com/v", "save_dir": "/etc"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("allowed", response.json)

    def test_clip_rejects_bad_ranges(self):
        response = self.client.post("/api/clip", json={
            "url": "https://example.com/v",
            "save_dir": None,
            "regions": [{"start": 30, "end": 10}]})
        self.assertEqual(response.status_code, 400)

    def test_remove_unknown_task_is_404(self):
        self.assertEqual(self.client.post("/api/remove", json={"task_id": "nope"}).status_code, 404)

    def test_ai_result_for_unknown_task_is_404(self):
        self.assertEqual(self.client.get("/api/ai/result/nope").status_code, 404)

    @mock.patch("app.ai_helper.analyze_video")
    def test_ai_pipeline_returns_clip_sections(self, mock_analyze):
        mock_analyze.return_value = {
            "video_id": "abcdefghijk",
            "language": "en",
            "transcript_entries": 40,
            "chunks": [{"id": 0, "start": 0.0, "end": 60.0, "text": "x", "importance": 0.9}],
            "sections": [{"start": 10.0, "end": 40.0, "score": 0.9, "label": "The good bit"}],
        }
        response = self.client.post("/api/ai/analyze",
                                    json={"url": "https://youtu.be/abcdefghijk"})
        self.assertEqual(response.status_code, 200)
        task_id = response.json["task_id"]

        task = None
        for _ in range(200):
            tasks = self.client.get("/api/queue").json
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task and task["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        self.assertIsNotNone(task, "analysis task never finished")
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertEqual(task["sections"][0]["label"], "The good bit")

        result = self.client.get(f"/api/ai/result/{task_id}").json
        self.assertEqual(result["sections"][0]["start"], 10.0)
        self.assertEqual(result["chunks"][0]["importance"], 0.9)

    @mock.patch("app.ai_helper.analyze_video")
    def test_ai_failure_is_reported_on_the_task(self, mock_analyze):
        mock_analyze.return_value = {"error": "No subtitles or captions were available."}
        response = self.client.post("/api/ai/analyze", json={"url": "https://youtu.be/abcdefghijk"})
        task_id = response.json["task_id"]

        task = None
        for _ in range(200):
            tasks = self.client.get("/api/queue").json
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task and task["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        self.assertEqual(task["status"], "failed")
        self.assertIn("subtitles", task["error"])


class TestEndToEndWithFakeBinaries(unittest.TestCase):
    """Runs real jobs through the worker using the offline yt-dlp/ffmpeg stubs.

    This is the regression guard for the two bugs that used to hang or silently
    degrade the queue: an undrained ffmpeg pipe deadlocking the only worker, and
    a format spec that resolved to the 360p progressive stream.
    """

    @classmethod
    def setUpClass(cls):
        cls.save_dir = tempfile.mkdtemp(prefix="studio-save-")
        cls.temp_dir = tempfile.mkdtemp(prefix="studio-tmp-")
        cls.fake_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fakebin")
        cls.config = dataclasses.replace(
            create_app_config({}),
            default_save_dir=cls.save_dir,
            allowed_save_dirs=(cls.save_dir,),
            temp_dir=cls.temp_dir,
            yt_dlp_bin=os.path.join(cls.fake_bin, "yt-dlp"),
            ffmpeg_bin=os.path.join(cls.fake_bin, "ffmpeg"),
        )
        cls.patcher = mock.patch.object(app_module, "APP_CONFIG", cls.config)
        cls.patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        for directory in (cls.save_dir, cls.temp_dir):
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                os.remove(path) if os.path.isfile(path) else shutil.rmtree(path)
            os.rmdir(directory)

    def setUp(self):
        self.client = app.test_client()

    def _wait(self, task_id, timeout=60):
        for _ in range(int(timeout / 0.05)):
            tasks = self.client.get("/api/queue").json
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task and task["status"] in ("completed", "failed", "cancelled"):
                return task
            time.sleep(0.05)
        return None

    def _clear(self):
        for name in os.listdir(self.save_dir):
            os.remove(os.path.join(self.save_dir, name))

    def test_download_job_writes_a_file(self):
        self._clear()
        response = self.client.post("/api/enqueue", json={
            "url": "https://example.com/watch?v=abcdefghijk",
            "save_dir": self.save_dir,
            "format": "best",
            "title": "E2E Video",
        })
        self.assertEqual(response.status_code, 200)
        task = self._wait(response.json["task_id"])
        self.assertIsNotNone(task, "download never finished")
        self.assertEqual(task["status"], "completed", task.get("error"))
        self.assertEqual(task["progress"], 100.0)
        self.assertEqual(os.listdir(self.save_dir), ["Fake Video.mp4"])

        self.client.post("/api/remove", json={"task_id": task["id"]})

    def test_clip_job_cuts_each_enabled_region(self):
        self._clear()
        response = self.client.post("/api/clip", json={
            "url": "https://example.com/watch?v=abcdefghijk",
            "save_dir": self.save_dir,
            "title": "E2E Video",
            "cut_mode": "precise",
            "regions": [
                {"start": 10, "end": 40, "label": "Intro", "enabled": True},
                {"start": 100, "end": 160, "label": "NOISY chatty", "enabled": True},
                {"start": 200, "end": 260, "label": "skipped", "enabled": False},
            ],
        })
        self.assertEqual(response.status_code, 200)
        task = self._wait(response.json["task_id"])
        self.assertIsNotNone(task, "clip export never finished")
        self.assertEqual(task["status"], "completed", task.get("error"))

        produced = sorted(os.listdir(self.save_dir))
        self.assertEqual(len(produced), 2, produced)
        self.assertIn("E2E Video - Intro.mp4", produced)
        # The NOISY label makes the stub emit >64KiB on stderr; a naive
        # wait() on undrained pipes would have hung here forever.
        self.assertTrue(any("NOISY" in name for name in produced))

        self.client.post("/api/remove", json={"task_id": task["id"]})

    def test_clip_failure_reports_the_ffmpeg_error(self):
        self._clear()
        response = self.client.post("/api/clip", json={
            "url": "https://example.com/watch?v=abcdefghijk",
            "save_dir": self.save_dir,
            "title": "E2E Video",
            "regions": [{"start": 0, "end": 10, "label": "FAILCLIP", "enabled": True}],
        })
        task = self._wait(response.json["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("fake ffmpeg failure", task["error"])

        self.client.post("/api/remove", json={"task_id": task["id"]})

    def test_cancelling_a_job_stops_it(self):
        response = self.client.post("/api/enqueue", json={
            "url": "https://example.com/watch?v=abcdefghijk",
            "save_dir": self.save_dir,
            "format": "best",
        })
        task_id = response.json["task_id"]
        self.client.post("/api/cancel", json={"task_id": task_id})
        task = self._wait(task_id)
        self.assertIn(task["status"], ("cancelled", "failed"))
        self.client.post("/api/remove", json={"task_id": task_id})

    def test_no_temp_directories_are_left_behind(self):
        self.assertEqual(
            [d for d in os.listdir(self.temp_dir) if d.startswith("temp_clips_")], [])


class TestAuth(unittest.TestCase):
    """C2: with APP_TOKEN set, nothing is served without signing in."""

    def setUp(self):
        self.config = make_config(app_token="s3cret")
        self.patcher = mock.patch.object(app_module, "APP_CONFIG", self.config)
        self.patcher.start()
        self.client = app.test_client()

    def tearDown(self):
        self.patcher.stop()

    def test_api_requires_authentication(self):
        self.assertEqual(self.client.get("/api/queue").status_code, 401)
        self.assertEqual(
            self.client.post("/api/enqueue", json={"url": "https://x.com/v"}).status_code, 401)

    def test_index_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_page_renders(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_wrong_token_is_rejected(self):
        response = self.client.post("/login", data={"token": "nope"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 401)

    def test_correct_token_grants_a_session(self):
        response = self.client.post("/login", data={"token": "s3cret"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/queue").status_code, 200)

    def test_token_header_works_for_scripts(self):
        self.assertEqual(
            self.client.get("/api/queue", headers={"X-App-Token": "s3cret"}).status_code, 200)
        self.assertEqual(
            self.client.get("/api/queue", headers={"X-App-Token": "bad"}).status_code, 401)

    def test_logout_clears_the_session(self):
        self.client.post("/login", data={"token": "s3cret"})
        self.client.get("/logout")
        self.assertEqual(self.client.get("/api/queue").status_code, 401)


if __name__ == '__main__':
    unittest.main()
