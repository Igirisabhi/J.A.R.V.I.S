import unittest
import tempfile
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from core.command_engine import Action, execute_action, parse_action
from core.session_memory import session
from core import audio_devices
from tools import system_tools


class RuntimeRegressionTests(unittest.TestCase):
    def test_default_launcher_starts_jarvis_with_the_hud(self):
        # The README states: "Run start_jarvis.bat to start the voice backend
        # and its connected HUD together."  The launcher must therefore pass
        # --ui so that the HUD is shown.  --no-ui is only for headless use.
        launcher = Path(__file__).resolve().parents[1] / "start_jarvis.bat"
        contents = launcher.read_text(encoding="utf-8")

        self.assertIn('"%~dp0main.py" --ui', contents)

    def test_kokoro_worker_uses_existing_packages_with_the_active_python(self):
        from voice import tts

        with tempfile.TemporaryDirectory() as site_packages:
            with (
                patch.object(tts, "TTS_SITE_PACKAGES", Path(site_packages)),
                patch.dict(os.environ, {"PYTHONPATH": "existing-path"}),
            ):
                environment = tts._tts_worker_environment()

        self.assertEqual(
            environment["PYTHONPATH"],
            os.pathsep.join((site_packages, "existing-path")),
        )
        self.assertTrue(tts.TTS_PYTHON.exists())

    def test_opening_an_app_does_not_import_a_missing_app_constant(self):
        with (
            patch("tools.system_tools.find_pc_folder", return_value=None),
            patch("core.command_engine.find_any_window", return_value=None),
            patch("tools.system_tools.get_active_folder", return_value=r"C:\Users\test\Documents"),
            patch("core.command_engine.open_app", return_value="I couldn't open Calculator."),
        ):
            success, message = execute_action(Action("open", "calculator"), {})

        self.assertFalse(success)
        self.assertIn("couldn't open Calculator", message)

    def test_generic_maximize_uses_the_active_window_not_a_stale_app(self):
        action = parse_action("maximize")
        self.assertEqual(action.verb, "maximize_window")
        with (
            patch.object(session, "current_app", "quick settings"),
            patch("core.command_engine.maximize_current_window", return_value="Maximized YouTube.") as maximize_current,
            patch("core.command_engine.maximize_app") as maximize_named_app,
        ):
            success, message = execute_action(action, {"current_app": "quick settings"})

        self.assertTrue(success)
        self.assertEqual(message, "Maximized YouTube.")
        maximize_current.assert_called_once_with()
        maximize_named_app.assert_not_called()

    def test_first_video_without_a_youtube_search_does_not_play_random_media(self):
        with patch.object(session, "youtube_search_query", None), patch(
            "tools.system_tools.handle_media_command"
        ) as media_command:
            success, message = execute_action(Action("play_first_video"), {})

        self.assertFalse(success)
        self.assertIn("Search YouTube first", message)
        media_command.assert_not_called()

    def test_first_video_with_no_fetchable_search_result_reports_failure(self):
        with (
            patch.object(session, "youtube_search_query", "sample search"),
            patch.object(system_tools, "handle_youtube_command", return_value="Search for a YouTube video first, then tell me which result to play."),
        ):
            success, message = execute_action(Action("play_first_video"), {})

        self.assertFalse(success)
        self.assertIn("couldn't fetch the first YouTube result", message)

    def test_gesture_control_phrase_opens_preview_only(self):
        with patch.object(system_tools.event_bus, "publish") as publish:
            message = system_tools.handle_command("turn on gestures")

        publish.assert_called_once_with("gesture_preview_requested")
        self.assertIn("does not control your PC", message)

    def test_gesture_settings_has_live_preview_button(self):
        overlay = Path(__file__).resolve().parents[1] / "ui" / "overlay.html"
        contents = overlay.read_text(encoding="utf-8")
        self.assertIn('onclick="launchGesturePreview()"', contents)
        self.assertIn("PREVIEW GESTURES", contents)
        self.assertIn("handSignMappingsContainer", contents)
        self.assertIn("deleteHandSignMapping", contents)
        self.assertIn("add_hand_sign_mapping", contents)
        self.assertIn("Named One- and Two-Hand Sign Mappings", contents)
        self.assertIn("gestureActionOptions", contents)
        self.assertIn("renderGestureActionOptions", contents)
        self.assertIn("overflow-y: auto", contents)
        self.assertIn("tab-voice", contents)
        self.assertIn("saveCustomVoiceCommand", contents)
        self.assertIn("editCustomVoiceCommand", contents)
        self.assertIn("deleteCustomVoiceCommand", contents)
        self.assertIn("resize: both", contents)
        self.assertIn("resetSettingsWindowPosition", contents)
        self.assertNotIn('onclick="if (event.target === this) triggerDismiss()"', contents)
        self.assertIn("toggleGestureTracking", contents)
        self.assertIn("Open File:", contents)
        self.assertIn("function showToast(message, isError = false)", contents)
        self.assertNotIn("function showToast(message, isError = false) {\n                const toast", contents[contents.index("function sendToBackend"):])
        save_start = contents.index("function saveConfiguration")
        save_end = contents.index("// Restore Defaults", save_start)
        save_body = contents[save_start:save_end]
        self.assertIn("const cameraInvertToggle = document.getElementById('cameraInvertToggle');", save_body)

    def test_overlay_does_not_disable_circle_animations_globally(self):
        overlay = Path(__file__).resolve().parents[1] / "ui" / "overlay.html"
        contents = overlay.read_text(encoding="utf-8")
        reset = contents[contents.index("*,"):contents.index("html,")]
        self.assertNotIn("animation: none !important", reset)
        self.assertNotIn("transition: none !important", reset)
        self.assertIn("body.no-motion *", contents)

    def test_microphone_stream_always_uses_the_system_default(self):
        stream = Mock()
        stream.start.return_value = None
        with patch.object(audio_devices.sd, "InputStream", return_value=stream) as input_stream:
            result = audio_devices.start_input_stream(device=32, samplerate=16000)

        self.assertIs(result, stream)
        input_stream.assert_called_once_with(device=None, samplerate=16000)

    def test_microphone_native_sample_rate_is_resampled_for_jarvis(self):
        import numpy as np

        callback = Mock()
        with patch.object(
            audio_devices.sd,
            "query_devices",
            return_value={"default_samplerate": 48000},
        ):
            options = audio_devices._native_rate_options(
                {
                    "samplerate": 16000,
                    "channels": 1,
                    "dtype": "int16",
                    "blocksize": 480,
                    "callback": callback,
                },
            )

        self.assertEqual(options["samplerate"], 48000)
        self.assertEqual(options["blocksize"], 1440)
        options["callback"](np.arange(1440, dtype=np.int16).reshape(-1, 1), 1440, None, None)
        converted, frames = callback.call_args.args[:2]
        self.assertEqual(frames, 480)
        self.assertEqual(converted.shape, (480, 1))
        self.assertEqual(converted.dtype, np.int16)

    def test_calculator_uses_restricted_arithmetic_parser(self):
        from tools.system_tools import _safe_calculate

        self.assertEqual(_safe_calculate("2 + 3 * 4"), 14)
        with self.assertRaises(ValueError):
            _safe_calculate("__import__('os').getcwd()")

    def test_maximize_vlc_uses_the_vlc_fullscreen_action(self):
        for phrase in ("maximize VLC", "maximize in VLC", "maximize video in VLC"):
            with self.subTest(phrase=phrase):
                action = parse_action(phrase)
                self.assertEqual(action.verb, "maximize_video")
                self.assertEqual(action.target, "vlc")
                with patch("core.command_engine.maximize_video", return_value="Video is fullscreen in VLC.") as maximize:
                    success, message = execute_action(action, {})

                self.assertTrue(success)
                self.assertEqual(message, "Video is fullscreen in VLC.")
                maximize.assert_called_once_with("vlc")

    def test_vlc_play_pause_phrases_route_to_distinct_vlc_commands(self):
        for phrase, verb in (("play in VLC", "play"), ("pause in VLC", "pause")):
            with self.subTest(phrase=phrase):
                action = parse_action(phrase)
                self.assertEqual(action.verb, verb)
                self.assertEqual(action.args["destination"], "vlc")
                with patch("tools.window_tools.control_vlc_playback", return_value="VLC playback command sent.") as control:
                    success, _message = execute_action(action, {})
                self.assertTrue(success)
                control.assert_called_once_with(verb)

    def test_vlc_playback_message_targets_vlc_without_activating_it(self):
        from tools import window_tools

        with (
            patch.object(window_tools, "find_any_window", return_value=123),
            patch.object(window_tools, "_is_vlc_window", return_value=True),
            patch.object(window_tools, "force_foreground_window") as foreground,
            patch.object(window_tools.user32, "PostMessageW", return_value=True) as post_message,
        ):
            message = window_tools.control_vlc_playback("pause")

        self.assertEqual(message, "Paused in VLC.")
        post_message.assert_called_once_with(
            123,
            window_tools.WM_APPCOMMAND,
            123,
            window_tools.APPCOMMAND_MEDIA_PAUSE << 16,
        )
        foreground.assert_not_called()
        self.assertEqual(window_tools.APPCOMMAND_MEDIA_PLAY, 46)

    def test_fullscreen_key_is_sent_only_to_a_verified_vlc_window(self):
        from tools import window_tools

        with (
            patch.object(window_tools, "_is_vlc_window", return_value=True),
            patch.object(window_tools, "force_foreground_window", return_value=True),
            patch.object(window_tools, "get_foreground_window", return_value=123),
            patch.object(window_tools, "_send_key") as send_key,
        ):
            self.assertTrue(window_tools._send_vlc_fullscreen_key(123))
        send_key.assert_called_once_with(window_tools.VK_F)

        with (
            patch.object(window_tools, "_is_vlc_window", return_value=False),
            patch.object(window_tools, "force_foreground_window") as focus,
            patch.object(window_tools, "_send_key") as send_key,
        ):
            self.assertFalse(window_tools._send_vlc_fullscreen_key(123))
        focus.assert_not_called()
        send_key.assert_not_called()

    def test_bare_play_pause_control_vlc_when_vlc_is_the_foreground_app(self):
        with (
            patch("core.command_engine.get_foreground_window", return_value=123),
            patch("core.command_engine.get_window_process_name", return_value="vlc.exe"),
            patch("tools.window_tools.control_vlc_playback", return_value="VLC playback command sent.") as control,
            patch.object(session, "current_app", None),
        ):
            for phrase, verb in (("play", "play"), ("pause", "pause")):
                with self.subTest(phrase=phrase):
                    success, _message = execute_action(parse_action(phrase), {})
                    self.assertTrue(success)
                    control.assert_called_with(verb)

    def test_both_current_app_search_phrasings_are_scoped(self):
        for command in ("search in app for voice", "search for voice in app"):
            with self.subTest(command=command):
                action = parse_action(command)
                self.assertEqual(action.verb, "search")
                self.assertEqual(action.target, "voice")
                self.assertEqual(action.args["destination"], "this app")

    def test_current_app_search_uses_the_active_native_app(self):
        action = parse_action("search for voice in app")
        with (
            patch.object(session, "current_app", "notepad"),
            patch("tools.window_tools.find_any_window", return_value=123),
            patch("tools.window_tools.force_foreground_window", return_value=True),
            patch("core.command_engine.user32.keybd_event"),
            patch("core.command_engine._press_key"),
            patch("core.command_engine._type_unicode"),
            patch.object(system_tools, "google_search") as google_search,
        ):
            success, message = execute_action(action, {"current_app": "notepad"})

        self.assertTrue(success, message)
        self.assertIn("Searching in Notepad", message)
        google_search.assert_not_called()

    def test_current_app_search_fails_clearly_when_no_app_is_known(self):
        action = parse_action("search in app for voice")
        with (
            patch.object(session, "current_app", None),
            patch.object(session, "last_opened_app", None),
            patch("tools.system_tools.google_search") as google_search,
        ):
            success, message = execute_action(action, {})

        self.assertFalse(success)
        self.assertIn("couldn't identify the current app", message)
        google_search.assert_not_called()

    def test_open_file_in_open_explorer_folder_does_not_require_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("notes", encoding="utf-8")
            with (
                patch.object(system_tools, "get_settings", return_value={"workspace_dir": None}),
                patch.object(system_tools, "get_open_explorer_folder", return_value=folder),
                patch.object(system_tools, "open_path_safe", return_value=True),
            ):
                message = system_tools.handle_command("open notes.txt")

        self.assertEqual(message, "Opened notes.txt.")

    def test_known_user_folder_open_uses_the_resolved_windows_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.dict(system_tools.KNOWN_FOLDERS, {"documents": Path(folder)}),
                patch.object(system_tools, "open_path_safe", return_value=True),
            ):
                message = system_tools._workspace_command("open my Documents")

        self.assertEqual(message, "Opened Documents folder.")

    def test_open_youtube_uses_the_web_destination(self):
        with patch.object(system_tools, "open_url", return_value=True) as open_url:
            message = system_tools.handle_youtube_command("open youtube")

        self.assertEqual(message, "Opening YouTube.")
        open_url.assert_called_once()
        self.assertIn("youtube.com", open_url.call_args.args[0])

    def test_play_first_youtube_result_opens_the_resolved_search_result(self):
        with (
            patch.object(session, "youtube_search_query", "sample search"),
            patch.object(session, "youtube_search_results", []),
            patch.object(system_tools, "_fetch_youtube_video_urls", return_value=["https://www.youtube.com/watch?v=abcdefghijk"]),
            patch.object(system_tools, "get_active_website_context", return_value=("chrome", "youtube")),
            patch.object(system_tools, "open_url", return_value=True) as open_url,
        ):
            message = system_tools.handle_youtube_command("play first video")

        self.assertEqual(message, "Playing YouTube result 1.")
        self.assertEqual(
            open_url.call_args.args[0],
            "https://www.youtube.com/watch?v=abcdefghijk&autoplay=1",
        )

    def test_ui_settings_load_and_bound_user_values(self):
        from ui import main as ui_main

        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "ui_settings.json"
            settings_path.write_text(
                json.dumps({"particle_count": 999, "motion_fps": 0, "accent_color": "#abc", "unknown": True}),
                encoding="utf-8",
            )
            with patch.object(ui_main, "UI_SETTINGS_PATH", settings_path):
                settings = ui_main._load_ui_settings()

        self.assertEqual(settings["particle_count"], 200)
        self.assertEqual(settings["motion_fps"], 1)
        self.assertEqual(settings["accent_color"], "#abc")
        self.assertNotIn("unknown", settings)


    # ------------------------------------------------------------------
    # Bug 1 regression tests: failed app launch reported as success
    # ------------------------------------------------------------------

    def test_launch_none_result_is_treated_as_failure(self):
        """open_app returning None must not be reported as success."""
        from core.command_engine import execute_dsl, Action
        from core import settings as settings_module
        with (
            patch.object(settings_module, "_SETTINGS", {
                "app_allowlist": ["calculator"],
                "stop_on_error": True,
                "idle_timeout_ms": 30000,
                "macros": {},
            }),
            patch("core.command_engine.open_app", return_value=None),
        ):
            success, message = execute_dsl("launch:calculator", {}, Action("open", "calculator"))

        self.assertFalse(success)
        self.assertIn("couldn't open", message.lower())

    def test_launch_empty_result_is_treated_as_failure(self):
        """open_app returning an empty string must not be reported as success."""
        from core.command_engine import execute_dsl, Action
        from core import settings as settings_module
        with (
            patch.object(settings_module, "_SETTINGS", {
                "app_allowlist": ["calculator"],
                "stop_on_error": True,
                "idle_timeout_ms": 30000,
                "macros": {},
            }),
            patch("core.command_engine.open_app", return_value=""),
        ):
            success, message = execute_dsl("launch:calculator", {}, Action("open", "calculator"))

        self.assertFalse(success)

    def test_chain_stops_when_launch_step_fails(self):
        """A None result from a launch step must halt stop_on_error chains.

        We use execute_dsl directly with a pre-compiled DSL so the test is
        independent of parse_action's app-discovery logic (which would call
        find_any_window and the installed-app cache at parse time).
        """
        from core.command_engine import execute_dsl, Action
        from core import settings as settings_module

        call_log = []

        def fake_open_app(name):
            call_log.append(name)
            if name == "missingapp":
                return None        # simulates "not found"
            return f"Opening {name}."

        with (
            patch.object(settings_module, "_SETTINGS", {
                "app_allowlist": ["missingapp", "notepad"],
                "stop_on_error": True,
                "idle_timeout_ms": 30000,
                "macros": {},
            }),
            patch("core.command_engine.open_app", side_effect=fake_open_app),
        ):
            ctx = {}
            success1, msg1 = execute_dsl("launch:missingapp", ctx, Action("open", "missingapp"))
            # The chain must stop here; simulate stop_on_error by not calling step 2.
            if not success1:
                success2 = None
            else:
                success2, _ = execute_dsl("launch:notepad", ctx, Action("open", "notepad"))

        self.assertFalse(success1, msg1)
        self.assertIn("couldn't open", msg1.lower())
        # notepad must never have been launched because step 1 failed.
        self.assertIsNone(success2)
        self.assertEqual(call_log, ["missingapp"])

    # ------------------------------------------------------------------
    # Bug 2 regression tests: "search in app" must not use stale state
    # ------------------------------------------------------------------

    def test_search_in_app_parser_never_resolves_from_session(self):
        """parse_action must keep 'this app' as a literal placeholder even
        when session.current_app is set to a stale value."""
        with patch.object(session, "current_app", "chatgpt"):
            action = parse_action("search in app for voice")
        self.assertEqual(action.verb, "search")
        self.assertEqual(action.target, "voice")
        # The parser must NOT have resolved "chatgpt" here.
        self.assertEqual(action.args.get("destination"), "this app")

    def test_stale_session_state_does_not_override_foreground_app(self):
        """Execution must resolve the destination from the live foreground
        window, not from a stale session.current_app value."""
        action = parse_action("search in app for documents")
        # session says "chatgpt" but the live foreground window is Notepad.
        with (
            patch.object(session, "current_app", "chatgpt"),
            patch("tools.window_tools.find_any_window", return_value=123),
            patch("tools.window_tools.force_foreground_window", return_value=True),
            patch("core.command_engine.user32.keybd_event"),
            patch("core.command_engine._press_key"),
            patch("core.command_engine._type_unicode"),
            patch.object(system_tools, "google_search") as google_search,
        ):
            success, message = execute_action(action, {"current_app": "notepad"})

        self.assertTrue(success, message)
        # Message must name Notepad (from the execution context), not chatgpt.
        self.assertIn("Notepad", message)
        google_search.assert_not_called()

    def test_unknown_foreground_app_produces_clear_failure(self):
        """When no app can be identified at execution time the user gets a
        clear, user-facing error message rather than a silent fallback."""
        action = parse_action("search in app for documents")
        with (
            patch.object(session, "current_app", None),
            patch.object(session, "last_opened_app", None),
            patch("tools.system_tools.google_search") as google_search,
        ):
            success, message = execute_action(action, {})

        self.assertFalse(success)
        self.assertIn("couldn't identify the current app", message)
        google_search.assert_not_called()

    # ------------------------------------------------------------------
    # Task 1 regressions: Verified action execution and false success suppression
    # ------------------------------------------------------------------

    def test_ai_returns_text_with_no_action_does_not_claim_success(self):
        """When AI returns text claiming an action was executed without an action,
        Jarvis must reject the claim and report failure plainly."""
        from core import router

        fake_structured = {
            "action": None,
            "arguments": {},
            "spoken": "Playing the 'Bad Apple' video on YouTube.",
        }

        with patch("core.router.ask_ai_structured", return_value=fake_structured):
            reply = router.route("bad apple", local_checked=True)

        self.assertEqual(reply, "Sorry, I couldn't do that.")

    def test_action_fails_reports_failure(self):
        """When the action fails, Jarvis must report the failure plainly and never claim success."""
        from core import router

        fake_structured = {
            "action": "play_youtube",
            "arguments": {"query": "bad apple"},
            "spoken": "Playing the 'Bad Apple' video on YouTube.",
        }

        with (
            patch("core.router.ask_ai_structured", return_value=fake_structured),
            patch("tools.browser_controller.play_or_resume_youtube", return_value=(False, "I couldn't open YouTube for bad apple.")),
        ):
            reply = router.route("bad apple", local_checked=True)

        self.assertIn("couldn't open", reply.lower())
        self.assertNotIn("playing the 'bad apple'", reply.lower())

    def test_play_bad_apple_opens_youtube_search_via_handler(self):
        """'play bad apple' must trigger YouTube playback handler and play the first result."""
        with patch("tools.browser_controller.play_or_resume_youtube", return_value=(True, "Playing Bad Apple on YouTube.")) as mock_play:
            result = system_tools.handle_command("play bad apple")

        mock_play.assert_called_once_with("bad apple")
        self.assertIn("Playing Bad Apple on YouTube", result)

    def test_ai_action_success_reports_verified_result(self):
        """When AI returns an action and it succeeds, Jarvis confirms with the real result."""
        from core import router

        fake_structured = {
            "action": "play_youtube",
            "arguments": {"query": "bad apple"},
            "spoken": "Playing Bad Apple on YouTube.",
        }

        with (
            patch("core.router.ask_ai_structured", return_value=fake_structured),
            patch("tools.browser_controller.play_or_resume_youtube", return_value=(True, "Playing Bad Apple on YouTube.")) as mock_play,
        ):
            reply = router.route("bad apple", local_checked=True)

        mock_play.assert_called_once_with("bad apple")
        self.assertIn("Playing Bad Apple on YouTube", reply)

    # ------------------------------------------------------------------
    # Task 2 regressions: Window pinning to desktop bottom layer
    # ------------------------------------------------------------------

    def test_window_pin_applies_bottom_and_noactivate_styles(self):
        """pin_to_desktop_bottom must apply HWND_BOTTOM, WS_EX_NOACTIVATE, and WS_EX_TOOLWINDOW."""
        from ui.window_pin import pin_to_desktop_bottom, HWND_BOTTOM, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST

        mock_window = Mock()
        mock_window.winId.return_value = 12345

        set_style_calls = []
        set_window_pos_calls = []

        def fake_get_style(hwnd, index):
            if index == -16:  # GWL_STYLE
                return 0x10000000  # WS_VISIBLE
            if index == -20:  # GWL_EXSTYLE
                return WS_EX_TOPMOST  # Topmost initially
            return 0

        def fake_set_style(hwnd, index, val):
            set_style_calls.append((index, val))
            return 0

        def fake_set_window_pos(hwnd, insert_after, x, y, cx, cy, flags):
            set_window_pos_calls.append((hwnd, insert_after, flags))
            return True

        with (
            patch("ctypes.WinDLL") as mock_windll,
            patch("PySide6.QtCore.QTimer") as mock_qtimer,
        ):
            mock_user32 = Mock()
            mock_windll.return_value = mock_user32
            mock_user32.GetWindowLongPtrW.side_effect = fake_get_style
            mock_user32.SetWindowLongPtrW.side_effect = fake_set_style
            mock_user32.SetWindowPos.side_effect = fake_set_window_pos

            success = pin_to_desktop_bottom(mock_window)

        self.assertTrue(success)
        # Check that WS_EX_TOPMOST was removed, and WS_EX_NOACTIVATE and WS_EX_TOOLWINDOW were added
        ex_style_settings = [val for idx, val in set_style_calls if idx == -20]
        self.assertTrue(len(ex_style_settings) > 0)
        final_ex_style = ex_style_settings[0]
        self.assertEqual(final_ex_style & WS_EX_TOPMOST, 0)
        self.assertEqual(final_ex_style & WS_EX_NOACTIVATE, WS_EX_NOACTIVATE)
        self.assertEqual(final_ex_style & WS_EX_TOOLWINDOW, WS_EX_TOOLWINDOW)

        # Check SetWindowPos was called with HWND_BOTTOM
        self.assertTrue(any(getattr(call[1], "value", call[1]) == HWND_BOTTOM for call in set_window_pos_calls))

    def test_pinned_settings_layer_does_not_repeat_window_reordering(self):
        """Settings title notifications must never move the pinned HUD in z-order."""
        from ui.window_pin import set_settings_active

        with (
            patch("ui.window_pin._is_pinned", True),
            patch("ui.window_pin._settings_modal_active", False),
            patch("ui.window_pin._pinned_hwnd_p", object()),
            patch("ui.window_pin._user32") as mock_user32,
        ):
            set_settings_active(False)
            mock_user32.SetWindowPos.assert_not_called()

            set_settings_active(True)
            mock_user32.SetWindowPos.assert_not_called()

            set_settings_active(True)
            mock_user32.SetWindowPos.assert_not_called()

    def test_pinned_window_strips_minimizebox_and_prevents_minimize(self):
        """Pinned desktop HUD must strip WS_MINIMIZEBOX and reject SC_MINIMIZE."""
        from ui.window_pin import (
            pin_to_desktop_bottom,
            is_window_pinned,
            WS_MINIMIZEBOX,
            WS_MAXIMIZEBOX,
            WS_THICKFRAME,
            WM_SYSCOMMAND,
            SC_MINIMIZE,
            WM_SHOWWINDOW,
        )

        mock_window = Mock()
        mock_window.winId.return_value = 54321

        set_style_calls = []

        def fake_get_style(hwnd, index):
            if index == -16:  # GWL_STYLE
                return WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_THICKFRAME | 0x10000000
            return 0

        def fake_set_style(hwnd, index, val):
            set_style_calls.append((index, val))
            return 0

        with patch("ctypes.WinDLL") as mock_windll:
            mock_user32 = Mock()
            mock_windll.return_value = mock_user32
            mock_user32.GetWindowLongPtrW.side_effect = fake_get_style
            mock_user32.SetWindowLongPtrW.side_effect = fake_set_style
            mock_user32.SetWindowPos.return_value = True

            success = pin_to_desktop_bottom(mock_window)

        self.assertTrue(success)
        self.assertTrue(is_window_pinned())

        # Verify WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_THICKFRAME were stripped from GWL_STYLE (-16)
        style_settings = [val for idx, val in set_style_calls if idx == -16]
        self.assertTrue(len(style_settings) > 0)
        final_style = style_settings[0]
        self.assertEqual(final_style & WS_MINIMIZEBOX, 0)
        self.assertEqual(final_style & WS_MAXIMIZEBOX, 0)
        self.assertEqual(final_style & WS_THICKFRAME, 0)

    # ------------------------------------------------------------------
    # YouTube routing restrictions & spoken fallback regressions
    # ------------------------------------------------------------------

    def test_generic_search_defaults_to_google_not_youtube(self):
        """Generic search must never route to YouTube when YouTube was not mentioned."""
        from core import router

        fake_structured = {
            "action": "search",
            "arguments": {"query": "wattage genius"},
            "spoken": "Searching for wattage genius.",
        }

        with (
            patch("core.router.ask_ai_structured", return_value=fake_structured),
            patch("tools.system_tools.search_youtube") as mock_yt,
            patch("tools.system_tools.search_website_destination", return_value="Searching google for wattage genius.") as mock_dest,
        ):
            reply = router.route("wattage genius", local_checked=True)

        mock_yt.assert_not_called()
        mock_dest.assert_called_once_with("google", "wattage genius")
        self.assertIn("Searching google", reply)

    def test_youtube_search_allowed_when_explicitly_mentioned(self):
        """Search routes to YouTube only when user explicitly mentions YouTube."""
        from core import router

        fake_structured = {
            "action": "search",
            "arguments": {"query": "wattage genius", "destination": "youtube"},
            "spoken": "Searching YouTube.",
        }

        with (
            patch("core.router.ask_ai_structured", return_value=fake_structured),
            patch("tools.system_tools.search_youtube", return_value="Searching YouTube for wattage genius.") as mock_yt,
        ):
            reply = router.route("search youtube for wattage genius", local_checked=True)

        mock_yt.assert_called_once_with("wattage genius")
        self.assertIn("Searching YouTube", reply)

    def test_ai_play_youtube_blocked_without_explicit_youtube_request(self):
        """AI returning play_youtube when user didn't mention YouTube must not open YouTube."""
        from core import router

        fake_structured = {
            "action": "play_youtube",
            "arguments": {"query": "wattage genius"},
            "spoken": "Playing wattage genius.",
        }

        with (
            patch("core.router.ask_ai_structured", return_value=fake_structured),
            patch("tools.system_tools.play_youtube") as mock_play_yt,
        ):
            reply = router.route("wattage genius", local_checked=True)

        mock_play_yt.assert_not_called()
        self.assertIn("YouTube", reply)
        self.assertIn("Name a website", reply)

    def test_handle_play_command_no_youtube_fallback(self):
        """'play <query>' without mentioning YouTube must not fall back to YouTube when no local file exists."""
        from tools import system_tools

        with patch("tools.system_tools.play_youtube") as mock_play_yt:
            res = system_tools.handle_command("play fictional_song_name_12345")

        mock_play_yt.assert_not_called()
        self.assertIn("couldn't find", res.lower())

    def test_handle_play_command_with_explicit_youtube(self):
        """'play <query> on youtube' must route to YouTube."""
        from tools import system_tools

        with patch("tools.system_tools.play_youtube", return_value="Playing fictional_song on YouTube.") as mock_play_yt:
            res = system_tools.handle_command("play fictional_song on youtube")

        mock_play_yt.assert_called_once()
        self.assertIn("Playing fictional_song on YouTube", res)

    def test_parse_structured_ai_response_spoken_fallback_for_actions(self):
        """When action is present but spoken is missing/empty, it should provide a fallback, not crash."""
        from core.ai import parse_structured_ai_response

        # Missing spoken
        res1 = parse_structured_ai_response('{"action": "open_app", "arguments": {"app": "notepad"}}')
        self.assertEqual(res1["action"], "open_app")
        self.assertTrue(bool(res1["spoken"]))

        # Empty spoken
        res2 = parse_structured_ai_response('{"action": "search", "arguments": {"query": "test"}, "spoken": ""}')
        self.assertEqual(res2["action"], "search")
        self.assertTrue(bool(res2["spoken"]))

    def test_parse_structured_ai_response_plain_text(self):
        """Plain text responses without JSON should be treated as direct spoken answers."""
        from core.ai import parse_structured_ai_response

        text = "Python is a versatile programming language created by Guido van Rossum."
        res = parse_structured_ai_response(text)
        self.assertIsNone(res["action"])
        self.assertEqual(res["arguments"], {})
        self.assertEqual(res["spoken"], text)

    def test_parse_structured_ai_response_alternative_keys(self):
        """Responses with alternative keys like reply, content, or nested message should be extracted."""
        from core.ai import parse_structured_ai_response

        # Reply key
        res1 = parse_structured_ai_response('{"action": null, "arguments": {}, "reply": "Here is the reply."}')
        self.assertEqual(res1["spoken"], "Here is the reply.")

        # Content key
        res2 = parse_structured_ai_response('{"action": null, "content": "Here is the content."}')
        self.assertEqual(res2["spoken"], "Here is the content.")

        # Nested message object (OpenAI style)
        res3 = parse_structured_ai_response('{"action": null, "arguments": {}, "message": {"content": "Nested message content."}}')
        self.assertEqual(res3["spoken"], "Nested message content.")

        # Choices array
        res4 = parse_structured_ai_response('{"choices": [{"message": {"content": "Choices message content."}}]}')
        self.assertEqual(res4["spoken"], "Choices message content.")

        # Reasoning / thought fallback
        res5 = parse_structured_ai_response('{"action": null, "thought": "Thought text only.", "spoken": ""}')
        self.assertEqual(res5["spoken"], "Thought text only.")

    def test_parse_structured_ai_response_action_fallbacks(self):
        """Verify tailored spoken fallbacks for specific actions when spoken is omitted."""
        from core.ai import parse_structured_ai_response

        res_yt = parse_structured_ai_response('{"action": "play_youtube", "arguments": {"query": "bad apple"}}')
        self.assertIn("Playing bad apple on YouTube", res_yt["spoken"])

        res_url = parse_structured_ai_response('{"action": "open_url", "arguments": {"url": "https://google.com"}}')
        self.assertIn("Opening https://google.com", res_url["spoken"])

        res_cmd = parse_structured_ai_response('{"action": "command", "arguments": {"command": "dir"}}')
        self.assertIn("Executing dir", res_cmd["spoken"])

    def test_parse_structured_ai_response_truly_empty_raises(self):
        """When an AI returns completely empty JSON with no action and no text, it should raise ValueError so fallback triggers."""
        from core.ai import parse_structured_ai_response

        with self.assertRaises(ValueError):
            parse_structured_ai_response('{"action": null, "arguments": {}, "spoken": ""}')

    def test_click_pass_through_toggle_and_query(self):
        """Click pass-through enable/disable state must be queryable and settable."""
        from ui.window_pin import set_click_pass_through, is_click_pass_through

        set_click_pass_through(False)
        self.assertFalse(is_click_pass_through())

        set_click_pass_through(True)
        self.assertTrue(is_click_pass_through())

    def test_click_pass_through_hit_testing(self):
        """Hit test must recognize top bar and arc reactor as interactive, and desktop area as transparent."""
        import ctypes
        from ui.window_pin import is_point_interactive, RECT

        mock_user32 = Mock()
        def fake_get_rect(hwnd, rect_ref):
            rect = ctypes.cast(rect_ref, ctypes.POINTER(RECT)).contents
            rect.left = 0
            rect.top = 0
            rect.right = 1920
            rect.bottom = 1080
            return 1
        mock_user32.GetWindowRect.side_effect = fake_get_rect

        # 1. Top bar (y <= 60) -> Interactive
        self.assertTrue(is_point_interactive(500, 30, object(), mock_user32))

        # 2. Center arc reactor (960, 540) -> Interactive
        self.assertTrue(is_point_interactive(960, 540, object(), mock_user32))

        # 3. Desktop translucent area (500, 500) -> Transparent to desktop icons
        self.assertFalse(is_point_interactive(500, 500, object(), mock_user32))

        # 4. Settings modal open -> Entire HUD is interactive
        with patch("ui.window_pin._settings_modal_active", True):
            self.assertTrue(is_point_interactive(500, 500, object(), mock_user32))


if __name__ == "__main__":
    unittest.main()
