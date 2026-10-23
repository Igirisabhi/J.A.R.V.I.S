import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import browser_controller as bc
from tools import system_tools
from tools import app_tools


class BrowserFeaturesTests(unittest.TestCase):
    def test_start_jarvis_bat_content(self):
        """BAT file must only activate venv, check dependencies, and run main.py."""
        bat_path = Path(__file__).resolve().parents[1] / "start_jarvis.bat"
        content = bat_path.read_text(encoding="utf-8")

        # Must activate venv
        self.assertIn(".venv\\Scripts\\activate.bat", content)
        # Dependency installation belongs to setup, not every app launch.
        self.assertIn("setup.bat", content)
        self.assertNotIn("pip install", content)
        # Must run main.py with HUD
        self.assertIn('python "%~dp0main.py" --ui', content)

        # Must NOT open browser, URLs, or apps
        self.assertNotIn("start http", content.lower())
        self.assertNotIn("start chrome", content.lower())
        self.assertNotIn("explorer http", content.lower())
        self.assertNotIn("chatgpt", content.lower())
        self.assertNotIn("openai", content.lower())

    def test_link_click_parsing_ordinals(self):
        """Support spoken phrases and ordinals for link clicking."""
        self.assertEqual(bc.parse_link_click_command("open the first link"), 1)
        self.assertEqual(bc.parse_link_click_command("open first link"), 1)
        self.assertEqual(bc.parse_link_click_command("open first result"), 1)
        self.assertEqual(bc.parse_link_click_command("click first link"), 1)
        self.assertEqual(bc.parse_link_click_command("click the first result"), 1)
        self.assertEqual(bc.parse_link_click_command("open second link"), 2)
        self.assertEqual(bc.parse_link_click_command("click second link"), 2)
        self.assertEqual(bc.parse_link_click_command("open 2nd link"), 2)
        self.assertEqual(bc.parse_link_click_command("open third link"), 3)
        self.assertEqual(bc.parse_link_click_command("click 3rd result"), 3)
        self.assertEqual(bc.parse_link_click_command("open fourth link"), 4)
        self.assertEqual(bc.parse_link_click_command("open fifth link"), 5)

    def test_scroll_command_parsing(self):
        """Support various scroll voice commands."""
        self.assertEqual(bc.parse_scroll_command("scroll down"), "down")
        self.assertEqual(bc.parse_scroll_command("scroll up"), "up")
        self.assertEqual(bc.parse_scroll_command("scroll to top"), "top")
        self.assertEqual(bc.parse_scroll_command("scroll to the top"), "top")
        self.assertEqual(bc.parse_scroll_command("scroll to bottom"), "bottom")
        self.assertEqual(bc.parse_scroll_command("scroll to the bottom"), "bottom")
        self.assertEqual(bc.parse_scroll_command("scroll down more"), "down_more")
        self.assertEqual(bc.parse_scroll_command("scroll down a lot"), "down_more")
        self.assertEqual(bc.parse_scroll_command("scroll up more"), "up_more")
        self.assertEqual(bc.parse_scroll_command("scroll up a lot"), "up_more")
        self.assertEqual(bc.parse_scroll_command("scroll a little"), "down_little")
        self.assertEqual(bc.parse_scroll_command("scroll down a little"), "down_little")
        self.assertEqual(bc.parse_scroll_command("scroll up a little"), "up_little")

    def test_first_link_handled_before_app_lookup(self):
        """'open the first link' must be handled by browser handler and never reach app search."""
        with patch.object(bc, "click_link_by_ordinal", return_value=(True, "Opening Python documentation.")) as mock_click:
            result = system_tools.handle_command("open the first link")
            self.assertEqual(result, "Opening Python documentation.")
            mock_click.assert_called_once_with(1)

        with patch.object(bc, "click_link_by_ordinal", return_value=(True, "Opening second result.")) as mock_click:
            result = system_tools.handle_command("open second link")
            self.assertEqual(result, "Opening second result.")
            mock_click.assert_called_once_with(2)

    def test_scrolling_handled_before_app_lookup(self):
        """Scroll commands must be handled before app search."""
        with patch.object(bc, "scroll_page", return_value=(True, "Scrolled down.")) as mock_scroll:
            result = system_tools.handle_command("scroll down")
            self.assertEqual(result, "Scrolled down.")
            mock_scroll.assert_called_once_with("down")

        with patch.object(bc, "scroll_page", return_value=(True, "Scrolled to top.")) as mock_scroll:
            result = system_tools.handle_command("scroll to top")
            self.assertEqual(result, "Scrolled to top.")
            mock_scroll.assert_called_once_with("top")

    def test_tab_reuse_in_open_url(self):
        """open_url must default to tab reuse and call navigate_to_url."""
        with patch("tools.browser_controller.navigate_to_url", return_value=True) as mock_nav:
            success = system_tools.open_url("https://example.com")
            self.assertTrue(success)
            mock_nav.assert_called_once_with("https://example.com", new_tab=False)

    def test_unrecognized_command_does_not_trigger_app_cache_rescan(self):
        """Non-open commands that fail to match an app must not trigger refresh_apps."""
        with patch("tools.app_tools.refresh_apps") as mock_refresh:
            system_tools.handle_app_command("what is the weather today")
            mock_refresh.assert_not_called()

            system_tools.handle_app_command("switch to nonexistentthing")
            mock_refresh.assert_not_called()

    def test_open_nonexistent_app_does_not_trigger_cache_rescan(self):
        """Unrecognized app commands must not rescan all apps; only explicit refresh does."""
        with (
            patch("tools.app_tools.refresh_apps") as mock_refresh,
            patch("tools.app_tools.find_app", return_value=None),
        ):
            system_tools.handle_app_command("open totallyrealapp1234")
            mock_refresh.assert_not_called()

        with patch("tools.app_tools.refresh_apps") as mock_refresh:
            res = system_tools.handle_app_command("refresh apps")
            mock_refresh.assert_called_once()
            self.assertIn("refreshed", res.lower())

    def test_search_command_sanitization_and_site_targeting(self):
        """Search query must be sanitized and targeted to active site."""
        from tools.browser_controller import clean_search_query, build_site_search_url

        # Test stripping bad URL fragments
        dirty = "ttps://www.youtube.com/results?search_query=bad+apple"
        cleaned = clean_search_query(dirty)
        self.assertEqual(cleaned, "bad apple")

        # Test YouTube site query building
        yt_url, clean_q, site = build_site_search_url("bad apple", current_site="youtube")
        self.assertEqual(yt_url, "https://www.youtube.com/results?search_query=bad+apple")
        self.assertEqual(clean_q, "bad apple")
        self.assertEqual(site, "youtube")

        # Test Google site query building
        google_url, clean_q, site = build_site_search_url("python docs", current_site="google")
        self.assertEqual(google_url, "https://www.google.com/search?q=python+docs")
        self.assertEqual(clean_q, "python docs")
        self.assertEqual(site, "google")

        # Test default when no site known
        default_url, clean_q, site = build_site_search_url("space exploration", current_site=None)
        self.assertEqual(default_url, "https://www.google.com/search?q=space+exploration")

        # Test end-to-end handle_search_command while active site is youtube
        with (
            patch("tools.browser_controller.get_current_active_browser_site", return_value="youtube"),
            patch("tools.browser_controller.navigate_to_url", return_value=True) as mock_nav,
        ):
            res = system_tools.handle_search_command("search bad apple")
            mock_nav.assert_called_once_with("https://www.youtube.com/results?search_query=bad+apple")
            self.assertIn("Searching YouTube for bad apple", res)

    def test_tolerant_json_parser(self):
        """Tolerant parser must recover from code fences and truncated JSON."""
        from core.ai import tolerant_json_parse

        # Code fences
        fenced = "```json\n{\"reply\": \"Hello\", \"action\": null}\n```"
        self.assertEqual(tolerant_json_parse(fenced), {"reply": "Hello", "action": None})

        # Unterminated string and missing closing brace
        broken = '{"reply": "Searching for music", "action": {"type": "search", "query": "lofi'
        parsed = tolerant_json_parse(broken)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("reply"), "Searching for music")

    def test_app_aliases_and_generic_word_guard(self):
        """Aliases must resolve mishearings and generic words must prompt without rescanning."""
        # Generic word "upload"
        app, prompt = app_tools.resolve_app("upload")
        self.assertIsNone(app)
        self.assertIn("What did you mean by 'upload'?", prompt)

        # Aliases with mock apps
        mock_apps = [
            ("Google Chrome", "C:\\Program Files\\Google\\Chrome\\chrome.exe"),
            ("Visual Studio Code", "C:\\Users\\User\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe"),
            ("Spotify", "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
        ]
        with patch("tools.app_tools.get_installed_apps", return_value=mock_apps):
            app, prompt = app_tools.resolve_app("crome")
            self.assertIsNotNone(app)
            self.assertIn("chrome", app[0].lower())

            app, prompt = app_tools.resolve_app("vscode")
            self.assertIsNotNone(app)
            self.assertIn("code", app[0].lower())

            app, prompt = app_tools.resolve_app("spotty fi")
            self.assertIsNotNone(app)
            self.assertIn("spotify", app[0].lower())

            # Unknown app suggestion (score between 50 and 79)
            app, prompt = app_tools.resolve_app("spoteee")
            self.assertIsNone(app)
            self.assertIn("did you mean", prompt.lower())

    def test_voice_stop_and_exit_commands(self):
        """Voice stop and exit commands must be accurately detected."""
        from voice.wake_word import should_stop, should_exit

        # Stop phrases
        self.assertTrue(should_stop("stop"))
        self.assertTrue(should_stop("cancel"))
        self.assertTrue(should_stop("be quiet"))
        self.assertTrue(should_stop("shut up"))
        self.assertTrue(should_stop("please shut up"))

        # Exit phrases
        self.assertTrue(should_exit("sleep"))
        self.assertFalse(should_exit("exit assistant"))
        self.assertFalse(should_exit("quit assistant"))
        self.assertFalse(should_exit("shut down assistant"))
        self.assertFalse(should_exit("open chrome"))

    def test_misheard_ordinals_mapping_and_fuzzy(self):
        """Misheard ordinals (e.g. later, fast, worst, secund, thurd) map correctly inside link commands."""
        # 1st aliases: later, fast, worst, forst, fist, thirst, 1st, one, number one
        self.assertEqual(bc.parse_link_click_command("open the later link"), 1)
        self.assertEqual(bc.parse_link_click_command("open fast link"), 1)
        self.assertEqual(bc.parse_link_click_command("click worst result"), 1)
        self.assertEqual(bc.parse_link_click_command("open forst link"), 1)
        self.assertEqual(bc.parse_link_click_command("click fist link"), 1)
        self.assertEqual(bc.parse_link_click_command("open thirst link"), 1)
        self.assertEqual(bc.parse_link_click_command("open 1st link"), 1)
        self.assertEqual(bc.parse_link_click_command("open number one link"), 1)

        # 2nd aliases: 2nd, two, number two, secund
        self.assertEqual(bc.parse_link_click_command("open secund link"), 2)
        self.assertEqual(bc.parse_link_click_command("open number two link"), 2)
        self.assertEqual(bc.parse_link_click_command("click 2nd result"), 2)

        # 3rd aliases: 3rd, three, number three, thurd
        self.assertEqual(bc.parse_link_click_command("open thurd link"), 3)
        self.assertEqual(bc.parse_link_click_command("open number three result"), 3)

        # Direct resolve_ordinal_word fuzzy fallback
        self.assertEqual(bc.resolve_ordinal_word("later"), 1)
        self.assertEqual(bc.resolve_ordinal_word("secund"), 2)
        self.assertEqual(bc.resolve_ordinal_word("thurd"), 3)

        # Normal words elsewhere are unaffected
        self.assertIsNone(bc.resolve_ordinal_word("hello"))
        self.assertIsNone(bc.resolve_ordinal_word("apple"))
        self.assertIsNone(bc.resolve_ordinal_word("chrome"))

    def test_play_always_plays_first_result(self):
        """'play <query>' always invokes play_or_resume_youtube before AI/app lookup."""
        with patch("tools.browser_controller.play_or_resume_youtube", return_value=(True, "Playing Bad Apple on YouTube.")) as mock_play:
            res = system_tools.handle_command("play bad apple")
            self.assertIn("Playing Bad Apple on YouTube", res)
            mock_play.assert_called_once_with("bad apple")

        # Empty "play" resumes playback
        with patch("tools.browser_controller.play_or_resume_youtube", return_value=(True, "Resumed playback.")) as mock_play:
            res = system_tools.handle_command("play")
            self.assertEqual(res, "Resumed playback.")
            mock_play.assert_called_once_with("")

    def test_casual_phrases_local_handling(self):
        """Casual conversational phrases are handled locally with no AI router call."""
        # Only the explicit sleep command exits.
        self.assertEqual(system_tools.handle_command("sleep"), "__GOODBYE_EXIT__")
        self.assertNotEqual(system_tools.handle_command("bye, bye, bye"), "__GOODBYE_EXIT__")
        self.assertNotEqual(system_tools.handle_command("goodbye"), "__GOODBYE_EXIT__")
        self.assertNotEqual(system_tools.handle_command("exit assistant"), "__GOODBYE_EXIT__")

        # Thanks phrases
        self.assertEqual(system_tools.handle_command("thank you"), "You're very welcome, sir.")
        self.assertEqual(system_tools.handle_command("thanks"), "You're very welcome, sir.")

        # Okay phrases
        self.assertEqual(system_tools.handle_command("okay"), "Standing by, sir.")
        self.assertEqual(system_tools.handle_command("got it"), "Standing by, sir.")

        # Greetings
        self.assertEqual(system_tools.handle_command("hello"), "Hello, sir. How can I assist you today?")
        self.assertEqual(system_tools.handle_command("hi"), "Hello, sir. How can I assist you today?")

    def test_claude_cloud_aliases_and_priority_rule(self):
        """'open cloud' resolves to Claude, with priority rule prompting if other cloud apps exist."""
        # 1. No other cloud app in cache: prefers Claude desktop or claude.ai in browser
        with patch("tools.app_tools.get_installed_apps", return_value=[]):
            app, prompt = app_tools.resolve_app("cloud")
            self.assertIsNotNone(app)
            self.assertEqual(app[0], "Claude")
            self.assertEqual(app[1], "https://claude.ai")

        # 2. Separate cloud app in cache (e.g. iCloud): prompts user for disambiguation
        mock_apps = [
            ("iCloud", "C:\\Program Files\\iCloud\\iCloud.exe"),
        ]
        with patch("tools.app_tools.get_installed_apps", return_value=mock_apps):
            app, prompt = app_tools.resolve_app("cloud")
            self.assertIsNone(app)
            self.assertIn("Did you mean Claude or iCloud?", prompt)

        # 3. User correction command: 'remember that cloud means Claude'
        with patch("tools.app_tools.add_alias", return_value=True) as mock_add_alias:
            res = system_tools.handle_command("remember that cloud means Claude")
            self.assertEqual(res, "I'll remember that cloud means Claude.")
            mock_add_alias.assert_called_once_with("cloud", "Claude")

    def test_hallucination_filter_sample_list(self):
        """Test list of sample replies (should pass / should be flagged)."""
        from core.router import filter_hallucinated_action_claims

        # Sample replies that SHOULD PASS (capabilities, offers, helpful text)
        should_pass = [
            "I'm here to help you with tasks like opening apps, searching the web, or playing media. Let me know what you'd like to do next.",
            "I can help with opening Chrome or playing videos.",
            "Let me know what you'd like to do.",
            "I am able to play music or open websites for you.",
            "Sure, what video would you like to watch?",
        ]
        for reply in should_pass:
            result = filter_hallucinated_action_claims(reply, tool_succeeded=False)
            self.assertEqual(result, reply, f"Capability reply should not have been flagged: {reply}")

        # Sample replies that SHOULD BE FLAGGED (past-tense completion claims with no tool execution)
        should_be_flagged = [
            "I opened Chrome for you.",
            "I've sent the message.",
            "Done, I played it.",
            "Playing the 'Bad Apple' video on YouTube.",
            "All set, I started Spotify.",
        ]
        for reply in should_be_flagged:
            result = filter_hallucinated_action_claims(reply, tool_succeeded=False)
            self.assertEqual(result, "Sorry, I couldn't do that.", f"Action claim should have been flagged: {reply}")

        # Sample reply with offending sentence stripped and remaining sentence kept
        mixed = "I opened Chrome. Let me know what you'd like to do next."
        result = filter_hallucinated_action_claims(mixed, tool_succeeded=False)
        self.assertEqual(result, "Let me know what you'd like to do next.")

        # When tool_succeeded is True, all replies pass
        for reply in should_be_flagged:
            result = filter_hallucinated_action_claims(reply, tool_succeeded=True)
            self.assertEqual(result, reply)

    def test_scrolling_silent_in_wake_word(self):
        """Scrolling must be executed silently without TTS chatter to prevent mic barge-in exit loops."""
        from voice.wake_word import should_speak_tool_result
        self.assertFalse(should_speak_tool_result("Scrolled down."))
        self.assertFalse(should_speak_tool_result("Scrolled up."))
        self.assertFalse(should_speak_tool_result("Scrolled to top."))
        self.assertFalse(should_speak_tool_result("Scrolled to bottom."))
        self.assertFalse(should_speak_tool_result("Scrolled."))

    def test_scroll_command_variations_and_misrecognitions(self):
        """parse_scroll_command must handle standalone scroll, directional move, and phonetic misrecognitions."""
        from tools.browser_controller import parse_scroll_command
        self.assertEqual(parse_scroll_command("scroll"), "down")
        self.assertEqual(parse_scroll_command("scroll down"), "down")
        self.assertEqual(parse_scroll_command("scroll, down"), "down")
        self.assertEqual(parse_scroll_command("go down"), "down")
        self.assertEqual(parse_scroll_command("move down"), "down")
        self.assertEqual(parse_scroll_command("slow down"), "down")
        self.assertEqual(parse_scroll_command("stroll down"), "down")
        self.assertEqual(parse_scroll_command("close down"), "down")
        self.assertEqual(parse_scroll_command("page down"), "down")
        self.assertEqual(parse_scroll_command("scroll up"), "up")
        self.assertEqual(parse_scroll_command("page up"), "up")
        self.assertEqual(parse_scroll_command("go up"), "up")
        self.assertEqual(parse_scroll_command("move up"), "up")

    def test_stt_cleanup_comma_resilience_and_corrections(self):
        """STT cleanup must handle comma-separated transcripts and mishearings."""
        from voice.stt_cleanup import cleanup_transcription
        self.assertEqual(cleanup_transcription("Scroll, down."), "scroll down")
        self.assertEqual(cleanup_transcription("slow down"), "scroll down")
        self.assertEqual(cleanup_transcription("close down"), "scroll down")
        self.assertEqual(cleanup_transcription("open, no pad"), "open Notepad")
        self.assertEqual(cleanup_transcription("go to, you tube"), "go to YouTube")

    def test_close_down_safely_scrolls_instead_of_closing(self):
        """'close down' must not close any active window; it must scroll."""
        with patch("tools.browser_controller.scroll_page", return_value=(True, "Scrolled down.")) as mock_scroll, \
             patch("tools.window_tools.close_current_window") as mock_close:
            res = system_tools.handle_command("close down")
            self.assertEqual(res, "Scrolled down.")
            mock_scroll.assert_called_once_with("down")
            mock_close.assert_not_called()

    def test_command_engine_scroll_action(self):
        """Command engine must parse and execute Action(verb='scroll', target='down')."""
        from core.command_engine import parse_action, execute_action
        action = parse_action("scroll down")
        self.assertIsNotNone(action)
        self.assertEqual(action.verb, "scroll")
        self.assertEqual(action.target, "down")

        with patch("tools.browser_controller.scroll_page", return_value=(True, "Scrolled down.")) as mock_scroll:
            ok, msg = execute_action(action, {})
            self.assertTrue(ok)
            self.assertEqual(msg, "Scrolled down.")
            mock_scroll.assert_called_once_with("down")

    def test_dynamic_json_corrections_connected_to_all_files(self):
        """Adding a wrong word directly to command_corrections.json works across STT, routing, browser, and windows."""
        from voice.stt_cleanup import cleanup_transcription, reload_corrections
        from tools.browser_controller import parse_scroll_command
        from tools.window_tools import handle_window_command
        from voice.stt_groq import get_whisper_prompt
        import json
        from pathlib import Path

        corr_path = Path(__file__).resolve().parent.parent / "config" / "command_corrections.json"
        with open(corr_path, "r", encoding="utf-8-sig") as f:
            original_data = json.load(f)

        try:
            # Inject a test mispronounced word
            modified = dict(original_data)
            new_corrections = {
                "testcustomappword": "SuperTestApp",
                "testmisheardscroll": "scroll down",
            }
            new_corrections.update(original_data.get("corrections", {}))
            modified["corrections"] = new_corrections

            with open(corr_path, "w", encoding="utf-8") as f:
                json.dump(modified, f, indent=2)

            reload_corrections()

            # 1. Connected to STT cleanup
            self.assertEqual(cleanup_transcription("testmisheardscroll"), "scroll down")

            # 2. Connected to browser controller scroll parsing
            self.assertEqual(parse_scroll_command("testmisheardscroll"), "down")

            # 3. Connected to Groq Whisper prompt
            prompt = get_whisper_prompt()
            self.assertIn("SuperTestApp", prompt)
        finally:
            with open(corr_path, "w", encoding="utf-8") as f:
                json.dump(original_data, f, indent=2)
            reload_corrections()

    def test_accent_corrections_for_maximize_minimize_restore(self):
        """Accent mishearings for maximize, minimize, and restore must resolve deterministically."""
        from voice.stt_cleanup import cleanup_transcription
        from core.command_engine import normalize_voice_text, parse_action
        from tools.window_tools import handle_window_command

        # 1. STT cleanup & phonetic normalization
        maximize_variants = [
            "maximise", "maximum", "maximized", "maximised", "maxim is",
            "max my", "max mice", "make some eyes", "make some ice",
            "max size", "make size", "namaz the tip is it",
        ]
        for variant in maximize_variants:
            cleaned = cleanup_transcription(variant)
            self.assertTrue(
                "maximize" in cleaned.lower(),
                f"STT cleanup failed for {variant!r}: got {cleaned!r}",
            )

        # 2. Command engine parsing to Action(verb="maximize_window", target="it")
        engine_maximize_inputs = [
            "maximize", "maximise", "maximum", "maximize it", "maximise it",
            "maximize window", "maximise window", "maximum window",
            "full screen window", "fullscreen", "maximized", "maximised",
            "namaz, the tip is it", "maxim is",
        ]
        for phrase in engine_maximize_inputs:
            normalized = normalize_voice_text(phrase)
            action = parse_action(normalized)
            self.assertIsNotNone(action, f"parse_action returned None for {phrase!r} (norm: {normalized!r})")
            self.assertEqual(
                action.verb, "maximize_window",
                f"Expected maximize_window for {phrase!r}, got verb={action.verb!r}",
            )

        # 3. Window tools direct handler routing
        with patch("tools.window_tools.maximize_current_window", return_value="Window maximized.") as mock_max:
            for phrase in ["maximize", "maximise", "maximum", "maximize it", "maximise it", "maximize window", "maximise window", "make full"]:
                res = handle_window_command(phrase)
                self.assertEqual(res, "Window maximized.", f"handle_window_command failed for {phrase!r}")
            self.assertEqual(mock_max.call_count, 8)

        # 4. Minimize and restore accent variants
        with patch("tools.window_tools.minimize_current_window", return_value="Window minimized.") as mock_min:
            for phrase in ["minimize", "minimise", "minimum", "minimize it", "minimise it", "minimize window"]:
                res = handle_window_command(phrase)
                self.assertEqual(res, "Window minimized.", f"handle_window_command minimize failed for {phrase!r}")
            self.assertEqual(mock_min.call_count, 6)

        with patch("tools.window_tools.restore_current_window", return_value="Restored window.") as mock_restore:
            for phrase in ["restore", "restor", "restored", "restore it", "restore window", "normal size"]:
                res = handle_window_command(phrase)
                self.assertEqual(res, "Restored window.", f"handle_window_command restore failed for {phrase!r}")
            self.assertEqual(mock_restore.call_count, 6)


if __name__ == "__main__":
    unittest.main()

