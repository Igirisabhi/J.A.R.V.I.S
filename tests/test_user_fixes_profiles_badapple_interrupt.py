import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from core.session_memory import session
from tools import browser_profiles, browser_controller, system_tools
from tools.window_tools import open_app as window_open_app
from voice import recorder, stt_cleanup, wake_word


class TestUserFixes(unittest.TestCase):
    def setUp(self):
        session.clear()

    # -------------------------------------------------------------------------
    # 1. Decrease Transcribing Time & Silence Duration
    # -------------------------------------------------------------------------
    def test_silence_duration_is_decreased_for_fast_transcription(self):
        """Silence duration should be set to 0.45s to avoid dead air before STT."""
        self.assertLessEqual(recorder.DEFAULT_SILENCE_DURATION, 0.50)
        self.assertLessEqual(recorder.MIN_SILENCE_DURATION, 0.45)

    def test_warm_stt_connection_runs_without_error(self):
        """warm_stt_connection pre-warms TLS handshake in Groq STT."""
        from voice.stt_groq import warm_stt_connection
        # Should execute safely without raising exceptions
        warm_stt_connection()

    # -------------------------------------------------------------------------
    # 2. Browser Profiles: Discovery, Matching & Spoken Commands
    # -------------------------------------------------------------------------
    def test_get_browser_profiles_parses_local_state(self):
        """Verify parsing of Chrome Local State JSON profile.info_cache."""
        mock_local_state = {
            "profile": {
                "info_cache": {
                    "Default": {
                        "name": "Bakka",
                        "user_name": "brabhijith3@gmail.com",
                        "gaia_name": "Bakka Yaro (Abhijith)",
                    },
                    "Profile 1": {
                        "name": "Kushal Majeti",
                        "user_name": "mjkushalchetty@gmail.com",
                        "gaia_name": "Kushal Majeti Jayakumar",
                    },
                }
            }
        }
        with patch("tools.browser_profiles._get_local_state_path") as mock_path:
            mock_file = MagicMock()
            mock_file.open.return_value.__enter__.return_value = io_mock = MagicMock()
            io_mock.read.return_value = json.dumps(mock_local_state)
            # Patch json.load
            with patch("json.load", return_value=mock_local_state):
                profiles = browser_profiles.get_browser_profiles("chrome")

        self.assertIn("Default", profiles)
        self.assertIn("Profile 1", profiles)
        self.assertEqual(profiles["Default"]["name"], "Bakka")
        self.assertEqual(profiles["Profile 1"]["name"], "Kushal Majeti")

    def test_match_browser_profile(self):
        """Verify matching queries against profile directory, names, and aliases."""
        mock_profiles = {
            "Default": {
                "dir": "Default",
                "name": "Bakka",
                "aliases": {"default", "bakka", "0", "profile 0"},
            },
            "Profile 1": {
                "dir": "Profile 1",
                "name": "Kushal Majeti",
                "aliases": {"profile 1", "1", "kushal", "majeti", "kushal majeti"},
            },
        }
        with patch.object(browser_profiles, "get_browser_profiles", return_value=mock_profiles):
            self.assertEqual(browser_profiles.match_browser_profile("bakka", "chrome"), ("Default", "Bakka"))
            self.assertEqual(browser_profiles.match_browser_profile("profile bakka", "chrome"), ("Default", "Bakka"))
            self.assertEqual(browser_profiles.match_browser_profile("kushal", "chrome"), ("Profile 1", "Kushal Majeti"))
            self.assertEqual(browser_profiles.match_browser_profile("profile 1", "chrome"), ("Profile 1", "Kushal Majeti"))
            self.assertIsNone(browser_profiles.match_browser_profile("nonexistent", "chrome"))

    def test_handle_browser_profile_command(self):
        """Verify voice command recognition for browser profiles."""
        with patch.object(browser_profiles, "open_browser_with_profile", return_value="Opening Chrome with profile Bakka.") as mock_open:
            # "open chrome profile bakka"
            res1 = browser_profiles.handle_browser_profile_command("open chrome profile bakka")
            self.assertEqual(res1, "Opening Chrome with profile Bakka.")
            mock_open.assert_called_with("chrome", profile_query="bakka")

            # "open chrome with profile bakka"
            res2 = browser_profiles.handle_browser_profile_command("open chrome with profile bakka")
            self.assertEqual(res2, "Opening Chrome with profile Bakka.")

            # "open chrome bakka profile"
            res3 = browser_profiles.handle_browser_profile_command("open chrome bakka profile")
            self.assertEqual(res3, "Opening Chrome with profile Bakka.")

            # "switch to chrome profile bakka"
            res4 = browser_profiles.handle_browser_profile_command("switch to chrome profile bakka")
            self.assertEqual(res4, "Opening Chrome with profile Bakka.")

    def test_system_tools_routes_browser_profile_command(self):
        """system_tools.handle_command recognizes browser profile voice commands."""
        with patch("tools.browser_profiles.handle_browser_profile_command", return_value="Opening Chrome with profile Bakka.") as mock_prof:
            res = system_tools.handle_command("open chrome profile bakka")
            self.assertEqual(res, "Opening Chrome with profile Bakka.")
            mock_prof.assert_called()

    # -------------------------------------------------------------------------
    # 3. Bad Apple Transcription Cleanup & Autoplay Direct Playback
    # -------------------------------------------------------------------------
    def test_bad_apple_transcription_cleanup(self):
        """Whisper misrecognitions for 'bad apple' are normalized properly."""
        self.assertEqual(stt_cleanup.cleanup_transcription("play pad apple"), "play Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("play bad app"), "play Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("play bad able"), "play Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("play that apple"), "play Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("play bed apple"), "play Bad Apple")

    def test_handle_play_command_routes_bad_apple_directly(self):
        """handle_play_command immediately routes 'bad apple' to YouTube autoplay."""
        with patch("tools.system_tools.play_youtube", return_value="Playing Bad Apple on YouTube.") as mock_play:
            res = system_tools.handle_play_command("play bad apple")
            self.assertEqual(res, "Playing Bad Apple on YouTube.")
            mock_play.assert_called_once_with("bad apple", browser=None)

    def test_browser_controller_play_youtube_direct_autoplay_fallback(self):
        """When CDP debugging is unavailable, play_or_resume_youtube fetches video URL and autoplays."""
        with (
            patch("tools.browser_controller.ensure_chrome_running_with_cdp"),
            patch("tools.browser_controller._bring_chrome_window_to_front"),
            patch("playwright.sync_api.sync_playwright", side_effect=RuntimeError("CDP connection refused")),
            patch("tools.system_tools._fetch_first_youtube_video_url", return_value="https://www.youtube.com/watch?v=FtutLA63Cp8&autoplay=1"),
            patch("tools.browser_controller.navigate_to_url", return_value=True) as mock_nav,
        ):
            ok, msg = browser_controller.play_or_resume_youtube("bad apple")
            self.assertTrue(ok)
            self.assertIn("Playing bad apple on YouTube", msg)
            mock_nav.assert_called_once_with("https://www.youtube.com/watch?v=FtutLA63Cp8&autoplay=1")

    # -------------------------------------------------------------------------
    # 4. Transcription Interrupt / Barge-in
    # -------------------------------------------------------------------------
    def test_transcribe_file_with_interrupt_stops_on_barge_in(self):
        """If user speaks while Jarvis is transcribing, returns empty text and interrupted_file."""
        fake_interrupted_file = Path("voice/interrupts/interrupted_command.wav")
        with patch("voice.thinking_listener.transcribe_with_interrupt", return_value=("", fake_interrupted_file)):
            text, interrupted_file = wake_word.transcribe_file_with_interrupt(Path("fake_audio.wav"))
            self.assertEqual(text, "")
            self.assertEqual(interrupted_file, fake_interrupted_file)

    # -------------------------------------------------------------------------
    # 5. Single-instance Window Reuse
    # -------------------------------------------------------------------------
    def test_open_app_reuses_existing_window_if_running(self):
        """If an application is already open, open_app restores/focuses it instead of launching a new one."""
        with (
            patch("tools.window_tools.find_any_window", return_value=12345),
            patch("tools.window_tools.force_foreground_window", return_value=True) as mock_focus,
            patch("tools.window_tools.user32") as mock_user32,
        ):
            mock_user32.IsIconic.return_value = True
            res = window_open_app("notepad")
            self.assertEqual(res, "Notepad is already open.")
            mock_user32.ShowWindow.assert_called_once_with(12345, 9)  # SW_RESTORE = 9
            mock_focus.assert_called_once_with(12345)

    # -------------------------------------------------------------------------
    # 6. Desktop Barrier & Folder-Scoped File Access
    # -------------------------------------------------------------------------
    def test_desktop_barrier_prevents_accessing_file_in_desktop_subfolder_when_on_desktop(self):
        """When on Desktop, Jarvis must NEVER search into desktop subfolders to open files."""
        import tempfile
        from tools import system_tools

        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop = Path(tmp_dir) / "Desktop"
            desktop.mkdir()
            subfolder = desktop / "MyFolder"
            subfolder.mkdir()
            table_file = subfolder / "table.py"
            table_file.write_text("print('hello table')")

            with (
                patch.dict(system_tools.KNOWN_FOLDERS, {"desktop": desktop}),
                patch.object(system_tools, "get_open_explorer_folder", return_value=str(desktop)),
                patch.object(system_tools, "open_path_safe", return_value=True) as mock_open,
            ):
                system_tools.set_active_folder(str(desktop))
                res = system_tools.handle_command("open table.py")
                # Must refuse to open and tell user to open the folder first
                self.assertIn("Open the folder first", res)
                mock_open.assert_not_called()

    def test_file_opens_successfully_when_folder_is_opened(self):
        """When user is in the folder containing table.py, 'open table.py' and 'open file' must open it."""
        import tempfile
        from tools import system_tools

        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop = Path(tmp_dir) / "Desktop"
            desktop.mkdir()
            subfolder = desktop / "MyFolder"
            subfolder.mkdir()
            table_file = subfolder / "table.py"
            table_file.write_text("print('hello table')")

            with (
                patch.dict(system_tools.KNOWN_FOLDERS, {"desktop": desktop}),
                patch.object(system_tools, "get_open_explorer_folder", return_value=str(subfolder)),
                patch.object(system_tools, "open_path_safe", return_value=True) as mock_open,
            ):
                system_tools.set_active_folder(str(subfolder))

                # User says "open table.py" while inside MyFolder
                res1 = system_tools.handle_command("open table.py")
                self.assertIn(res1, {"Opened table.py.", "Opening table.py."})
                mock_open.assert_called_with(str(table_file))

                # User says "open file" while inside MyFolder
                mock_open.reset_mock()
                res2 = system_tools.handle_command("open file")
                self.assertEqual(res2, "Opening table.py.")
                mock_open.assert_called_with(str(table_file))

    def test_find_file_in_hierarchy_desktop_barrier(self):
        """find_file_in_hierarchy never returns subfolder files when active_folder is Desktop."""
        import tempfile
        from tools.system_tools import find_file_in_hierarchy

        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop = Path(tmp_dir) / "Desktop"
            desktop.mkdir()
            subfolder = desktop / "MyFolder"
            subfolder.mkdir()
            table_file = subfolder / "table.py"
            table_file.write_text("print('hello table')")
            direct_file = desktop / "direct.txt"
            direct_file.write_text("hello direct")

            with patch.dict(system_tools.KNOWN_FOLDERS, {"desktop": desktop}):
                # From Desktop: table.py must NOT be found
                hit_table = find_file_in_hierarchy("table.py", str(desktop))
                self.assertIsNone(hit_table)

                # From Desktop: direct file on desktop IS found
                hit_direct = find_file_in_hierarchy("direct.txt", str(desktop))
                self.assertIsNotNone(hit_direct)
                self.assertEqual(Path(hit_direct).resolve(), direct_file.resolve())

                # From MyFolder: table.py IS found
                hit_inside = find_file_in_hierarchy("table.py", str(subfolder))
                self.assertIsNotNone(hit_inside)
                self.assertEqual(Path(hit_inside).resolve(), table_file.resolve())

    def test_command_engine_file_scoped_to_active_folder(self):
        """command_engine Action(verb='open', target='table.py') honors desktop barrier and folder scoping."""
        import tempfile
        from core.command_engine import Action, execute_action
        from tools import system_tools

        with tempfile.TemporaryDirectory() as tmp_dir:
            desktop = Path(tmp_dir) / "Desktop"
            desktop.mkdir()
            subfolder = desktop / "MyFolder"
            subfolder.mkdir()
            table_file = subfolder / "table.py"
            table_file.write_text("print('hello table')")

            with (
                patch.dict(system_tools.KNOWN_FOLDERS, {"desktop": desktop}),
                patch.object(system_tools, "open_path_safe", return_value=True),
            ):
                # On Desktop: must fail
                with patch.object(system_tools, "get_open_explorer_folder", return_value=str(desktop)):
                    system_tools.set_active_folder(str(desktop))
                    action = Action(verb="open", target="table.py")
                    ok, msg = execute_action(action, context={})
                    self.assertFalse(ok)
                    self.assertIn("on the desktop. Open the folder first", msg)

                # Inside MyFolder: must succeed
                with patch.object(system_tools, "get_open_explorer_folder", return_value=str(subfolder)):
                    system_tools.set_active_folder(str(subfolder))
                    action = Action(verb="open", target="table.py")
                    ok, msg = execute_action(action, context={})
                    self.assertTrue(ok)
                    self.assertEqual(msg, "Opened table.py.")

    # -------------------------------------------------------------------------
    # 7. Self-Listening Prevention & Interruption Loop Fixes
    # -------------------------------------------------------------------------
    def test_tts_is_speaking_state_and_thinking_listener_suppression(self):
        """While TTS is speaking, ThinkingInterruptListener must ignore mic input to prevent self-interruption."""
        import numpy as np
        from voice import tts
        from voice.thinking_listener import ThinkingInterruptListener

        listener = ThinkingInterruptListener()
        listener.running = True
        fake_audio_frame = np.full((512, 1), 0.5, dtype=np.float32)  # Loud frame

        with patch.object(tts, "is_speaking", return_value=True):
            # Callback should return early without setting speech_detected or recording
            listener.callback(fake_audio_frame, 512, None, None)
            self.assertFalse(listener.speech_detected)
            self.assertFalse(listener.interrupted)
            self.assertEqual(len(listener.audio_chunks), 0)

        with patch.object(tts, "is_speaking", return_value=False):
            # When not speaking, loud frames are processed
            listener.callback(fake_audio_frame, 512, None, None)
            self.assertTrue(listener.speech_detected)

    def test_play_audio_interruptible_returns_none_on_normal_completion(self):
        """When TTS completes without barge-in, play_audio_interruptible returns None (not Jarvis's own voice)."""
        import numpy as np
        from voice import tts

        mock_listener = MagicMock()
        mock_listener.start.return_value = True
        mock_listener.interrupted = False
        mock_listener.speech_detected = False
        fake_audio = np.zeros(1600, dtype=np.float32)

        mock_stream = MagicMock(active=False)
        with (
            patch("soundfile.read", return_value=(fake_audio, 16000)),
            patch("voice.tts.BargeInListener", return_value=mock_listener),
            patch("sounddevice.play"),
            patch("sounddevice.get_stream", return_value=mock_stream),
        ):
            result = tts.play_audio_interruptible("fake_file.wav")
            self.assertIsNone(result)

    # -------------------------------------------------------------------------
    # 8. First Video on YouTube Playback
    # -------------------------------------------------------------------------
    def test_first_video_command_parsing(self):
        """Verify various phrasings for first video all parse to play_first_video."""
        from core.command_engine import parse_action

        for phrase in [
            "first video",
            "the first video",
            "play first video",
            "play the first video",
            "play first video in yt",
            "play first video on youtube",
            "open first video",
            "open the first video",
            "open first video in yt",
            "open first video on youtube",
            "watch first video",
            "click the first video",
        ]:
            action = parse_action(phrase)
            self.assertIsNotNone(action, f"Failed to parse: {phrase}")
            self.assertEqual(action.verb, "play_first_video", f"Expected play_first_video for {phrase}, got {action.verb}")

    def test_first_video_execution_fallback_to_direct_url(self):
        """When CDP is unattached, handle_youtube_command for 'first video' uses direct URL fallback."""
        with (
            patch("tools.browser_controller.click_link_by_ordinal", return_value=(False, "I couldn't connect to the active Chrome tab.")),
            patch("tools.system_tools._fetch_youtube_video_urls", return_value=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]),
            patch("tools.system_tools._youtube_browser_target", return_value="chrome"),
            patch("tools.system_tools.open_url", return_value=True) as mock_open_url,
        ):
            session.youtube_search_query = "bad apple"
            res = system_tools.handle_youtube_command("play first video")
            self.assertEqual(res, "Playing YouTube result 1.")
            mock_open_url.assert_called_once_with("https://www.youtube.com/watch?v=dQw4w9WgXcQ&autoplay=1", browser="chrome", new_tab=True)

    # -------------------------------------------------------------------------
    # 9. Direct Browser Profile Commands
    # -------------------------------------------------------------------------
    def test_direct_profile_name_routing(self):
        """Commands like 'bakka', 'open bakka', 'bakka profile', and compound 'open chrome and tell it to open bakka profile' must launch the profile."""
        with (
            patch("tools.browser_profiles.open_browser_with_profile", return_value="Opening Chrome with profile Bakka.") as mock_open,
            patch("tools.browser_profiles.match_browser_profile", return_value=("Default", "Bakka")),
            patch("tools.browser_profiles.find_profile_across_browsers", return_value=("chrome", "Default", "Bakka")),
        ):
            for phrase in [
                "bakka",
                "open bakka",
                "bakka profile",
                "open bakka profile",
                "tell it to open bakka profile",
                "open chrome and tell it to open bakka profile",
            ]:
                res = browser_profiles.handle_browser_profile_command(phrase)
                self.assertEqual(res, "Opening Chrome with profile Bakka.", f"Failed on phrase: {phrase}")

    def test_open_app_routes_profile_commands(self):
        """window_tools.open_app routes profile names and profile commands before single-instance check."""
        with patch("tools.browser_profiles.handle_browser_profile_command", return_value="Opening Chrome with profile Bakka.") as mock_prof:
            res = window_open_app("bakka")
            self.assertEqual(res, "Opening Chrome with profile Bakka.")
            mock_prof.assert_called()

    # -------------------------------------------------------------------------
    # 10. Bakka Mispronunciation & Search Bad Apple Broken Link Fixes
    # -------------------------------------------------------------------------
    def test_bakka_mispronunciation_cleanup_and_routing(self):
        """When user says 'bakka', STT mishearings ('backup', 'back up', 'baka') are normalized to Bakka."""
        self.assertEqual(stt_cleanup.cleanup_transcription("backup"), "Bakka")
        self.assertEqual(stt_cleanup.cleanup_transcription("back up"), "Bakka")
        self.assertEqual(stt_cleanup.cleanup_transcription("baka"), "Bakka")
        self.assertEqual(stt_cleanup.cleanup_transcription("backup profile"), "Bakka profile")
        self.assertEqual(stt_cleanup.cleanup_transcription("open backup"), "open Bakka profile")
        self.assertEqual(stt_cleanup.cleanup_transcription("open backup profile"), "open Bakka profile")
        self.assertEqual(stt_cleanup.cleanup_transcription("switch to backup"), "switch to Bakka profile")

    def test_search_bad_apple_cleanup_and_routing(self):
        """'search bad apple' and mishearings are cleaned and routed cleanly to YouTube search."""
        self.assertEqual(stt_cleanup.cleanup_transcription("search bad apple"), "search Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("search pad apple"), "search Bad Apple")
        self.assertEqual(stt_cleanup.cleanup_transcription("search bad app"), "search Bad Apple")

        with patch("tools.system_tools.search_youtube", return_value="Searching YouTube for Bad Apple.") as mock_search:
            res = system_tools.handle_command("search bad apple")
            self.assertEqual(res, "Searching YouTube for Bad Apple.")
            mock_search.assert_called_with("bad apple", browser="chrome")

    def test_broken_file_s_url_sanitization_and_recovery(self):
        """file:///S:// and s:// broken YouTube URLs from address bar miskeys are sanitized cleanly."""
        broken_url = "file:///S://www.youtube.com/results%3Fsearch_query=Bad+Apple"
        self.assertEqual(browser_controller.clean_search_query(broken_url), "Bad Apple")

        broken_s_url = "s://www.youtube.com/results?search_query=Bad+Apple"
        self.assertEqual(browser_controller.clean_search_query(broken_s_url), "Bad Apple")

        with (
            patch("tools.browser_controller.is_chrome_running", return_value=True),
            patch("tools.browser_controller._cdp_manager.get_browser", return_value=None),
            patch("tools.system_tools._navigate_current_browser_tab", return_value=True) as mock_nav,
        ):
            browser_controller.navigate_to_url(broken_url)
            # Must sanitize the file:///S:// prefix to https://
            mock_nav.assert_called_once_with("https://www.youtube.com/results%3Fsearch_query=Bad+Apple", browser="chrome")

    def test_whisper_prompt_includes_bakka_profile(self):
        """Whisper vocabulary prompt in stt_groq includes profile Bakka."""
        from voice.stt_groq import WHISPER_PROMPT
        self.assertIn("Bakka", WHISPER_PROMPT)

    def test_all_json_files_connected_and_synchronized(self):
        """Verify aliases.json, command_corrections.json, and app_tools are bidirectionally connected."""
        from tools import app_tools
        from voice import stt_cleanup

        # 1. load_aliases loads from both aliases.json and command_corrections.json
        aliases = app_tools.load_aliases()
        self.assertIn("backup", aliases)
        self.assertEqual(aliases["backup"], "bakka")

        # 2. stt_cleanup loads from both aliases.json and command_corrections.json
        cleaned = stt_cleanup.cleanup_transcription("open backup")
        self.assertEqual(cleaned, "open Bakka profile")

    def test_execute_dsl_resolves_connected_aliases_and_profiles(self):
        """execute_dsl allows launching via connected aliases and browser profiles without allowlist rejection."""
        from core.command_engine import execute_dsl
        with (
            patch("core.command_engine.open_app", return_value="Opening Chrome with profile Bakka.") as mock_open,
            patch("tools.browser_profiles.find_profile_across_browsers", return_value=("chrome", "Default", "Bakka")),
        ):
            # Bakka is a browser profile
            ok, msg = execute_dsl("launch:bakka", context={})
            self.assertTrue(ok)
            self.assertEqual(msg, "Opening Chrome with profile Bakka.")
            mock_open.assert_called_with("bakka")

            # crome is an alias to chrome in aliases.json
            mock_open.return_value = "Chrome is already open."
            ok2, msg2 = execute_dsl("launch:crome", context={})
            self.assertTrue(ok2)
            self.assertEqual(msg2, "Chrome is already open.")


if __name__ == "__main__":
    unittest.main()




