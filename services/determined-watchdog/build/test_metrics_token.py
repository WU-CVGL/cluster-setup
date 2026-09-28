import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metrics_token import write_metrics_token


class MetricsTokenTest(unittest.TestCase):
    def test_rotation_and_rejected_refresh_preserve_private_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            write_metrics_token(path, "first-test-token")
            write_metrics_token(path, "second-test-token")
            self.assertEqual(path.read_text(), "second-test-token\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for bad in ["", "token\ninjected", "a b", None]:
                with self.assertRaises(ValueError):
                    write_metrics_token(path, bad)
            with patch("metrics_token.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    write_metrics_token(path, "third-test-token")
            self.assertEqual(path.read_text(), "second-test-token\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
