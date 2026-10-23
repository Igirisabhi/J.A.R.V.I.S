import json
import tempfile
import unittest
from pathlib import Path

from core.config_store import save_json_atomic


class ConfigStoreTests(unittest.TestCase):
    def test_atomic_write_creates_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_json_atomic(path, {"enabled": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"enabled": True})

    def test_validation_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"safe": true}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                save_json_atomic(path, {"safe": False}, validator=lambda _: (_ for _ in ()).throw(ValueError("invalid")))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"safe": True})


if __name__ == "__main__":
    unittest.main()
