import io
import sys
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from hidemyemail_generator.webapp import _configure_utf8_stdio, create_app


class WebAppStdioTests(unittest.TestCase):
    def test_reconfigures_gbk_streams_before_rich_writes_unicode(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            sys.stdout = stdout
            sys.stderr = stderr
            _configure_utf8_stdio()

            self.assertEqual(stdout.encoding.lower(), "utf-8")
            self.assertEqual(stderr.encoding.lower(), "utf-8")
            Console(file=stdout, force_terminal=False).print(":star:")
            stdout.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_default_openai_runtime_uses_current_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "hidemyemail-generator"
            app = create_app(base_dir=base_dir)

            self.assertEqual(
                app["browser_manager"].target_project_dir,
                (base_dir.parent / "openai-register-paylink").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
