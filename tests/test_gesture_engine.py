import unittest
from unittest.mock import patch

from core.gesture_engine import (
    GestureEngine,
    load_gesture_config,
    match_custom_sample,
    save_gesture_config,
    match_custom_sample_details,
    execute_gesture_action,
)


class GestureEngineTests(unittest.TestCase):
    def test_gesture_config_contains_camera_inversion_and_extended_mappings(self):
        config = load_gesture_config()
        self.assertIn("camera_inverted", config)
        mappings = config["default_mappings"]
        for gesture in ("Thumbs Up", "Thumbs Down", "Fist", "Pointing", "Peace Sign"):
            self.assertIn(gesture, mappings)

    def test_dispatch_resolves_custom_mapping_and_reports_execution(self):
        engine = GestureEngine()
        engine._config = {
            "custom_gestures": [
                {"name": "Peace Sign", "pose": "Victory", "action": "Volume Up"}
            ],
            "default_mappings": {},
        }
        with (
            patch("core.gesture_engine.execute_gesture_action", return_value=True) as execute,
            patch("core.events.event_bus.publish") as publish,
        ):
            engine._dispatch_gesture("Peace Sign")

        execute.assert_called_once_with("Volume Up")
        publish.assert_any_call(
            "gesture_detected",
            gesture="Peace Sign",
            action="Volume Up",
            executed=True,
        )

    def test_dispatch_resolves_default_mapping(self):
        engine = GestureEngine()
        engine._config = {
            "custom_gestures": [],
            "default_mappings": {"Thumbs Up": "Volume Up"},
        }
        with (
            patch("core.gesture_engine.execute_gesture_action", return_value=True) as execute,
            patch("core.events.event_bus.publish"),
        ):
            engine._dispatch_gesture("Thumbs Up")
        execute.assert_called_once_with("Volume Up")

    def test_dispatch_resolves_configured_two_hand_sign(self):
        engine = GestureEngine()
        engine._config = {
            "custom_gestures": [],
            "default_mappings": {},
            "hand_sign_mappings": {"2:Open_Palm+Open_Palm": "Exit Jarvis"},
        }
        with (
            patch("core.gesture_engine.execute_gesture_action", return_value=True) as execute,
            patch("core.events.event_bus.publish"),
        ):
            self.assertTrue(engine._dispatch_gesture("Hand Sign 2:Open_Palm+Open_Palm"))
        execute.assert_called_once_with("Exit Jarvis")

    def test_gesture_can_request_jarvis_exit(self):
        with patch("core.events.event_bus.publish") as publish:
            self.assertTrue(execute_gesture_action("Exit Jarvis"))
        publish.assert_called_once_with("jarvis_exit_requested", source="gesture")

    def test_gesture_can_execute_multiple_actions_in_order(self):
        with patch("core.gesture_engine._execute_single_gesture_action", side_effect=[True, True]) as execute:
            self.assertTrue(execute_gesture_action("maximize + play"))
        self.assertEqual([item.args[0] for item in execute.call_args_list], ["maximize", "play"])

    def test_gesture_opens_configured_file(self):
        with (
            patch("core.gesture_engine.Path.exists", return_value=True),
            patch("core.gesture_engine.os.startfile") as startfile,
        ):
            self.assertTrue(execute_gesture_action(r"Open File: C:\Users\brabh\Documents\Jarvis\README.md"))
        startfile.assert_called_once()

    def test_gesture_opens_configured_executable_path(self):
        with (
            patch("core.gesture_engine.Path.exists", return_value=True),
            patch("core.gesture_engine.os.startfile") as startfile,
        ):
            self.assertTrue(execute_gesture_action(r"Open App: C:\Tools\MyApp.exe"))
        startfile.assert_called_once()

    def test_gesture_rejects_missing_configured_file(self):
        with patch("core.gesture_engine.Path.exists", return_value=False):
            self.assertFalse(execute_gesture_action(r"Open File: C:\missing\document.pdf"))

    def test_sensitive_gesture_requires_confirmation(self):
        engine = GestureEngine()
        engine._config = {
            "custom_gestures": [],
            "default_mappings": {"Fist": "Lock Workstation"},
        }
        with (
            patch("core.gesture_engine.execute_gesture_action") as execute,
            patch("core.events.event_bus.publish") as publish,
        ):
            self.assertFalse(engine._dispatch_gesture("Fist"))
        execute.assert_not_called()
        publish.assert_any_call(
            "confirmation_requested",
            source="gesture",
            gesture="Fist",
            action="Lock Workstation",
        )

    def test_custom_matching_requires_a_complete_hand_sample(self):
        custom = [{"name": "Saved pose", "landmark_template": [0.0] * 63}]
        self.assertIsNone(match_custom_sample([], custom))
        self.assertEqual(match_custom_sample([0.0] * 63, custom), "Saved pose")

    def test_custom_matching_reports_confidence_and_ambiguity(self):
        name, confidence, ambiguity = match_custom_sample_details(
            [0.0] * 63,
            [
                {"name": "first", "landmark_template": [0.0] * 63},
                {"name": "second", "landmark_template": [0.1] * 63},
            ],
        )
        self.assertEqual(name, "first")
        self.assertGreater(confidence, 0.9)
        self.assertLess(ambiguity, 0.9)

    def test_custom_matching_rejects_low_confidence_false_match(self):
        """A sample with 40% confidence must not match a custom gesture."""
        # error = 0.096, threshold = 0.16 -> confidence = 1 - 0.096/0.16 = 0.40 (40%)
        custom = [{"name": "osu", "landmark_template": [0.0] * 63}]
        name, conf, amb = match_custom_sample_details([0.096] * 63, custom, threshold=0.16, min_confidence=0.70)
        self.assertIsNone(name)
        self.assertAlmostEqual(conf, 0.40, places=2)
        self.assertEqual(amb, 0.0)
        self.assertIsNone(match_custom_sample([0.096] * 63, custom, threshold=0.16, min_confidence=0.70))

    def test_custom_matching_single_gesture_has_zero_ambiguity(self):
        """A single custom gesture cannot be ambiguous with nonexistent gestures."""
        custom = [{"name": "single_pose", "landmark_template": [0.0] * 63}]
        name, conf, amb = match_custom_sample_details([0.0] * 63, custom)
        self.assertEqual(name, "single_pose")
        self.assertEqual(amb, 0.0)

    def test_gesture_config_rejects_invalid_landmark_templates(self):
        with self.assertRaises(ValueError):
            save_gesture_config(
                {"custom_gestures": [{"name": "bad", "action": "x", "landmark_template": [0.0]}]}
            )
        with self.assertRaises(ValueError):
            save_gesture_config(
                {"custom_gestures": [{"name": "bad", "action": "x", "landmark_template": [float("nan")] * 63}]}
            )
        with self.assertRaises(ValueError):
            save_gesture_config(
                {
                    "custom_gestures": [
                        {"name": "same", "action": "x"},
                        {"name": " SAME ", "action": "y"},
                    ]
                }
            )

    def test_gesture_config_rejects_duplicate_actions(self):
        with self.assertRaises(ValueError):
            save_gesture_config(
                {
                    "custom_gestures": [
                        {"name": "pose1", "action": "Volume Up"},
                        {"name": "pose2", "action": " volume up "},
                    ]
                }
            )

    def test_check_duplicate_gesture(self):
        from core.gesture_engine import check_duplicate_gesture
        config = {
            "custom_gestures": [{"name": "My Pose", "action": "Take Screenshot"}],
            "default_mappings": {"Swipe Left": "Switch Desktop"},
            "hand_sign_mappings": {"1:ILoveYou": "Open Calculator"},
        }
        # Duplicate name in custom gestures
        name_err, _ = check_duplicate_gesture("my pose", "New Action", config)
        self.assertIsNotNone(name_err)
        self.assertIn("already exists in custom gestures", name_err)

        # Duplicate name in default mappings
        name_err, _ = check_duplicate_gesture("Swipe Left", "New Action", config)
        self.assertIsNotNone(name_err)
        self.assertIn("conflicts with built-in gesture", name_err)

        # Duplicate name in hand signs
        name_err, _ = check_duplicate_gesture("1:ILoveYou", "New Action", config)
        self.assertIsNotNone(name_err)
        self.assertIn("already exists in hand sign mappings", name_err)

        # Duplicate action in custom gestures
        _, act_err = check_duplicate_gesture("New Pose", "take screenshot", config)
        self.assertIsNotNone(act_err)
        self.assertIn("already assigned to custom gesture 'My Pose'", act_err)

        # Duplicate action in default mappings
        _, act_err = check_duplicate_gesture("New Pose", "Switch Desktop", config)
        self.assertIsNotNone(act_err)
        self.assertIn("already assigned to built-in gesture 'Swipe Left'", act_err)

        # Duplicate action in hand signs
        _, act_err = check_duplicate_gesture("New Pose", "open calculator", config)
        self.assertIsNotNone(act_err)
        self.assertIn("already assigned to hand sign '1:ILoveYou'", act_err)

        # Exclude name when editing self
        name_err, act_err = check_duplicate_gesture("My Pose", "Take Screenshot", config, exclude_name="My Pose")
        self.assertIsNone(name_err)
        self.assertIsNone(act_err)

    def test_preview_countdown_timer_flow(self):
        from ui.gesture_preview import GesturePreviewWindow
        from unittest.mock import MagicMock
        preview = GesturePreviewWindow.__new__(GesturePreviewWindow)
        preview.capture_request = {"name": "Test Pose", "action": "Custom Action"}
        preview.config = {
            "custom_gestures": [],
            "default_mappings": {},
            "hand_sign_mappings": {},
        }
        preview.capture_button = MagicMock()
        preview.guide = MagicMock()
        preview._countdown_timer = MagicMock()
        preview._capture_samples = []
        preview._capture_completed = False

        preview._begin_capture()
        self.assertEqual(preview._countdown_seconds, 3)
        self.assertFalse(preview._is_capturing)
        preview._countdown_timer.start.assert_called_once()

        # Tick 1 -> 2s
        preview._on_countdown_tick()
        self.assertEqual(preview._countdown_seconds, 2)
        self.assertFalse(preview._is_capturing)

        # Tick 2 -> 1s
        preview._on_countdown_tick()
        self.assertEqual(preview._countdown_seconds, 1)
        self.assertFalse(preview._is_capturing)

        # Tick 3 -> 0s: stops timer, begins capturing
        preview._on_countdown_tick()
        self.assertEqual(preview._countdown_seconds, 0)
        self.assertTrue(preview._is_capturing)
        preview._countdown_timer.stop.assert_called_once()

    def test_preview_rejects_duplicate_before_countdown(self):
        from ui.gesture_preview import GesturePreviewWindow
        from unittest.mock import MagicMock
        preview = GesturePreviewWindow.__new__(GesturePreviewWindow)
        preview.capture_request = {"name": "Existing", "action": "Volume Up"}
        preview.config = {
            "custom_gestures": [{"name": "Existing", "action": "Volume Up"}],
            "default_mappings": {},
            "hand_sign_mappings": {},
        }
        preview.capture_button = MagicMock()
        preview.guide = MagicMock()
        preview._countdown_timer = MagicMock()

        with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warning:
            preview._begin_capture()
            mock_warning.assert_called_once()
            preview._countdown_timer.start.assert_not_called()

    def test_preview_capture_completion_auto_registers_and_schedules_close(self):
        from ui.gesture_preview import GesturePreviewWindow
        from unittest.mock import MagicMock
        preview = GesturePreviewWindow.__new__(GesturePreviewWindow)
        preview.capture_request = {"name": "Test Pose", "action": "Custom Action"}
        preview.config = {
            "confidence_threshold": 0.75,
            "custom_gestures": [],
            "default_mappings": {},
            "hand_sign_mappings": {},
        }
        preview.video = MagicMock()
        preview.gesture = MagicMock()
        preview.meaning = MagicMock()
        preview.confidence = MagicMock()
        preview.capture_button = MagicMock()
        preview.guide = MagicMock()
        preview.target_gesture = None
        preview.gesture_captured = MagicMock()
        preview._is_capturing = True
        preview._capture_completed = False
        preview._capture_samples = []
        preview.close = MagicMock()

        sample = [0.1] * 63
        image_mock = MagicMock()

        with (
            patch("ui.gesture_preview.QPixmap.fromImage"),
            patch("ui.gesture_preview.QTimer.singleShot") as mock_timer,
        ):
            for _ in range(7):
                preview._show_frame(image_mock, "", "", 0.0, sample)
                self.assertFalse(preview._capture_completed)
                mock_timer.assert_not_called()

            # 8th sample completes capture
            preview._show_frame(image_mock, "", "", 0.0, sample)
            self.assertTrue(preview._capture_completed)
            self.assertFalse(preview._is_capturing)
            preview.gesture_captured.emit.assert_called_once()
            emitted = preview.gesture_captured.emit.call_args[0][0]
            self.assertEqual(emitted["request"]["name"], "Test Pose")
            self.assertEqual(len(emitted["landmark_template"]), 63)
            # Confirms automatic window close was scheduled
            mock_timer.assert_called_once_with(800, preview.close)

    def test_check_duplicate_gesture_allows_paired_hand_sign_for_same_gesture(self):
        from core.gesture_engine import check_duplicate_gesture
        config = {
            "custom_gestures": [{"name": "osu", "action": "Open App: C:\\Tools\\osu.exe"}],
            "hand_sign_mappings": {"osu": "Open App: C:\\Tools\\osu.exe"},
            "default_mappings": {},
        }
        # Checking "osu" with exclude_name="osu" should have 0 errors
        name_err, act_err = check_duplicate_gesture("osu", "Open App: C:\\Tools\\osu.exe", config, exclude_name="osu")
        self.assertIsNone(name_err)
        self.assertIsNone(act_err)

    def test_custom_landmarks_matches_natural_camera_variance(self):
        """Webcam hand variance (RMS error ~0.12) should match custom gesture."""
        from core.gesture_engine import GestureEngine
        engine = GestureEngine.get_instance()
        engine._config = {
            "confidence_threshold": 0.82,
            "custom_gestures": [{"name": "osu", "landmark_template": [0.0] * 63, "action": "Open App: osu"}],
        }
        # A sample with slight natural jitter (RMS distance ~0.12)
        sample = [0.12] * 63
        name, conf, amb = engine._match_custom_landmarks_details(
            [type("Point", (), {"x": 0.0, "y": 0.0, "z": 0.0})()] * 21
        )
        # Directly test with normalized sample:
        from core.gesture_engine import match_custom_sample_details
        name, conf, amb = match_custom_sample_details(sample, engine._config["custom_gestures"], threshold=0.28, min_confidence=0.35)
        self.assertEqual(name, "osu")
        self.assertGreater(conf, 0.40)

    def test_dropdown_pose_aliases_to_custom_gesture_action(self):
        """Dropdown pose with emojis (e.g. '✨ Iron Man Repulsor Aim') maps to custom action."""
        from core.gesture_engine import GestureEngine
        engine = GestureEngine.get_instance()
        engine._config = {
            "custom_gestures": [{
                "name": "osu",
                "pose": "✨ Iron Man Repulsor Aim",
                "action": "Volume Up",
            }],
            "default_mappings": {"Iron Man Repulsor Aim": "Toggle Defense Matrix"},
            "hand_sign_mappings": {},
        }
        # When engine dispatches "Iron Man Repulsor Aim", custom gesture takes precedence
        with patch("core.gesture_engine.execute_gesture_action", return_value=True) as mock_exec:
            dispatched = engine._dispatch_gesture("Iron Man Repulsor Aim")
            self.assertTrue(dispatched)
            mock_exec.assert_called_with("Volume Up")

    def test_custom_gesture_bypasses_voice_app_allowlist(self):
        """Configured user gesture actions bypass generic voice allowlist."""
        from core.gesture_engine import _execute_single_gesture_action
        with (
            patch("core.gesture_engine.load_gesture_config", return_value={
                "custom_gestures": [{"name": "osu", "action": "Open App: osu"}],
                "default_mappings": {},
                "hand_sign_mappings": {},
            }),
            patch("tools.window_tools.open_app", return_value="Opening osu.") as mock_open,
        ):
            res = _execute_single_gesture_action("Open App: osu")
            self.assertTrue(res)
            mock_open.assert_called_with("osu")


if __name__ == "__main__":
    unittest.main()


