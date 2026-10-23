import io
import queue
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from voice import wake_word, tts, recorder


class VoicePipelineRegressionTests(unittest.TestCase):
    """Regression tests for the voice pipeline root causes."""

    def test_empty_transcript_retries_listening_automatically(self):
        """
        Hypothesis 4 & 8: STT returning an empty transcript on speech detection
        must automatically trigger a re-listen retry instead of silently dropping
        the command and requiring the user to repeat.
        """
        dummy_audio = io.BytesIO(b"RIFFdummydataWAVEfmt ")

        # transcribe_file returns "" on the first attempt, then "open notepad" on retry
        transcribe_side_effects = ["", "open notepad"]

        with (
            patch.object(wake_word, "record_command", return_value=dummy_audio) as mock_record,
            patch.object(wake_word, "transcribe_file", side_effect=transcribe_side_effects) as mock_transcribe,
        ):
            result = wake_word.get_command(max_empty_retries=1)

        self.assertEqual(result, "open notepad")
        self.assertEqual(mock_record.call_count, 2)
        self.assertEqual(mock_transcribe.call_count, 2)

    def test_local_tts_profile_is_sent_to_kokoro_worker(self):
        request = {}
        with patch.object(tts, "_TTS_SETTINGS", {
            "local_voice": "af_heart",
            "speech_rate": 1.0,
        }), patch.object(tts, "start_tts_worker") as start_worker, patch.object(
            tts.threading, "Event"
        ) as event_factory:
            worker = Mock()
            worker.poll.return_value = None
            worker.stdin = Mock()
            start_worker.return_value = worker
            event_factory.return_value.wait.return_value = True
            tts._generate_with_worker("hello", Path("missing.wav"))
            request = __import__("json").loads(worker.stdin.write.call_args.args[0])
        self.assertEqual(request["voice"], "af_heart")

    def test_empty_transcript_exhausted_retries_does_not_break_listener(self):
        """
        When empty transcript retries are exhausted, get_command returns empty string
        gracefully so the active conversation loop stays alive.
        """
        dummy_audio = io.BytesIO(b"RIFFdummydataWAVEfmt ")

        with (
            patch.object(wake_word, "record_command", return_value=dummy_audio) as mock_record,
            patch.object(wake_word, "transcribe_file", return_value="") as mock_transcribe,
        ):
            result = wake_word.get_command(max_empty_retries=1)

        self.assertEqual(result, "")
        self.assertEqual(mock_record.call_count, 2)
        self.assertEqual(mock_transcribe.call_count, 2)

    def test_command_arriving_during_speaking_to_listening_transition(self):
        """
        Hypothesis 2: When speech starts as TTS is finishing, BargeInListener must
        not discard the speech, and the post-playback transition window must be short
        (not a blocking 0.8s dead zone).
        """
        self.assertLessEqual(tts.POST_TTS_SETTLE_SECONDS, 0.1)

        mock_audio_file = Path("voice/interrupts/test_interrupt.wav")
        fake_audio = np.zeros(1600, dtype=np.float32)

        with (
            patch("soundfile.read", return_value=(fake_audio, 16000)),
            patch("sounddevice.play"),
            patch("sounddevice.stop") as mock_sd_stop,
            patch("sounddevice.get_stream", return_value=Mock(active=False)),
            patch.object(tts.BargeInListener, "start", return_value=True),
            patch.object(tts.BargeInListener, "set_playback_reference"),
            patch.object(tts.BargeInListener, "wait_for_command_end") as mock_wait_end,
            patch.object(tts.BargeInListener, "get_audio_file", return_value=mock_audio_file),
            patch.object(tts.BargeInListener, "stop") as mock_listener_stop,
        ):
            # Instantiate and set speech_detected = True as if speech began during playback
            with patch.object(tts, "BargeInListener") as MockListenerClass:
                mock_inst = Mock()
                mock_inst.interrupted = False
                mock_inst.speech_detected = True
                mock_inst.start.return_value = True
                mock_inst.get_audio_file.return_value = mock_audio_file
                MockListenerClass.return_value = mock_inst

                interrupted_file = tts.play_audio_interruptible(Path("fake.wav"))

        self.assertEqual(interrupted_file, mock_audio_file)
        mock_inst.wait_for_command_end.assert_called_once()
        mock_sd_stop.assert_called()

    def test_first_utterance_preserved_after_activation(self):
        """
        Hypothesis 1: When user speaks command in one breath ("Hey Jarvis open chrome"),
        follow-on speech is captured in the active wake stream and returned as audio.
        """
        audio_q = queue.Queue()
        # Put 3 speech frames with volume above threshold
        loud_chunk = (np.ones(1280, dtype=np.int16) * 5000)
        silent_chunk = np.zeros(1280, dtype=np.int16)

        audio_q.put(loud_chunk)
        audio_q.put(loud_chunk)
        # Followed by silent chunks to trigger silence timeout
        for _ in range(12):
            audio_q.put(silent_chunk)

        result_buffer = wake_word._capture_follow_on_speech(audio_q, initial_chunk=loud_chunk)

        self.assertIsNotNone(result_buffer)
        self.assertIsInstance(result_buffer, io.BytesIO)
        # Check that buffer contains valid WAV header
        header = result_buffer.getvalue()[:4]
        self.assertEqual(header, b"RIFF")

    def test_first_utterance_pause_returns_none_for_acknowledgment(self):
        """
        When user pauses after saying 'Hey Jarvis', follow-on detection times out
        and returns None so Jarvis speaks 'Yes, sir.' acknowledgment.
        """
        audio_q = queue.Queue()
        silent_chunk = np.zeros(1280, dtype=np.int16)

        # Queue only silence
        for _ in range(10):
            audio_q.put(silent_chunk)

        with patch.object(wake_word, "POST_WAKE_SPEECH_WINDOW", 0.05):
            result = wake_word._capture_follow_on_speech(audio_q, initial_chunk=silent_chunk)

        self.assertIsNone(result)

    def test_silence_duration_timeout_is_not_aggressive(self):
        """
        Trailing silence duration allows fast response while avoiding premature cutoffs (>= 0.40s).
        """
        self.assertGreaterEqual(recorder.SILENCE_DURATION, 0.40)
        self.assertGreaterEqual(recorder.MIN_SILENCE_DURATION, 0.40)
