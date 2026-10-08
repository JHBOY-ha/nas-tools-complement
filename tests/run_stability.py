"""Run the stability regressions offline with disposable configuration and databases.

Usage: python3 -m tests.run_stability [tests.test_module ...]
"""
import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch


DATABASE_REVIEW_FIXES = (
    'tests.test_database_review_fixes',
    # Only corrected behaviors belong in the default suite; the remaining
    # review observations are intentionally kept in the explicit audit runner.
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_01_low_space_blocks_prepare_and_real_write',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_03_matching_schema_skips_payload_hashes',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_04_sync_rejection_preserves_previous_cache',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_06_failed_boots_reuse_identical_migration_backup',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_10_pending_restore_can_be_canceled_and_archive_explicitly_deleted',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_11_baseline_snapshot_works_without_git_history',
    'tests.test_deepseek_audit_reproduction.DeepSeekReproduction.test_12_update_without_prepare_bootstraps_safely',
)


STABILITY_MODULES = (
    "tests.test_stability_regressions",
    # Simulated workloads verify budgets without probing the production NAS.
    "tests.test_workload_limits",
    "tests.test_b4_hardening",
    "tests.test_review_completion",
    # WAL itself has a mandatory fixed-runtime acceptance runner. These real
    # file/transaction/migration cases also run on the legacy DELETE fallback.
    "tests.test_database_governance",
    "tests.test_database_migrations",
    "tests.test_subtitle_tasks",
    "tests.test_subtitle_transaction_safety",
    "tests.test_subtitle_task_pipeline",
    "tests.test_subtitle_upload",
    "tests.test_subtitle_performance",
    "tests.test_subtitle_season_pack",
    "tests.test_subtitle_align",
    "tests.test_subtitle_task_security",
) + DATABASE_REVIEW_FIXES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module, class or method names")
    args = parser.parse_args()
    network_attempts = []

    def deny_network(*_args, **_kwargs):
        # Even if application code catches the exception, the final exit status
        # must expose an accidentally unmocked network request.
        network_attempts.append(True)
        raise RuntimeError("Stability tests must mock external network calls")

    with tempfile.TemporaryDirectory(prefix="nas-stability-tests-") as directory:
        config_path = Path(directory) / "config.yaml"
        config_path.write_text("app: {}\nmedia: {}\npt: {}\nllm:\n  enable: false", encoding="utf-8")
        # Configure isolation before importing any application module: Config and
        # database engines are created at import time in this project.
        with patch.dict(os.environ, {"NASTOOL_CONFIG": str(config_path), "NASTOOL_OFFLINE_TESTS": "1"}), \
                patch.object(socket.socket, "connect", side_effect=deny_network), \
                patch.object(socket.socket, "connect_ex", side_effect=deny_network), \
                patch.object(socket, "getaddrinfo", side_effect=deny_network):
            from app.db import init_db
            init_db()
            suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests or STABILITY_MODULES)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        if network_attempts:
            print("FAILED: %d unmocked network attempt(s) were blocked" % len(network_attempts))
        return 0 if result.wasSuccessful() and not network_attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
