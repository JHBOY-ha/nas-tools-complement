"""Disposable workload smoke test; never load application config or SQLite.

Run locally by default. --scratch-root may point to an explicitly selected NAS
test directory; only a uniquely created temporary subtree is written/removed.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch-root', type=Path)
    parser.add_argument('--files', type=int, default=8)
    parser.add_argument('--file-mib', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.files <= 32 or not 1 <= args.file_mib <= 64:
        parser.error('Use 1–32 files of 1–64 MiB')
    root = Path(__file__).resolve().parents[1]
    workload = load('nas_workload_verify', root / 'app/utils/workload.py')
    isolated = load('nas_io_verify', root / 'app/utils/isolated_io.py')
    pool = isolated.IsolationPool(2, 16)
    gate = workload.FairConcurrencyGate(2)
    executor = workload.BoundedExecutor(4, 32, 'nas-verify')
    lock = threading.Lock()
    active = peak = 0
    timings = []
    try:
        with tempfile.TemporaryDirectory(prefix='nas-tools-workload-verify-', dir=args.scratch_root) as temporary:
            source = Path(temporary) / 'source.bin'
            block = b'nas-workload-smoke\n' * 4096
            with source.open('wb') as stream:
                remaining = args.file_mib * 1024 * 1024
                while remaining:
                    data = block[:remaining]
                    stream.write(data); remaining -= len(data)
            expected = pool.execute('hash', path=str(source))
            def copy(number):
                nonlocal active, peak
                started = time.monotonic()
                with gate.slot():
                    with lock:
                        active += 1; peak = max(peak, active)
                    try:
                        target = Path(temporary) / ('copy-%s.bin' % number)
                        pool.execute('transfer', source=str(source), target=str(target), mode='copy', timeout=120)
                        if pool.execute('hash', path=str(target), timeout=120) != expected:
                            raise RuntimeError('Copy content differs')
                    finally:
                        with lock: active -= 1
                timings.append(time.monotonic() - started)
            started = time.monotonic()
            futures = [executor.submit(copy, number) for number in range(args.files)]
            for future in futures: future.result(180)
            report = {'files': args.files, 'file_mib': args.file_mib, 'verified_bytes': args.files * args.file_mib * 1024 * 1024,
                      'elapsed_seconds': round(time.monotonic() - started, 3),
                      'per_file_median_seconds': round(statistics.median(timings), 3),
                      'peak_transfer_concurrency': peak, 'limit': 2,
                      'processes': pool.snapshot(), 'scratch_parent': str(Path(temporary).parent)}
            if peak > 2: raise RuntimeError('Transfer concurrency exceeded budget')
            print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        executor.shutdown(); pool.close()


if __name__ == '__main__':
    main()
