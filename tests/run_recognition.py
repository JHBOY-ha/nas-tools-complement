"""Run the recognition regressions offline with disposable configuration and databases.

Usage: python3 -m tests.run_recognition [tests.test_module ...]
"""
import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch


# Keep the complete recognition workflow in one reproducible entry point, including
# downstream download guards; method counts are not a real-world accuracy metric.
RECOGNITION_MODULES = (
    "tests.test_metainfo",
    "tests.test_ordinal_seasons",
    "tests.test_metadata_binding_guards",
    "tests.test_meta_recognition_plan",
    "tests.test_fractional_versions",
    "tests.test_special_episodes",
    "tests.test_special_confirmation",
    "tests.test_review_regressions",
    "tests.test_recognition_performance",
    "tests.test_llm_season_binding",
    "tests.test_media_cn_fallback",
    "tests.test_meta_llm_parser",
    "tests.test_sync_reliability",
    "tests.test_download_dedupe",
    "tests.test_rss_prefilter",
    "tests.test_media_cache_numbering",
    "tests.test_media_recognition_integrity",
    "tests.test_meta_parser_boundaries",
    "tests.test_meta_multi_episode_guards",
    "tests.test_meta_recognition_matrix",
    "tests.test_media_identity_matrix",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module, class or method names")
    args = parser.parse_args()
    network_attempts = []

    def deny_network(*_args, **_kwargs):
        # Even if application code catches the exception, the final exit status
        # must expose an accidentally unmocked network request.
        network_attempts.append(True)
        raise RuntimeError("Recognition tests must mock external network calls")

    with tempfile.TemporaryDirectory(prefix="nas-recognition-tests-") as directory:
        config_path = Path(directory) / "config.yaml"
        config_path.write_text("app: {}\nmedia: {}\npt: {}\nllm:\n  enable: false\n", encoding="utf-8")
        # Configure isolation before importing any application module: Config and
        # database engines are created at import time in this project.
        with patch.dict(os.environ, {"NASTOOL_CONFIG": str(config_path)}), \
                patch.object(socket.socket, "connect", side_effect=deny_network), \
                patch.object(socket.socket, "connect_ex", side_effect=deny_network), \
                patch.object(socket, "getaddrinfo", side_effect=deny_network):
            from app.db import init_db
            init_db()
            suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests or RECOGNITION_MODULES)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        # Surface semantic sample counts separately from unittest method counts;
        # do not load extra modules when the caller selected only one test.
        matrix = sys.modules.get("tests.test_meta_recognition_matrix")
        if matrix:
            print("Filename matrix scenarios:", matrix.recognition_matrix_counts())
        identities = sys.modules.get("tests.test_media_identity_matrix")
        if identities:
            print("Identity success samples:", len(identities.SUCCESS_CASES))
        if network_attempts:
            print("FAILED: %d unmocked network attempt(s) were blocked" % len(network_attempts))
        return 0 if result.wasSuccessful() and not network_attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
