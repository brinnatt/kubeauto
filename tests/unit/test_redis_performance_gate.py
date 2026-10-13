"""Percentile and actual owned-resource statistics contracts."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_performance', ROOT / 'tests/helpers/redis-performance-gate.py')
performance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(performance)


class RedisPerformanceTests(unittest.TestCase):
    def test_percentiles_use_nearest_rank_and_keep_outliers(self):
        samples = list(reversed(range(1, 101)))
        self.assertEqual(performance.percentile(samples, .50), 50)
        self.assertEqual(performance.percentile(samples, .95), 95)
        self.assertEqual(performance.percentile(samples, .99), 99)
        self.assertEqual(performance.percentile([1, 10000], .99), 10000)
        for values, fraction in (([], .99), ([1], 0), ([1], 1.01)):
            with self.assertRaises(ValueError):
                performance.percentile(values, fraction)

    def test_stats_keep_exact_namespace_and_pods_and_measure_only_pvc_disk(self):
        def pod(namespace, name):
            return {'podRef': {'namespace': namespace, 'name': name, 'uid': 'owned'},
                    'containers': [{'cpu': {'usageCoreNanoSeconds': 10}, 'memory': {'workingSetBytes': 20}},
                                   {'cpu': {'usageCoreNanoSeconds': 3}, 'memory': {'workingSetBytes': 5}}],
                    'volume': [{'name': 'data', 'usedBytes': 123, 'pvcRef': {'name': 'data-pvc'}},
                               {'name': 'tls', 'usedBytes': 9999}],
                    'network': {'rxBytes': 30, 'txBytes': 40}}
        sample = performance.resource_sample({'pods': [pod('redis-gate', 'data-0'),
            pod('customer', 'data-0'), pod('redis-gate', 'foreign')]}, 'redis-gate', {'data-0': 'owned'})
        self.assertEqual(sample, {'data-0': {'uid': 'owned', 'cpu_ns': 13, 'memory_bytes': 25,
                                          'pvc_used_bytes': 123, 'rx_bytes': 30, 'tx_bytes': 40}})
        bad = pod('redis-gate', 'data-0')
        del bad['network']['rxBytes']
        with self.assertRaises(KeyError):
            performance.resource_sample({'pods': [bad]}, 'redis-gate', {'data-0': 'owned'})
        with self.assertRaisesRegex(ValueError, 'stale Pod'):
            performance.resource_sample({'pods': [pod('redis-gate', 'data-0')]}, 'redis-gate', {'data-0': 'current'})


if __name__ == '__main__':
    unittest.main()
