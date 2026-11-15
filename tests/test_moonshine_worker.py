"""Worker protocol tests without microphones, network or model downloads."""
import importlib.util
import io
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("moonshine_worker", Path(__file__).resolve().parents[1] / "scripts/moonshine-worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Listener:
    completed = False
    failed = False


class FakeStream:
    def __init__(self, finalize):
        self.finalize = finalize
        self.closed = self.stopped = False
        self.samples = 0

    def add_listener(self, listener):
        self.listener = listener

    def start(self):
        pass

    def add_audio(self, data, rate):
        assert rate == 16000
        assert len(data) <= 1600
        self.samples += len(data)
        self.listener.completed = self.finalize

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self, finalize):
        self.finalize = finalize
        self.streams = []

    def create_stream(self, update_interval):
        assert update_interval == 0.25
        stream = FakeStream(self.finalize)
        self.streams.append(stream)
        return stream


class WorkerTests(unittest.TestCase):
    def test_pcm_extremes_and_truncation(self):
        self.assertEqual(worker.pcm_samples(b"\x00\x80\x00\x00\xff\x7f"), [-1.0, 0.0, 32767 / 32768])
        with self.assertRaises(ValueError):
            worker.pcm_samples(b"\x00")

    def test_native_history_rotation_soft_and_hard_limit(self):
        for complete, seconds in ((True, 60), (False, 120)):
            with self.subTest(complete=complete):
                transcriber = FakeTranscriber(complete)
                source = io.BytesIO(b"\0\0" * worker.SAMPLE_RATE * (seconds + 1))
                worker.transcribe_pcm(transcriber, source, Listener())
                self.assertEqual(len(transcriber.streams), 2)
                self.assertEqual(transcriber.streams[0].samples, seconds * worker.SAMPLE_RATE)
                self.assertTrue(all(s.closed and s.stopped for s in transcriber.streams))

    def test_invalid_pcm_closes_stream(self):
        transcriber = FakeTranscriber(False)
        with self.assertRaises(ValueError):
            worker.transcribe_pcm(transcriber, io.BytesIO(b"\0"), Listener())
        self.assertTrue(transcriber.streams[0].closed)


if __name__ == "__main__":
    unittest.main()
