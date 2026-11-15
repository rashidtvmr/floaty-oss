#!/usr/bin/env python3
"""Bundled offline STT worker. stdin: mono 16kHz s16le; stdout: bounded JSONL.

No microphone access, downloads, recordings or provider credentials here. The
authorized Rust session owns capture and destroys this process on Pause/Stop.
"""
import argparse
import array
import json
import sys
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200
MAX_TEXT = 8000


def emit(kind, text=""):
    print(json.dumps({"type": kind, "text": text[:MAX_TEXT]}), flush=True)


def pcm_samples(data):
    if len(data) % 2:
        raise ValueError("incomplete PCM sample")
    samples = array.array("h", data)
    if sys.byteorder != "little":
        samples.byteswap()
    return [value / 32768.0 for value in samples]


def transcribe_pcm(transcriber, source, listener):
    """Rotate native transcript state at phrase boundaries, at most 120s.

    Rotation bounds Moonshine's native transcript/audio history. At the hard
    limit an uninterrupted utterance is finalized before starting a new stream.
    """
    stream = None
    samples = 0
    try:
        while True:
            data = source.read(CHUNK_BYTES)
            if not data:
                break
            if stream is None:
                stream = transcriber.create_stream(update_interval=0.25)
                stream.add_listener(listener)
                stream.start()
                samples = 0
            listener.completed = False
            values = pcm_samples(data)
            stream.add_audio(values, SAMPLE_RATE)
            if listener.failed:
                raise RuntimeError("transcription failed")
            samples += len(values)
            if samples >= SAMPLE_RATE * 120 or (
                samples >= SAMPLE_RATE * 60 and listener.completed
            ):
                stream.stop()
                stream.close()
                stream = None
        if stream is not None:
            stream.stop()
        if listener.failed:
            raise RuntimeError("transcription failed")
    finally:
        if stream is not None:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    # PyInstaller's data directory, or the source checkout for development.
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    model = args.model_dir or bundled / "models/moonshine"
    try:
        from moonshine_voice import Transcriber, TranscriptEventListener, ModelArch

        class Listener(TranscriptEventListener):
            completed = False
            failed = False

            def on_line_text_changed(self, event):
                emit("partial", event.line.text)

            def on_line_completed(self, event):
                self.completed = True
                emit("final", event.line.text)

            def on_error(self, _event):
                self.failed = True

        # Pinned English tiny-streaming model is shipped by build-moonshine.py.
        with Transcriber(model_path=str(model), model_arch=ModelArch.TINY_STREAMING) as transcriber:
            emit("ready")
            if not args.check:
                transcribe_pcm(transcriber, sys.stdin.buffer, Listener())
        return 0
    except (Exception, KeyboardInterrupt):
        # Native exception messages may contain audio-derived data or paths.
        emit("error", "Built-in Moonshine failed; rebuild the speech runtime and model.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
