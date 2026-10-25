"""Bounded filesystem baseline; buffered I/O is explicitly distinct from fio/direct I/O."""
import hashlib
import math
import os
from pathlib import Path
import random
import time


def benchmark(directory, seed, *, dataset_bytes=64 * 1024**2, sequential_operations=256,
              random_operations=4096):
    """Verify every block; acknowledge writes only after a successful file fsync."""
    block = 4096
    if (type(dataset_bytes) is not int or not block <= dataset_bytes <= 128 * 1024**2 or
            dataset_bytes % block or type(sequential_operations) is not int or
            type(random_operations) is not int or not 1 <= sequential_operations <= 8192 or
            not 1 <= random_operations <= 8192 or not isinstance(seed, str) or not seed):
        raise ValueError('invalid bounded filesystem workload')
    path = Path(directory) / 'kubeauto-performance'
    # O_EXCL refuses existing files and symlinks. Never truncate foreign data.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    count = dataset_bytes // block
    expected = [None] * count
    rng = random.Random(seed)
    results = []

    def data(index, revision):
        return hashlib.sha256((seed + ':' + str(index) + ':' + str(revision)).encode()).digest() * 128

    def verify(offset, content):
        if len(content) % block:
            raise ValueError('short benchmark read')
        for index in range(len(content) // block):
            if hashlib.sha256(content[index * block:(index + 1) * block]).hexdigest() != expected[offset // block + index]:
                raise ValueError('benchmark data corruption')

    try:
        for index in range(count):
            content = data(index, 0)
            if os.pwrite(fd, content, index * block) != block:
                raise ValueError('short benchmark initialization write')
            expected[index] = hashlib.sha256(content).hexdigest()
        os.fsync(fd)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        for mode in ('sequential-write', 'random-write', 'sequential-read', 'random-read'):
            size = min(1024**2, dataset_bytes) if mode.startswith('sequential') else block
            operations = sequential_operations if mode.startswith('sequential') else random_operations
            latencies = []
            transferred = 0
            started = time.monotonic_ns()
            for operation in range(operations):
                offset = ((operation * size) % dataset_bytes if mode.startswith('sequential') else
                          rng.randrange(count) * block)
                # A small deterministic fixture may end with a partial chunk.
                length = min(size, dataset_bytes - offset)
                transferred += length
                if mode.endswith('write'):
                    content = b''.join(data(index, operation + 1) for index in
                                       range(offset // block, (offset + length) // block))
                    io_started = time.monotonic_ns()
                    if os.pwrite(fd, content, offset) != length:
                        raise ValueError('short benchmark write')
                    os.fsync(fd)
                    latencies.append(time.monotonic_ns() - io_started)
                    for index in range(length // block):
                        expected[offset // block + index] = hashlib.sha256(content[index * block:(index + 1) * block]).hexdigest()
                else:
                    io_started = time.monotonic_ns()
                    content = os.pread(fd, length, offset)
                    latencies.append(time.monotonic_ns() - io_started)
                    if len(content) != length:
                        raise ValueError('short benchmark read')
                    verify(offset, content)
            elapsed = max(1, time.monotonic_ns() - started) / 1e9
            ordered = sorted(latencies)
            results.append({'mode': mode, 'operations': operations, 'io_size_bytes': size,
                'elapsed_s': elapsed, 'iops': operations / elapsed,
                'transferred_bytes': transferred, 'throughput_bytes_s': transferred / elapsed,
                'latency_ms': {str(p): ordered[math.ceil(len(ordered) * p / 100) - 1] / 1e6
                               for p in (50, 95, 99)}})
        for index in range(count):
            content = os.pread(fd, block, index * block)
            if len(content) != block:
                raise ValueError('short final benchmark read')
            verify(index * block, content)
        return {'dataset_bytes': dataset_bytes, 'block_bytes': block, 'concurrency': 1,
            'buffered_io': True, 'write_fsync_per_operation': True, 'errors': 0,
            'verified_blocks': count, 'results': results,
            'boundary': 'Warm page cache; single client; throughput includes generation/verification overhead; '
                        'latency covers only pread or pwrite+fsync. Not a raw-device, cold-cache or production limit.'}
    finally:
        os.close(fd)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError('benchmark file replaced; refusing cleanup')
        path.unlink()
