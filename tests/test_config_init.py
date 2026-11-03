"""Configuration initialization with disposable files only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import config


class ConfigInitTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="nas-config-init-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load_config(self, path):
        # Reset the singleton without touching any caller's configuration instance.
        with patch.object(config, "_CONFIG", None), \
                patch.dict(os.environ, {"NASTOOL_CONFIG": str(path)}), \
                patch.object(sys, "path", sys.path.copy()):
            return config.Config()

    def test_missing_config_is_copied_from_repository_example(self):
        target = self.root / "config.yaml"
        example = Path(config.__file__).parent / "config" / "config.example.yaml"
        expected = example.read_bytes()

        instance = self.load_config(target)

        self.assertEqual(target.read_bytes(), expected)
        self.assertIsInstance(instance.get_config("app"), dict)
        self.assertEqual(example.read_bytes(), expected)

    def test_existing_config_is_loaded_without_copying_template(self):
        target = self.root / "config.yaml"
        original = b"# existing user settings\napp:\n  login_user: existing-user\ncustom:\n  preserved: true\n"
        target.write_bytes(original)

        with patch.object(config.shutil, "copy", side_effect=AssertionError("Existing config must not be overwritten")):
            instance = self.load_config(target)

        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(instance.get_config("app")["login_user"], "existing-user")
        self.assertTrue(instance.get_config("custom")["preserved"])

    def test_custom_config_path_and_save_leave_template_unchanged(self):
        target = self.root / "custom-settings.yaml"
        example = Path(config.__file__).parent / "config" / "config.example.yaml"
        expected = example.read_bytes()
        instance = self.load_config(target)

        instance.save_config({"app": {"login_user": "saved-user"}})
        reloaded = self.load_config(target)

        self.assertEqual(reloaded.get_config("app")["login_user"], "saved-user")
        self.assertEqual(example.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
