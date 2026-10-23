"""Read-only camera preview for Jarvis gesture controls."""

from __future__ import annotations

import re
import time

from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget, QInputDialog, QMessageBox


GESTURE_MEANINGS = {
    "Open_Palm": "Open palm",
    "Closed_Fist": "Closed fist",
    "Pointing_Up": "Pointing up",
    "Thumb_Down": "Thumbs down",
    "Thumb_Up": "Thumbs up",
    "Victory": "Peace sign",
    "ILoveYou": "I love you hand sign",
}
GESTURE_GUIDES = {
    "Swipe Left": ("Move your open hand quickly from right to left.", "Open hand • move horizontally ←"),
    "Swipe Right": ("Move your open hand quickly from left to right.", "Open hand • move horizontally →"),
    "Two-Finger Pinch": ("Touch your thumb tip to your index-finger tip.", "Thumb + index touch • other fingers relaxed"),
    "Thumbs Up": ("Close your fingers into a fist and point only your thumb upward.", "Fist • thumb up"),
    "Thumbs Down": ("Close your fingers into a fist and point only your thumb downward.", "Fist • thumb down"),
    "Fist": ("Curl all five fingers into your palm.", "All fingers folded • thumb across fingers"),
    "Pointing": ("Make a fist and extend only your index finger.", "Index finger up • all other fingers folded"),
    "Peace Sign": ("Extend your index and middle fingers in a V shape.", "Index + middle up • ring + pinky folded"),
    "Iron Man Repulsor Aim": ("Extend your thumb, index, and pinky. Fold your middle and ring fingers.", "Thumb + index + pinky up • middle + ring folded"),
    "Open Palm": ("Hold your hand flat toward the camera.", "All five fingers straight and spread"),
    "Two Hands": ("Show the configured signs with both hands at the same time.", "Use both hands • each hand may have a different sign"),
}
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)


class _GestureCamera(QThread):
    frame_ready = Signal(QImage, str, str, float, object)
    failed = Signal(str)

    def run(self):
        camera = None
        recognizer = None
        try:
            import cv2
            import mediapipe as mp
            from pathlib import Path

            model = Path(__file__).resolve().parents[1] / "models" / "gesture_recognizer.task"
            if not model.is_file():
                raise FileNotFoundError(f"Gesture model not found: {model}")

            from core.gesture_engine import get_camera_runtime_config, load_gesture_config
            gesture_config = load_gesture_config()
            runtime_config = get_camera_runtime_config(gesture_config)
            configured_device = gesture_config.get("camera_device", 0)
            camera_index = int(configured_device) if str(configured_device).strip().isdigit() else 0
            camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                camera = cv2.VideoCapture(0)
            if not camera.isOpened():
                raise RuntimeError("No camera is available for gesture preview.")
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, runtime_config["width"])
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, runtime_config["height"])
            camera.set(cv2.CAP_PROP_FPS, runtime_config["camera_fps"])
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            options = mp.tasks.vision.GestureRecognizerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_hands=2,
            )
            recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)
            next_recognition = 0.0
            recognition_interval = 1.0 / runtime_config["recognition_fps"]
            while not self.isInterruptionRequested():
                ok, frame = camera.read()
                if not ok:
                    self.msleep(30)
                    continue
                if gesture_config.get("camera_inverted", True):
                    frame = cv2.flip(frame, 1)
                now = time.monotonic()
                if now < next_recognition:
                    continue
                next_recognition = now + recognition_interval
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = recognizer.recognize_for_video(image, int(now * 1000))
                categories = []
                confidence = 0.0
                hand_data = []
                for index, hand_categories in enumerate(result.gestures or []):
                    if hand_categories:
                        top = hand_categories[0]
                        landmarks = result.hand_landmarks[index] if index < len(result.hand_landmarks) else []
                        x_position = landmarks[0].x if landmarks else float(index)
                        handedness = ""
                        if index < len(result.handedness) and result.handedness[index]:
                            handedness = str(result.handedness[index][0].category_name or "").casefold()
                        side_order = 0 if handedness == "left" else 1 if handedness == "right" else 2
                        hand_data.append((side_order, x_position, top.category_name or ""))
                        confidence = max(confidence, float(top.score or 0.0))
                hand_data.sort(key=lambda item: (item[0], item[1]))
                categories = [item[2] for item in hand_data]
                category = f"{len(categories)}:{'+'.join(categories)}" if categories else ""
                normalized = []
                if result.hand_landmarks:
                    from core.gesture_engine import normalize_landmark_hands
                    normalized = normalize_landmark_hands(
                        result.hand_landmarks,
                        getattr(result, "handedness", None),
                    )
                    for hand in result.hand_landmarks:
                        for start, end in HAND_CONNECTIONS:
                            x1, y1 = int(hand[start].x * frame.shape[1]), int(hand[start].y * frame.shape[0])
                            x2, y2 = int(hand[end].x * frame.shape[1]), int(hand[end].y * frame.shape[0])
                            cv2.line(frame, (x1, y1), (x2, y2), (84, 214, 255), 2)
                        for point in hand:
                            cv2.circle(
                                frame,
                                (int(point.x * frame.shape[1]), int(point.y * frame.shape[0])),
                                3,
                                (255, 255, 255),
                                -1,
                            )
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if not normalized:
                    category = ""
                    meaning = "No hand detected"
                else:
                    meaning = " + ".join(GESTURE_MEANINGS.get(item, item) for item in categories)
                height, width, channels = rgb.shape
                qt_image = QImage(
                    rgb.data, width, height, channels * width, QImage.Format.Format_RGB888
                ).copy()
                self.frame_ready.emit(qt_image, category, meaning, confidence, normalized)
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if recognizer is not None:
                try:
                    recognizer.close()
                except Exception:
                    pass
            if camera is not None:
                camera.release()


class GesturePreviewWindow(QWidget):
    """A regular, independent window; recognition never sends PC actions."""

    gesture_captured = Signal(object)
    closed = Signal()

    def __init__(self, parent=None, capture_request=None, target_gesture=None):
        super().__init__(parent)
        self.capture_request = capture_request
        self.target_gesture = target_gesture
        self._capture_samples = []
        self._capture_completed = False
        self._countdown_seconds = 0
        self._is_capturing = False
        self._last_config_load = 0.0
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)
        self.setWindowTitle("Jarvis Gesture Control Preview")
        self.setMinimumSize(720, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        target_text = f" — show: {target_gesture}" if target_gesture else ""
        self.notice = QLabel(f"PREVIEW ONLY — gestures shown here will not execute actions{target_text}.")
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setStyleSheet("font-size:13px;font-weight:700;color:#54d6ff;padding:8px")
        guide_title, guide_pose = self._guide_for(target_gesture)
        self.guide = QLabel(
            f"<b>HOW TO MAKE IT</b><br>{guide_title}<br>"
            f"<span style='color:#54d6ff'>{guide_pose}</span>"
        )
        self.guide.setWordWrap(True)
        self.guide.setStyleSheet(
            "font-size:14px;color:#dce6f2;padding:10px;background:#101c2b;"
            "border:1px solid #23516a;border-radius:3px;"
        )
        self.video = QLabel("Starting camera…")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(680, 440)
        self.video.setStyleSheet("background:#10141c;color:#dce6f2")
        self.gesture = QLabel("Detected gesture: —")
        self.gesture.setStyleSheet("font-size:20px;font-weight:600")
        self.meaning = QLabel("Mapped action: waiting for a gesture")
        self.meaning.setStyleSheet("font-size:15px")
        self.confidence = QLabel("Confidence: —")
        btn_label = f"Start 3s Capture: '{capture_request.get('name')}'" if capture_request else "Capture custom pose"
        self.capture_button = QPushButton(btn_label)
        self.capture_button.clicked.connect(self._begin_capture)
        self.capture_button.setVisible(True)
        self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#54d6ff")
        self.confidence.setStyleSheet("font-size:13px;color:#aab8ca")
        layout.addWidget(self.notice)
        layout.addWidget(self.guide)
        layout.addWidget(self.video)
        layout.addWidget(self.gesture)
        layout.addWidget(self.meaning)
        layout.addWidget(self.confidence)
        layout.addWidget(self.capture_button)

        try:
            from core.gesture_engine import load_gesture_config
            self.config = load_gesture_config()
        except Exception:
            self.config = {}

        self.camera = _GestureCamera(self)
        self.camera.frame_ready.connect(self._show_frame)
        self.camera.failed.connect(self._show_error)
        self.camera.start()

        if capture_request:
            QTimer.singleShot(400, self._begin_capture)

    @staticmethod
    def _guide_for(target_gesture):
        if not target_gesture:
            return (
                "Choose a gesture button to see its hand position and movement.",
                "The camera view below shows your live detected pose.",
            )
        guide = GESTURE_GUIDES.get(target_gesture)
        if guide:
            return guide
        return (
            "This is a custom learned pose. Match the live camera reference used when it was captured.",
            "Keep your hand in the same shape and position until it is recognized.",
        )

    def begin_capture(self, request):
        if hasattr(self, '_countdown_timer') and self._countdown_timer.isActive():
            self._countdown_timer.stop()
        self.capture_request = request
        self._capture_samples = []
        self._capture_completed = False
        self._is_capturing = False
        name = request.get("name", "custom pose")
        self.capture_button.setText(f"Get ready… 3s")
        self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#ffd600")
        self.capture_button.setEnabled(False)
        self.capture_button.setVisible(True)
        QTimer.singleShot(250, self._begin_capture)

    def _begin_capture(self):
        if not self.capture_request:
            name, accepted = QInputDialog.getText(self, "Save hand gesture", "Gesture name:")
            if not accepted or not name.strip():
                return
            action, accepted = QInputDialog.getText(self, "Map hand gesture", "Action or command:")
            if not accepted or not action.strip():
                return
            self.capture_request = {"name": name.strip(), "action": action.strip()}

        name = str(self.capture_request.get("name", "")).strip()
        action = str(self.capture_request.get("action", "")).strip()
        editing_name = self.capture_request.get("editing_name")

        from core.gesture_engine import check_duplicate_gesture, load_gesture_config
        try:
            self.config = load_gesture_config()
        except Exception:
            pass
        if self.capture_request.get("app_scoped"):
            name_err, act_err = None, None
        else:
            name_err, act_err = check_duplicate_gesture(name, action, self.config, exclude_name=editing_name)
        if name_err or act_err:
            QMessageBox.warning(self, "Duplicate Gesture / Action", name_err or act_err)
            self.capture_button.setText("Capture custom pose")
            self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#54d6ff")
            self.capture_button.setEnabled(True)
            return

        self._capture_samples = []
        self._capture_completed = False
        self._is_capturing = False
        self._countdown_seconds = 3
        self.capture_button.setEnabled(False)
        self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#ffd600")
        self.capture_button.setText("Get ready… 3s")
        self.guide.setText(
            f"<b>CAPTURING '{name}'</b><br>"
            f"<span style='color:#ffd600;font-size:15px;'>⏱️ Place your hand in position! Capture begins in 3 seconds…</span>"
        )
        self._countdown_timer.start()

    def _on_countdown_tick(self):
        self._countdown_seconds -= 1
        name = self.capture_request.get("name", "custom pose") if self.capture_request else "custom pose"
        if self._countdown_seconds > 0:
            self.capture_button.setText(f"Get ready… {self._countdown_seconds}s")
            self.guide.setText(
                f"<b>CAPTURING '{name}'</b><br>"
                f"<span style='color:#ffd600;font-size:15px;'>⏱️ Place your hand in position! Capture begins in {self._countdown_seconds} second{'s' if self._countdown_seconds > 1 else ''}…</span>"
            )
        else:
            self._countdown_timer.stop()
            self._is_capturing = True
            self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#00e676")
            self.capture_button.setText("Recording pose… Hold steady!")
            self.guide.setText(
                f"<b>CAPTURING '{name}'</b><br>"
                f"<span style='color:#00e676;font-size:15px;'>📸 Hold the pose steady and keep both hands visible for a two-hand gesture…</span>"
            )

    def _show_frame(self, image, category, meaning, confidence, normalized):
        self.video.setPixmap(QPixmap.fromImage(image).scaled(
            self.video.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        now_ts = time.monotonic()
        if not hasattr(self, "_last_config_load") or (now_ts - getattr(self, "_last_config_load", 0.0)) > 1.5:
            try:
                from core.gesture_engine import load_gesture_config
                self.config = load_gesture_config()
                self._last_config_load = now_ts
            except Exception:
                pass

        custom_min_conf = 0.35
        custom_gesture = None
        custom_confidence = 0.0
        ambiguity = 0.0
        if normalized:
            from core.gesture_engine import match_custom_sample_details
            custom_gesture, custom_confidence, ambiguity = match_custom_sample_details(
                normalized,
                self.config.get("custom_gestures", []),
                threshold=0.28,
                min_confidence=custom_min_conf,
            )

        hand_sign = category if category.startswith("2:") else ""
        primary_category = category.split(":", 1)[1].split("+", 1)[0] if ":" in category else category
        standard_gesture = {
            "Victory": "Peace Sign",
            "ILoveYou": "Iron Man Repulsor Aim",
            "Closed_Fist": "Fist",
            "Pointing_Up": "Pointing",
            "Thumb_Up": "Thumbs Up",
            "Thumb_Down": "Thumbs Down",
            "Open_Palm": "Open Palm",
        }.get(primary_category, primary_category.replace("_", " ") if primary_category else "")

        # Check if standard gesture maps to any user custom gesture pose
        from core.gesture_engine import _clean_gesture_name
        matched_dropdown_custom = None
        if standard_gesture:
            std_clean = _clean_gesture_name(standard_gesture)
            for cg in self.config.get("custom_gestures", []):
                cg_pose_clean = _clean_gesture_name(cg.get("pose", ""))
                if " hold" in cg_pose_clean:
                    cg_pose_clean = cg_pose_clean.replace(" hold", "").strip()
                if std_clean and cg_pose_clean == std_clean:
                    matched_dropdown_custom = cg
                    break

        conf_threshold = float(self.config.get("confidence_threshold", 0.75))
        mapped_hand_sign = hand_sign in self.config.get("hand_sign_mappings", {})
        if hand_sign and mapped_hand_sign:
            gesture = f"Two hands: {hand_sign}"
            shown_confidence = confidence
            shown_ambiguity = 0.0
            display_meaning = meaning
        elif custom_gesture and custom_confidence >= custom_min_conf:
            gesture = custom_gesture
            shown_confidence = custom_confidence
            shown_ambiguity = ambiguity
            display_meaning = f"Custom: {custom_gesture}"
        elif hand_sign:
            gesture = f"Two hands: {hand_sign}"
            shown_confidence = confidence
            shown_ambiguity = 0.0
            display_meaning = meaning
        elif matched_dropdown_custom and confidence >= min(conf_threshold, 0.65):
            gesture = matched_dropdown_custom.get("name", standard_gesture)
            shown_confidence = confidence
            shown_ambiguity = 0.0
            display_meaning = f"Custom: {gesture} ({standard_gesture})"
        elif standard_gesture and confidence >= conf_threshold:
            gesture = standard_gesture
            shown_confidence = confidence
            shown_ambiguity = 0.0
            display_meaning = GESTURE_MEANINGS.get(primary_category, primary_category.replace("_", " "))
        elif standard_gesture and confidence > 0.0:
            gesture = standard_gesture
            shown_confidence = confidence
            shown_ambiguity = 0.0
            display_meaning = GESTURE_MEANINGS.get(primary_category, primary_category.replace("_", " "))
        else:
            gesture = "No hand detected" if not normalized else "Unknown pose"
            shown_confidence = max(confidence, custom_confidence)
            shown_ambiguity = ambiguity
            display_meaning = meaning if meaning and meaning != "No hand detected" else gesture

        action = self._mapped_action(gesture, hand_sign if mapped_hand_sign else "")
        self.gesture.setText(f"Detected gesture: {gesture}")
        self.meaning.setText(f"Mapped action: {action or 'No action mapped'}")

        # Highlight when target gesture matches (by gesture name, pose, or target name)
        target_matched = False
        if self.target_gesture:
            tgt = self.target_gesture.lower().strip()
            gest = gesture.lower().strip()
            clean_gest = re.sub(r"^[^\w\d]+", "", gest).strip()
            clean_tgt = re.sub(r"^[^\w\d]+", "", tgt).strip()
            if gest == tgt or clean_gest == clean_tgt or gest == clean_tgt:
                target_matched = True
            else:
                for item in self.config.get("custom_gestures", []):
                    c_name = str(item.get("name", "")).strip().lower()
                    c_pose = str(item.get("pose", "")).strip().lower()
                    clean_c_pose = re.sub(r"^[^\w\d]+", "", c_pose).strip()
                    if c_name in {tgt, clean_tgt} and (gest in {c_pose, clean_c_pose} or clean_gest in {c_pose, clean_c_pose}):
                        target_matched = True
                        break

        if target_matched:
            self.gesture.setStyleSheet("font-size:20px;font-weight:700;color:#00e676")
            self.notice.setText(f"✓ MATCHED: '{self.target_gesture}' recognized successfully!")
            self.notice.setStyleSheet("font-size:13px;font-weight:700;color:#00e676;padding:8px")
        elif self.target_gesture:
            self.gesture.setStyleSheet("font-size:20px;font-weight:600;color:#54d6ff")
            self.notice.setText(f"PREVIEW ONLY — gestures shown here will not execute actions — show: {self.target_gesture}.")
            self.notice.setStyleSheet("font-size:13px;font-weight:700;color:#54d6ff;padding:8px")
        else:
            self.gesture.setStyleSheet("font-size:20px;font-weight:600;color:#54d6ff")

        self.confidence.setText(
            f"{display_meaning}  •  Confidence: {shown_confidence:.0%}  •  Ambiguity: {shown_ambiguity:.0%}"
            if normalized else "Confidence: —  •  Ambiguity: —"
        )
        if self._is_capturing and not self._capture_completed and normalized:
            if self._capture_samples and len(normalized) != len(self._capture_samples[0]):
                self._capture_samples.clear()
            self._capture_samples.append(normalized)
            if len(self._capture_samples) >= 8:
                count = len(self._capture_samples)
                template = [
                    sum(sample[index] for sample in self._capture_samples) / count
                    for index in range(len(normalized))
                ]
                self._is_capturing = False
                self._capture_completed = True
                self.capture_button.setStyleSheet("padding:10px;font-weight:700;color:#07111d;background:#00e676")
                self.capture_button.setText("Pose captured successfully! Auto-registering… ✓")
                name = self.capture_request.get('name', '') if self.capture_request else ''
                self.guide.setText(
                    f"<b>CAPTURE SUCCESSFUL</b><br>"
                    f"<span style='color:#00e676;font-size:15px;'>✓ Pose for '{name}' captured! Registering into Jarvis…</span>"
                )
                self.gesture_captured.emit({
                    "request": self.capture_request or {},
                    "landmark_template": template,
                    "sample_count": count,
                })
                self.capture_button.setEnabled(False)
                QTimer.singleShot(800, self.close)

    def _mapped_action(self, gesture, hand_sign=""):
        if hand_sign:
            action = self.config.get("hand_sign_mappings", {}).get(hand_sign)
            if action:
                return str(action)
        normalized = " ".join(gesture.casefold().split())
        clean_normalized = re.sub(r"^[^\w\d]+", "", normalized).strip()
        for item in self.config.get("custom_gestures", []):
            name = str(item.get("name", ""))
            pose = str(item.get("pose", ""))
            clean_pose = re.sub(r"^[^\w\d]+", "", pose).strip().casefold()
            clean_name = re.sub(r"^[^\w\d]+", "", name).strip().casefold()
            aliases = {
                " ".join(name.casefold().split()),
                clean_name,
                " ".join(pose.casefold().split()),
                clean_pose,
            }
            if " hold" in clean_pose:
                aliases.add(clean_pose.replace(" hold", ""))
            if normalized in aliases or clean_normalized in aliases:
                return str(item.get("action", ""))
        mappings = self.config.get("default_mappings", {})
        for name, action in mappings.items():
            if normalized == " ".join(str(name).casefold().split()) or clean_normalized == " ".join(str(name).casefold().split()):
                return str(action)
        hand_mappings = self.config.get("hand_sign_mappings", {})
        for name, action in hand_mappings.items():
            if normalized in {" ".join(f"hand sign {name}".casefold().split()), " ".join(str(name).casefold().split())} or clean_normalized == " ".join(str(name).casefold().split()):
                return str(action)
        return ""

    def _show_error(self, message):
        self.video.setText(f"Gesture preview unavailable\n{message}")
        self.gesture.setText("Gesture: —")
        self.meaning.setText("Mapped action: —")
        self.confidence.setText("Confidence: —")

    def closeEvent(self, event):
        if hasattr(self, '_countdown_timer') and self._countdown_timer.isActive():
            self._countdown_timer.stop()
        self.camera.requestInterruption()
        if self.camera.isRunning():
            self.camera.wait(1500)
        try:
            self.closed.emit()
        except Exception:
            pass
        super().closeEvent(event)

