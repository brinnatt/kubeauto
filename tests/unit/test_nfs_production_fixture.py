import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('nfs_fixture', ROOT/'tests/helpers/nfs-production-fixture.py')
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class NfsProductionFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory=Path(self.temporary.name)
        self.calls=[]
        self.states={'nfs-server.service':'inactive','rpcbind.service':'active','rpcbind.socket':'active'}
        self.active=''
        self.export=directory/'exports.d/test.exports'
        self.data=directory/'data'
        for name,value in [('STATE',directory/'owner.json'),('DATA',self.data),('EXPORT',self.export),
                           ('CONTENT',str(self.data)+' '+ ' '.join(h+'(rw,sync,no_subtree_check,no_root_squash)' for h in fixture.CLIENTS)+'\n')]:
            p=patch.object(fixture,name,value);p.start();self.addCleanup(p.stop)
        for name,value in [('require_host',lambda:None),('boot_id',lambda:'boot-fixture'),('command',self.command)]:
            p=patch.object(fixture,name,value);p.start();self.addCleanup(p.stop)

    def command(self,*args,check=True):
        self.calls.append(args)
        output=''
        if args[:2]==('systemctl','is-active'):output=self.states[args[2]]
        elif args[:2]==('systemctl','start'):
            for unit in args[2:]:self.states[unit]='active'
        elif args[:2]==('systemctl','stop'):
            for unit in args[2:]:self.states[unit]='inactive'
        elif args==('exportfs','-s'):output=self.active
        elif args==('exportfs','-r'):self.active=fixture.CONTENT
        elif args[:2]==('exportfs','-u'):self.active=''
        return subprocess.CompletedProcess(args,0,stdout=output)

    def test_export_is_owned_and_original_active_rpcbind_is_preserved(self):
        fixture.prepare('run-1')
        self.assertEqual(fixture.STATE.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.export.read_text(),fixture.CONTENT)
        (self.data/'payload').write_text('business')
        fixture.cleanup('run-1')
        self.assertFalse(fixture.STATE.exists())
        self.assertFalse(self.export.exists())
        self.assertFalse(self.data.exists())
        self.assertEqual(self.states,{'nfs-server.service':'inactive','rpcbind.service':'active','rpcbind.socket':'active'})
        self.assertEqual([c[2] for c in self.calls if c[:2]==('exportfs','-u')],
                         [host+':'+str(self.data) for host in fixture.CLIENTS])
        self.assertNotIn(('exportfs','-ua'),self.calls)

    def test_partial_prepare_without_loaded_export_is_recoverable(self):
        original=fixture.command
        def fail_start(*args,**kwargs):
            if args[:2]==('systemctl','start'):raise RuntimeError('fixture start failure')
            return original(*args,**kwargs)
        with patch.object(fixture,'command',fail_start):
            with self.assertRaises(RuntimeError):fixture.prepare('run-1')
        fixture.cleanup('run-1')
        self.assertFalse(fixture.STATE.exists())
        self.assertFalse(self.data.exists())

    def test_foreign_state_and_changed_declaration_refuse_mutation(self):
        fixture.prepare('run-1')
        for kind in ('run','declaration','foreign-export','symlink'):
            with self.subTest(kind=kind):
                self.active=fixture.CONTENT
                self.export.write_text(fixture.CONTENT)
                run='run-1'
                if kind=='run':run='other-run'
                if kind=='declaration':self.export.write_text('/foreign\n')
                if kind=='foreign-export':self.active+='/foreign 192.168.122.212(rw)\n'
                if kind=='symlink':self.export.unlink();self.export.symlink_to(self.data/'outside')
                before=len(self.calls)
                with self.assertRaises(RuntimeError):fixture.cleanup(run)
                self.assertFalse(any(c[:2] in (('exportfs','-u'),('systemctl','stop')) for c in self.calls[before:]))
                self.assertTrue(fixture.STATE.exists())
                if kind=='symlink':self.export.unlink()

    def test_existing_exports_or_fixture_never_get_adopted(self):
        self.active='/foreign 192.168.122.212(rw)\n'
        with self.assertRaises(RuntimeError):fixture.prepare('run-1')
        self.assertFalse(fixture.STATE.exists())
        self.active=''
        self.data.mkdir()
        with self.assertRaises(RuntimeError):fixture.prepare('run-1')
        self.assertFalse(fixture.STATE.exists())


if __name__=='__main__':unittest.main()
