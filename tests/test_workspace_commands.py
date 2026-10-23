import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import settings as settings_module
from core.command_engine import (
    Action,
    execute_action,
    execute_command_chain,
    execute_dsl,
    normalize_voice_text,
    parse_action,
    parse_command_chain,
)
from core.state import state_machine
from tools.system_tools import WorkspaceError, resolve_in_workspace


class WorkspaceCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root.joinpath("notes.txt").write_text("main", encoding="utf-8")
        self.settings_patch = patch.object(settings_module, "_SETTINGS", {
            "workspace_dir": str(self.root),
            "stop_on_error": True,
            "idle_timeout_ms": 30000,
            "app_allowlist": ["notepad", "code"],
            "macros": {},
        })
        self.settings_patch.start()

    def tearDown(self):
        state_machine.set_pending([])
        self.settings_patch.stop()
        self.temp.cleanup()

    def test_open_file_inside_workspace(self):
        self.assertEqual(resolve_in_workspace("notes.txt"), self.root / "notes.txt")
        with patch("tools.system_tools.get_open_explorer_folder", return_value=str(self.root)), patch("tools.system_tools.open_path_safe", return_value=True), patch("tools.system_tools.set_active_folder"):
            success, message = execute_action(Action("open", "notes.txt"), {"current_app": None})
        self.assertTrue(success, message)

    def test_fuzzy_filename_match_does_not_open_an_unconfirmed_file(self):
        action = parse_action("open noets.txt")
        with patch("tools.system_tools.get_open_explorer_folder", return_value=str(self.root)), patch("tools.system_tools.open_path_safe", return_value=True), patch("tools.system_tools.set_active_folder"):
            success, message = execute_action(action, {"current_app": None})
        self.assertFalse(success)
        self.assertIn("couldn't find noets.txt", message)

    def test_ambiguous_filename_match_does_not_guess_a_file(self):
        self.root.joinpath("noted.txt").write_text("x", encoding="utf-8")
        self.root.joinpath("notes1.txt").write_text("x", encoding="utf-8")
        with patch("tools.system_tools.get_open_explorer_folder", return_value=str(self.root)):
            success, message = execute_action(parse_action("open noets.txt"), {"current_app": None})
        self.assertFalse(success)
        self.assertIn("couldn't find noets.txt", message)

    def test_outside_qualifiers_are_rejected(self):
        for command in ("open notes.txt in desktop", "open notes.txt in downloads", "open notes.txt in documents"):
            with self.subTest(command=command), patch("tools.system_tools.get_open_explorer_folder", return_value=str(self.root)):
                action = parse_action(command)
                self.assertEqual(action.verb, "open_file_in")
                success, message = execute_action(action, {})
                self.assertFalse(success)
                self.assertIn("folder currently open there", message)

    def test_absolute_parent_and_traversal_are_rejected(self):
        for name in (r"C:\outside\secret.txt", r"..\secret.txt", "~/secret.txt"):
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                resolve_in_workspace(name)

    def test_symlink_to_outside_is_rejected(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside.txt")
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "outside-link.txt"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symlinks are not enabled: {error}")
        with self.assertRaises(WorkspaceError):
            resolve_in_workspace("outside-link.txt")
        outside.unlink(missing_ok=True)

    def test_chain_splits_only_between_commands(self):
        actions = parse_command_chain("open a.txt and then open b.txt")
        self.assertEqual(len(actions), 2)
        self.assertEqual(parse_command_chain('open "rock and roll.txt"'), [])
        self.assertEqual(parse_action("search for main").target, "main")

    def test_pronoun_carries_created_file(self):
        seen = []
        def fake_execute(dsl, context, action):
            seen.append((action.verb, action.target))
            if action.verb == "create_file":
                return True, "Created a.txt."
            return True, "Opened a.txt."
        with patch("core.command_engine.execute_dsl", side_effect=fake_execute):
            execute_command_chain("create file a.txt then open it")
        self.assertEqual([item[0] for item in seen], ["create_file", "open"])
        self.assertTrue(str(seen[1][1]).endswith("a.txt"))

    def test_stop_on_error_true_reports_skipped_steps(self):
        with patch("core.command_engine.get_settings", return_value={"stop_on_error": True, "idle_timeout_ms": 30000}):
            with patch("core.command_engine.execute_dsl", side_effect=[(False, "missing"), (True, "opened")]) as run:
                result = execute_command_chain("open missing.txt then open notes.txt")
        self.assertIn("Skipped", result)
        self.assertEqual(run.call_count, 1)

    def test_stop_on_error_false_runs_remaining_steps(self):
        with patch("core.command_engine.get_settings", return_value={"stop_on_error": False, "idle_timeout_ms": 30000}):
            with patch("core.command_engine.execute_dsl", side_effect=[(False, "missing"), (True, "opened")]) as run:
                result = execute_command_chain("open missing.txt then open notes.txt")
        self.assertEqual(run.call_count, 2)
        self.assertIn("Completed steps: open notes.txt", result)

    def test_destructive_chain_requires_one_confirmation(self):
        with patch("core.command_engine.execute_dsl") as run:
            result = execute_command_chain("delete notes.txt then open notes.txt")
        self.assertIn("confirm", result.lower())
        run.assert_not_called()
        with patch("core.command_engine.execute_dsl", return_value=(True, "done")) as run:
            execute_command_chain("delete notes.txt then open notes.txt", confirm_callback=lambda steps: len(steps) == 1)
        self.assertEqual(run.call_count, 2)

    def test_spoken_filename_normalization(self):
        self.assertEqual(normalize_voice_text("Jarvis please open notes dot txt"), "open notes.txt")

    def test_allowlist_rejects_unknown_launch(self):
        success, message = execute_dsl("launch:malware", {}, Action("open", "malware"))
        self.assertFalse(success)
        self.assertIn("app_allowlist", message)

    def test_state_machine_idle_timeout_discards_chain(self):
        state_machine.set_pending(["open:notes.txt"])
        state_machine._last_activity = time.monotonic() - 10
        self.assertTrue(state_machine.expire(1000))
        self.assertEqual(state_machine._pending, [])


if __name__ == "__main__":
    unittest.main()
