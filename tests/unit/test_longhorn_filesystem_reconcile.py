"""Native V1 additions beside existing V2 disks; initial bootstrap stays upstream-owned."""
import copy
import inspect
import unittest
from unittest.mock import patch
from tests.unit.test_longhorn_delivery import product, FakeAPI, native


class FilesystemReconciliation(unittest.TestCase):
 def setUp(self):
  self.c={'longhorn_storage_nodes':['approved'],'longhorn_disk_receipts':{'approved':{'path':'/data/approved','total_bytes':24000}}}
  self.nodes={'approved':{'metadata':{'name':'approved-node'}}};self.api=FakeAPI()
  self.block={'diskType':'block','path':'/dev/disk/by-id/owned','allowScheduling':False,'tags':['keep']}
  self.node=native('Node','approved-node',{'disks':{'raw':copy.deepcopy(self.block)}})
  self.api.put('nodes.longhorn.io',self.node,product.NAMESPACE)
 def run_reconcile(self):return product.reconcile_filesystem_disks(self.c,self.api,self.nodes)
 def test_existing_raw_disk_gets_only_verified_filesystem_patch(self):
  self.assertTrue(self.run_reconcile())
  args=self.api.writes[0][1];self.assertEqual(args[:2],('nodes.longhorn.io','approved-node'))
  self.assertEqual(args[2]['metadata'],{'uid':self.node['metadata']['uid'],'resourceVersion':'1'})
  disks=args[2]['spec']['disks'];self.assertEqual(set(disks),{'kubeauto-data'})
  self.assertEqual(disks['kubeauto-data'],{'path':'/data/approved','diskType':'filesystem',
    'allowScheduling':True,'evictionRequested':False,'storageReserved':2400,'tags':[]})
  self.assertEqual(self.node['spec']['disks']['raw'],self.block)
 def test_upstream_initial_bootstrap_and_existing_admin_maintenance_remain_unchanged(self):
  for disks in ({},{'raw':self.block,'admin-name':{'path':'/data/approved','diskType':'filesystem','allowScheduling':False,'storageReserved':999,'tags':['keep']}}):
   self.node['spec']['disks']=copy.deepcopy(disks);self.api.writes=[]
   self.assertTrue(self.run_reconcile());self.assertEqual(self.api.writes,[])
 def test_conflicting_name_path_or_other_filesystem_refuses_before_patch(self):
  for disks in ({'kubeauto-data':self.block},{'raw':dict(self.block,path='/data/approved')},
      {'raw':self.block,'other':{'path':'/data/other','diskType':'filesystem'}},
      {'a':{'path':'/data/approved'},'b':{'path':'/data/approved'}}):
   self.node['spec']['disks']=copy.deepcopy(disks)
   with self.assertRaises(ValueError):self.run_reconcile()
   self.assertEqual(self.api.writes,[])
 def test_missing_node_waits_and_replaced_node_refuses(self):
  with patch.object(self.api,'get',return_value=None):self.assertFalse(self.run_reconcile())
  self.run_reconcile();self.node['metadata']['uid']='replacement';self.api.writes=[]
  with self.assertRaisesRegex(ValueError,'identity changed'):self.run_reconcile()
  self.assertEqual(self.api.writes,[])
 def test_conflicts_refresh_only_same_node_and_retry_is_bounded(self):
  error=product.CommandError('kubectl',1,'Error from server (Conflict): stale')
  with patch.object(self.api,'patch',side_effect=[error,None]) as write:
   self.assertTrue(self.run_reconcile());self.assertEqual(write.call_count,2)
  with patch.object(self.api,'patch',side_effect=error) as write:
   with self.assertRaisesRegex(ValueError,'retry limit'):self.run_reconcile()
   self.assertEqual(write.call_count,3)
 def test_original_control_ready_consumes_filesystem_before_health(self):
  source=inspect.getsource(product.reconcile)
  self.assertIn('return reconcile_filesystem_disks(c, api, nodes) and reconcile_block_disks(c, api, nodes) and healthy(c, api, nodes)',source)
