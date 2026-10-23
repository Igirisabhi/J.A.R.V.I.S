import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from voice import tts


class TTSVoiceProfileTests(unittest.TestCase):
    def test_voice_profiles_dictionary(self):
        """Ensure all 3 voice profiles are properly mapped for each engine."""
        self.assertIn("christopher", tts.VOICE_PROFILES)
        self.assertIn("guy", tts.VOICE_PROFILES)
        self.assertIn("sonia", tts.VOICE_PROFILES)

        self.assertEqual(tts.VOICE_PROFILES["christopher"]["groq_voice"], "troy")
        self.assertEqual(tts.VOICE_PROFILES["christopher"]["gemini_voice"], "Puck")
        self.assertEqual(tts.VOICE_PROFILES["christopher"]["local_voice"], "bm_george")

        self.assertEqual(tts.VOICE_PROFILES["guy"]["groq_voice"], "austin")
        self.assertEqual(tts.VOICE_PROFILES["guy"]["gemini_voice"], "Fenrir")
        self.assertEqual(tts.VOICE_PROFILES["guy"]["local_voice"], "am_adam")

        self.assertEqual(tts.VOICE_PROFILES["sonia"]["groq_voice"], "diana")
        self.assertEqual(tts.VOICE_PROFILES["sonia"]["gemini_voice"], "Kore")
        self.assertEqual(tts.VOICE_PROFILES["sonia"]["local_voice"], "bf_emma")

    def test_speak_passes_voice_profile_to_chunks(self):
        """speak() should resolve voice_profile and pass overrides to speak_chunk."""
        with patch.object(tts, "speak_chunk") as mock_chunk:
            mock_chunk.return_value = None
            tts.speak("Testing Guy voice.", voice_profile="guy", speech_rate=1.2, volume=0.9)

            mock_chunk.assert_called_once()
            _, kwargs = mock_chunk.call_args
            self.assertEqual(kwargs["groq_voice"], "austin")
            self.assertEqual(kwargs["gemini_voice"], "Fenrir")
            self.assertEqual(kwargs["local_voice"], "am_adam")
            self.assertEqual(kwargs["speech_rate"], 1.2)
            self.assertEqual(kwargs["volume"], 0.9)

    def test_speak_sonia_profile(self):
        """speak() should correctly resolve Sonia profile."""
        with patch.object(tts, "speak_chunk") as mock_chunk:
            mock_chunk.return_value = None
            tts.speak("Testing Sonia voice.", voice_profile="sonia")

            mock_chunk.assert_called_once()
            _, kwargs = mock_chunk.call_args
            self.assertEqual(kwargs["groq_voice"], "diana")
            self.assertEqual(kwargs["gemini_voice"], "Kore")
            self.assertEqual(kwargs["local_voice"], "bf_emma")

    def test_groq_voice_normalization(self):
        """Groq generation should normalize voice aliases to supported Orpheus voices."""
        with patch.object(tts, "_get_groq_tts_client") as mock_client, patch.object(
            tts, "_validate_generated_audio"
        ) as mock_validate:
            client_instance = Mock()
            mock_client.return_value = client_instance

            # Guy -> austin
            tts._generate_with_groq("Hello", Path("test.wav"), voice="guy")
            call_kwargs = client_instance.audio.speech.create.call_args.kwargs
            self.assertEqual(call_kwargs["voice"], "austin")

            # Sonia -> diana
            tts._generate_with_groq("Hello", Path("test.wav"), voice="sonia")
            call_kwargs = client_instance.audio.speech.create.call_args.kwargs
            self.assertEqual(call_kwargs["voice"], "diana")

            # Christopher -> troy
            tts._generate_with_groq("Hello", Path("test.wav"), voice="christopher")
            call_kwargs = client_instance.audio.speech.create.call_args.kwargs
            self.assertEqual(call_kwargs["voice"], "troy")

    def test_gemini_voice_normalization(self):
        """Gemini generation should normalize voice aliases to supported Gemini voices."""
        with patch.object(tts, "_get_gemini_tts_client") as mock_client, patch.object(
            tts, "_validate_generated_audio"
        ) as mock_validate:
            client_instance = Mock()
            mock_client.return_value = client_instance
            mock_interaction = Mock()
            mock_interaction.output_audio = Mock(data="ZHVtbXk=")
            client_instance.interactions.create.return_value = mock_interaction

            # Guy -> Fenrir
            tts._generate_with_gemini("Hello", Path("test.wav"), voice="guy")
            call_kwargs = client_instance.interactions.create.call_args.kwargs
            self.assertEqual(call_kwargs["generation_config"]["speech_config"][0]["voice"], "Fenrir")

            # Sonia -> Kore
            tts._generate_with_gemini("Hello", Path("test.wav"), voice="sonia")
            call_kwargs = client_instance.interactions.create.call_args.kwargs
            self.assertEqual(call_kwargs["generation_config"]["speech_config"][0]["voice"], "Kore")

            # Christopher -> Puck
            tts._generate_with_gemini("Hello", Path("test.wav"), voice="christopher")
            call_kwargs = client_instance.interactions.create.call_args.kwargs
            self.assertEqual(call_kwargs["generation_config"]["speech_config"][0]["voice"], "Puck")

    def test_worker_voice_normalization(self):
        """Kokoro worker should receive normalized local voices."""
        with patch.object(tts, "start_tts_worker") as start_worker, patch.object(
            tts.threading, "Event"
        ) as event_factory:
            worker = Mock()
            worker.poll.return_value = None
            worker.stdin = Mock()
            start_worker.return_value = worker
            event_factory.return_value.wait.return_value = True

            # Guy -> am_adam
            tts._generate_with_worker("hello", Path("out.wav"), voice="guy")
            request = json.loads(worker.stdin.write.call_args.args[0])
            self.assertEqual(request["voice"], "am_adam")

            # Sonia -> bf_emma
            tts._generate_with_worker("hello", Path("out.wav"), voice="sonia")
            request = json.loads(worker.stdin.write.call_args.args[0])
            self.assertEqual(request["voice"], "bf_emma")

            # Christopher -> bm_george
            tts._generate_with_worker("hello", Path("out.wav"), voice="christopher")
            request = json.loads(worker.stdin.write.call_args.args[0])
            self.assertEqual(request["voice"], "bm_george")


if __name__ == "__main__":
    unittest.main()
