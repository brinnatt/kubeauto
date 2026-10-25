import json
import unittest
from tests.unit.test_longhorn_delivery import product, FakeAPI, native, configuration
from tests.unit.test_longhorn_v2 import v2_configuration


class CPUIsolationPolicy(unittest.TestCase):
    def test_anonymous_memory_and_exclusive_cpu_reject_before_runtime_changes(self):
        c = v2_configuration()
        c.update(longhorn_v2_cpu_cores=2, longhorn_v2_hugepages=False)
        with self.assertRaisesRegex(ValueError, 'anonymous memory with exclusive CPU'):
            product.validate(c)
        c['longhorn_v2_hugepages'] = True
        product.validate(c)
        c.update(longhorn_v2_cpu_cores=0, longhorn_v2_hugepages=False)
        product.validate(c)

    def test_optional_setting_preserves_unmanaged_policy(self):
        c = v2_configuration()
        self.assertNotIn('dataEngineCPUIsolationEnabled', product.chart_values(c)['defaultSettings'])
        for value in (False, True):
            c['longhorn_v2_cpu_isolation_enabled'] = value
            product.validate(c)
            self.assertEqual(product.chart_values(c)['defaultSettings']['dataEngineCPUIsolationEnabled'], {'v2':str(value).lower()})
        c['longhorn_version']='1.12.0'
        with self.assertRaisesRegex(ValueError, '1.12.1'): product.validate(c)
        c['longhorn_v2_cpu_isolation_enabled']=False
        product.validate(c)
        self.assertNotIn('dataEngineCPUIsolationEnabled', product.chart_values(c)['defaultSettings'])
        c=configuration();c['longhorn_v2_cpu_isolation_enabled']=True
        with self.assertRaisesRegex(ValueError,'enabled V2'): product.validate(c)

    def test_override_global_config_and_native_precedence(self):
        c=v2_configuration();api=FakeAPI();im=native('InstanceManager','im',{})
        setting=native('Setting','data-engine-cpu-isolation-enabled');setting['value']='{"v2":"true"}'
        api.put('settings.longhorn.io',setting,product.NAMESPACE)
        self.assertTrue(product.v2_cpu_isolation(c,api,im))
        c['longhorn_v2_cpu_isolation_enabled']=False
        self.assertFalse(product.v2_cpu_isolation(c,api,im))
        im['spec']['dataEngineSpec']={'v2':{'cpuIsolationEnabled':'true'}}
        self.assertTrue(product.v2_cpu_isolation(c,api,im))
        im['spec']['dataEngineSpec']['v2']['cpuIsolationEnabled']='false'
        c['longhorn_v2_cpu_isolation_enabled']=True
        self.assertFalse(product.v2_cpu_isolation(c,api,im))
        im['spec']['dataEngineSpec']['v2']['cpuIsolationEnabled']=''
        del c['longhorn_v2_cpu_isolation_enabled']
        for value in ('{}','{"v2":true}','{"v2":"invalid"}','broken'):
            setting['value']=value;api.put('settings.longhorn.io',setting,product.NAMESPACE)
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'native V2'): product.v2_cpu_isolation(c,api,im)

    def test_native_override_rejects_invalid_value(self):
        c=v2_configuration();api=FakeAPI();im=native('InstanceManager','im',{'dataEngineSpec':{'v2':{'cpuIsolationEnabled':'TRUE'}}})
        with self.assertRaisesRegex(ValueError,'per-instance'): product.v2_cpu_isolation(c,api,im)
