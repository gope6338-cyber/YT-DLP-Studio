import os
import unittest
import tempfile
import time
import subprocess
import sys

# Import functions to test
from ai_helper import parse_subtitles, chunk_transcript, time_to_seconds
from app import kill_process_tree, clean_filename

class TestYTDLPStudio(unittest.TestCase):

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

if __name__ == '__main__':
    unittest.main()
