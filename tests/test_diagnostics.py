import unittest
from core.diagnostics import diagnostics_exit_code, format_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_required_failure_returns_nonzero(self):
        results = [{"name": "config", "ok": False, "detail": "broken", "required": True}]
        self.assertEqual(diagnostics_exit_code(results), 1)
        self.assertIn("[FAIL]", format_diagnostics(results))

    def test_optional_failure_is_warning(self):
        results = [{"name": "camera", "ok": False, "detail": "missing", "required": False}]
        self.assertEqual(diagnostics_exit_code(results), 0)
        self.assertIn("[WARN]", format_diagnostics(results))


if __name__ == "__main__":
    unittest.main()
