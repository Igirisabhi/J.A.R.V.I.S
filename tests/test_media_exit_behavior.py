import unittest
from unittest.mock import patch
from pathlib import Path

from tools import system_tools
from voice.wake_word import should_exit


class MediaAndExitBehaviorTests(unittest.TestCase):
    def test_play_first_prefers_visible_browser_video(self):
        with patch.object(
            system_tools,
            "_play_visible_browser_video",
            return_value="Playing the visible video.",
        ):
            result = system_tools.handle_play_command("play first")
        self.assertEqual(result, "Playing the visible video.")

    def test_open_this_routes_to_visible_browser_item(self):
        with patch.object(
            system_tools,
            "_open_visible_browser_item",
            return_value="Opening the visible link.",
        ):
            result = system_tools.handle_command("open this")
        self.assertEqual(result, "Opening the visible link.")

    def test_context_memory_isolated_between_folders(self):
        from core.session_memory import SessionMemory

        memory = SessionMemory()
        memory.activate_context("folder:c:\\one")
        memory.last_opened_file = "one.txt"
        memory.last_search = "alpha"
        memory.activate_context("folder:c:\\two")
        self.assertIsNone(memory.last_opened_file)
        self.assertIsNone(memory.last_search)
        memory.last_opened_file = "two.txt"
        memory.activate_context("folder:c:\\one")
        self.assertEqual(memory.last_opened_file, "one.txt")

    def test_this_video_prefers_desktop_media(self):
        from core.session_memory import session
        old_media = session.last_played_media
        old_folder = session.active_folder
        old_active = system_tools._ACTIVE_FOLDER
        try:
            with patch.object(system_tools, "_search_media_in_folder", return_value=[r"C:\Users\Test\Desktop\clip.mp4"]), \
                 patch.object(system_tools, "_open_media_file", return_value=True), \
                 patch.object(system_tools, "KNOWN_FOLDERS", {name: Path(r"C:\Users\Test\\" + name.title()) for name in ("desktop", "documents", "downloads", "pictures", "videos", "music")}), \
                 patch("os.path.isdir", return_value=True):
                result = system_tools.handle_play_command("play this video")
        finally:
            session.last_played_media = old_media
            session.active_folder = old_folder
            system_tools._ACTIVE_FOLDER = old_active
        self.assertEqual(result, "Playing clip.mp4.")

    def test_only_sleep_is_an_exit_command(self):
        self.assertTrue(should_exit("sleep"))
        for phrase in ("bye", "goodbye", "exit assistant", "quit", "shut down assistant"):
            with self.subTest(phrase=phrase):
                self.assertFalse(should_exit(phrase))


if __name__ == "__main__":
    unittest.main()
