"""Actual Kafka CLI dispatch and native argv, plus deployment handoffs."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
TOOL=ROOT/'tools/kafka/KafkaCli.py'
CID='MkU3OEVBNTcwNTJENDM2Qk'


class KafkaParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='kafka-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.home=self.base/'kafka';(self.home/'bin').mkdir(parents=True)
        self.trace=self.base/'calls.jsonl'
        body='#!'+sys.executable+'\n'+r'''
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
record=dict(script=Path(sys.argv[0]).name,argv=args)
if 'producer' in record['script']: record['stdin']=sys.stdin.read()
if '--command-config' in args:
    p=Path(args[args.index('--command-config')+1])
    record['config']=p.read_text();record['mode']=p.stat().st_mode & 0o777
with open(os.environ['CALLS'],'a') as output: output.write(json.dumps(record)+'\n')
if '--verify' in args: print('Reassignment of partition events-0 is completed.')
elif 'consumer.sh' in record['script']: print('fixture-message')
else: print('fixture-result')
'''
        for name in ('kafka-topics','kafka-consumer-groups','kafka-console-producer','kafka-console-consumer',
                'kafka-metadata-quorum','kafka-configs','kafka-reassign-partitions','kafka-leader-election',
                'kafka-broker-api-versions','kafka-storage'):
            p=self.home/'bin'/(name+'.sh');p.write_text(body);p.chmod(0o700)
        with mock.patch.dict(os.environ,KAFKA_LOG_DIR=self.tmp.name):
            spec=importlib.util.spec_from_file_location('kafka_parameters',TOOL)
            self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def cli(self,*flags,expected=0):
        self.trace.unlink(missing_ok=True)
        result=subprocess.run([sys.executable,'-B',str(TOOL),'--kafka-home',str(self.home),
            '--bootstrap-server','broker.invalid:19092',*map(str,flags)],
            cwd=self.base,env=dict(os.environ,KAFKA_LOG_DIR=self.tmp.name,CALLS=str(self.trace)),
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,expected,(result.stdout+result.stderr)[-2500:])
        calls=[json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []
        return result,calls

    def test_all_native_admin_operations_receive_exact_options(self):
        topics=self.base/'topics.json';topics.write_text('{"topics":[{"topic":"events"}],"version":1}')
        reassign=self.base/'reassign.json';reassign.write_text('{"partitions":[],"version":1}')
        command_config=self.base/'client.properties';command_config.write_text('security.protocol=PLAINTEXT\n')
        cases=[
            (['--topic-create','--topic','events','--partitions','3','--replication-factor','2'],['--create','--topic','events','--partitions','3','--replication-factor','2']),
            (['--topic-delete','--topic','events'],['--delete','--topic','events']),
            (['--topic-describe','--topic','events'],['--describe','--topic','events']),
            (['--topic-list'],['--list']),
            (['--group-list'],['--list']),
            (['--group-describe','--consumer-group','fixture-group'],['--describe','--group','fixture-group']),
            (['--config-describe-broker','--config-entity-name','2'],['--entity-type','brokers','--entity-name','2','--describe']),
            (['--config-describe-topic','--topic','events'],['--entity-type','topics','--entity-name','events','--describe']),
            (['--quorum-add-controller','--bootstrap-controller','controller.invalid:19093'],['--bootstrap-controller','controller.invalid:19093','add-controller']),
            (['--preferred-replica-election'],['--election-type','PREFERRED','--all-topic-partitions']),
            (['--broker-decommission-generate','--broker-list','1,2','--topics-to-move-json',topics],['--generate','--broker-list','1,2','--topics-to-move-json-file',str(topics)]),
            (['--broker-decommission-execute','--reassignment-json-file',reassign,'--throttle','123456'],['--execute','--reassignment-json-file',str(reassign),'--throttle','123456']),
            (['--broker-decommission-verify','--reassignment-json-file',reassign],['--verify','--reassignment-json-file',str(reassign)]),
        ]
        for flags,tail in cases:
            with self.subTest(flags=flags):
                result,calls=self.cli(*flags,'--command-config',command_config)
                self.assertEqual(len(calls),1)
                args=calls[0]['argv']
                for item in tail: self.assertIn(item,args)
                self.assertEqual(args[args.index('--command-config')+1],str(command_config))
                conn='--bootstrap-controller' if 'add-controller' in args else '--bootstrap-server'
                self.assertLess(args.index('--command-config'),args.index(conn))

    def test_records_message_file_consumer_limits_and_private_sasl_config(self):
        flags=['--topic','events','--kafka-user','fixture-user','--kafka-password','private-secret',
               '--kafka-sasl-mechanism','SCRAM-SHA-256']
        result,calls=self.cli('--produce','--message','你好 JSON {"a":1}',*flags)
        self.assertEqual(calls[0]['stdin'],'你好 JSON {"a":1}\n')
        self.assertIn('SCRAM-SHA-256',calls[0]['config'])
        self.assertIn('private-secret',calls[0]['config'])
        self.assertEqual(calls[0]['mode'],0o600)
        self.assertNotIn('private-secret',result.stdout+result.stderr)
        source=self.base/'messages.txt';source.write_text('first\nsecond\n')
        _,calls=self.cli('--produce','--input-file',source,*flags)
        self.assertEqual([c['stdin'] for c in calls],['first\n','second\n'])
        result,calls=self.cli('--consume','--consumer-group','fixture-group','--max-messages','7',*flags)
        self.assertIn('fixture-message',result.stdout)
        args=calls[0]['argv'];self.assertEqual(args[args.index('--max-messages')+1],'7')
        self.assertIn('fixture-group',args)
        for options in (['--consume','--max-messages','0'],['--produce','--message','x','--input-file',source]):
            _,calls=self.cli(*options,'--topic','events',expected=1)
            self.assertEqual(calls,[])

    def routed(self,*flags,config=None,expected=0):
        config_flags=[]
        if config is not None:
            path=self.base/'config.json';path.write_text(json.dumps(config));config_flags=['--config',str(path)]
        with mock.patch.object(sys,'argv',['KafkaCli','--kafka-home',str(self.home),*config_flags,*map(str,flags)]), \
                mock.patch.object(self.m,'KafkaDeployer') as factory, \
                mock.patch.object(self.m.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            owner=factory.return_value
            for name in ('deploy_standalone','deploy_controller','deploy_broker','clean_deployment',
                         'verify_broker_started','verify_controller_started','verify_controller_quorum','show_cluster_status'):
                getattr(owner,name).return_value=True
            try: self.m.main();rc=0
            except SystemExit as error: rc=error.code
        self.assertEqual(rc,expected)
        return factory,owner

    def test_deployment_role_paths_identity_cluster_and_listeners_handoff(self):
        common=['--node-id','0','--advertised-host','node.invalid','--log-dirs',self.base/'data',
            '--java-home','/fixture/jdk','--user','operator','--group','operators','--no-systemd',
            '--assume-kafka-version','4.3.1','--skip-kraft-version-check','--verify',
            '--extra-properties','{"num.partitions":3}']
        for cluster_flags,key in ((['--cluster-id',CID],'cluster_id'),
                (['--generate-cluster-id'],'generate_cluster_id'),(['--use-disk-cluster-id'],'use_disk_cluster_id')):
            factory,owner=self.routed('--deploy','standalone',*common,*cluster_flags,'--listeners','PLAINTEXT://:19092')
            factory.assert_called_once_with(str(self.home),user='operator',group='operators',
                assume_kafka_version='4.3.1',skip_kraft_version_check=True)
            kw=owner.deploy_standalone.call_args.kwargs
            self.assertEqual(kw[key],CID if key=='cluster_id' else True)
            self.assertEqual(kw['node_id'],0)
            self.assertEqual(kw['listeners'],'PLAINTEXT://:19092')
            self.assertEqual(kw['extra_properties'],{'num.partitions':'3'})
            self.assertFalse(kw['enable_systemd'])
            self.assertEqual(kw['java_home'],'/fixture/jdk')
            owner.show_cluster_status.assert_called_once()
        _,owner=self.routed('--deploy','controller',*common,'--controller-scope','cluster',
            '--cluster-id',CID,'--join-quorum','--metadata-log-dir',self.base/'meta',
            '--controller-quorum-bootstrap-servers','controller.invalid:19093',
            '--controller-listen-host','0.0.0.0','--controller-listen-port','19093')
        kw=owner.deploy_controller.call_args.kwargs
        for key,value in dict(controller_scope='cluster',join_quorum=True,node_id=0,
                controller_listen_host='0.0.0.0',controller_listener_port=19093,
                metadata_log_dir=str(self.base/'meta'),controller_quorum_bootstrap_servers='controller.invalid:19093').items():
            self.assertEqual(kw[key],value)
        owner.verify_controller_quorum.assert_called_once()
        _,owner=self.routed('--deploy','broker',*common,'--cluster-id',CID,
            '--controller-quorum-bootstrap-servers','controller.invalid:19093',
            '--broker-listen-host','0.0.0.0','--broker-listen-port','19092')
        kw=owner.deploy_broker.call_args.kwargs
        self.assertEqual(kw['broker_listen_port'],19092)
        self.assertEqual(kw['broker_listen_host'],'0.0.0.0')
        self.assertEqual(kw['node_id'],0)

    def test_clean_force_clean_first_and_status_config_scope(self):
        for flags in (['--clean','--clean-data'],['--clean','--force']):
            _,owner=self.routed('--deploy','broker','--node-id','0','--log-dirs',self.base/'data',
                '--metadata-log-dir',self.base/'meta',*flags)
            owner.clean_deployment.assert_called_once_with('broker',node_id=0,backup_config=True,clean_data=True,
                log_dirs=str(self.base/'data'),metadata_log_dir=str(self.base/'meta'))
            owner.deploy_broker.assert_not_called()
        _,owner=self.routed('--deploy','standalone','--node-id','0','--log-dirs',self.base/'data',
            '--advertised-host','node.invalid','--generate-cluster-id','--clean-first')
        owner.clean_deployment.assert_called_once()
        owner.deploy_standalone.assert_called_once()
        _,owner=self.routed('--status','--bootstrap-controller','controller.invalid:19093',
            config=dict(bootstrap_server='broker.invalid:19092'))
        self.assertEqual(owner.show_cluster_status.call_args.kwargs['bootstrap_controller'],'controller.invalid:19093')

    def test_metrics_modes_preserve_partial_results_and_report_failed_collection(self):
        for flag in ('--metrics','--metrics-json'):
            for healthy in (True,False):
                data={key:dict(ok=healthy,count=3) for key in ('broker_connect','quorum',
                    'partition_health','topic_inventory','consumer_group_inventory','consumer_lag')}
                output=io.StringIO()
                with mock.patch.object(sys,'argv',['KafkaCli',flag,'--kafka-home',str(self.home),
                        '--bootstrap-server','broker.invalid:19092','--bootstrap-controller','ctrl.invalid:19093']), \
                        mock.patch.object(self.m,'KafkaMetricsCollector') as collector, \
                        contextlib.redirect_stdout(output):
                    collector.return_value.collect_all.return_value=data
                    with self.assertRaises(SystemExit) as result: self.m.main()
                self.assertEqual(result.exception.code,0 if healthy else 1)
                collector.assert_called_once_with(str(self.home),'broker.invalid:19092',None,
                    bootstrap_controller='ctrl.invalid:19093')
                if flag=='--metrics-json': self.assertEqual(json.loads(output.getvalue()),data)
                else: self.assertIn('Consumer Lag',output.getvalue())

    def test_initial_voters_accept_shared_cluster_id_and_refuse_conflicting_modes(self):
        voters='0@node0.invalid:19093:'+CID+',1@node1.invalid:19093:'+CID
        self.assertIsNone(self.m._validate_controller_scope_rules('cluster',CID,False,False,False,voters))
        for scope,cid,gen,disk,join in (('single',CID,False,False,False),
                ('cluster',None,False,False,False),('cluster',CID,True,False,False),
                ('cluster',CID,False,True,False),('cluster',CID,False,False,True)):
            self.assertIsNotNone(self.m._validate_controller_scope_rules(scope,cid,gen,disk,join,voters))
        _,owner=self.routed('--deploy','controller','--controller-scope','cluster','--cluster-id',CID,
            '--node-id','0','--advertised-host','node0.invalid','--initial-controllers',voters,
            '--controller-quorum-bootstrap-servers','node0.invalid:19093,node1.invalid:19093',
            '--metadata-log-dir',self.base/'meta','--log-dirs',self.base/'logs',
            '--controller-listen-host','0.0.0.0')
        self.assertEqual(owner.deploy_controller.call_args.kwargs['initial_controllers'],voters)

    def test_sasl_plain_and_tls_deploy_credentials_reach_server_properties(self):
        keystore=self.base/'server.p12';keystore.touch()
        truststore=self.base/'trust.jks';truststore.touch()
        for mode in ('--deploy-sasl-plain','--deploy-sasl-ssl'):
            with self.subTest(mode=mode):
                _,owner=self.routed('--deploy','standalone','--node-id','0','--advertised-host','node.invalid',
                    '--log-dirs',self.base/'data','--generate-cluster-id',mode,
                    '--kafka-user','fixture-user','--kafka-password','private-password',
                    '--ssl-keystore-path',keystore,'--ssl-keystore-password','store-password',
                    '--ssl-key-password','key-password','--ssl-truststore-path',truststore,
                    '--ssl-truststore-password','trust-password')
                props=owner.deploy_standalone.call_args.kwargs
                self.assertEqual(props['sasl_password'],'private-password')
                self.assertEqual(props['sasl_username'],'fixture-user')
                self.assertEqual(props['enable_sasl_plain'],mode.endswith('plain'))
                if mode.endswith('ssl'):
                    self.assertEqual(props['sasl_ssl_material'],(str(keystore),'store-password',
                        'key-password',str(truststore),'trust-password'))

    def test_batch_executes_in_order_stops_on_failure_and_refuses_invalid_plan(self):
        path=self.base/'batch.json'
        nodes=[dict(target_host='node'+str(i)+'.invalid',deploy='broker',node_id=i) for i in range(3)]
        for outcomes,expected_count in (([True,True,True],3),([True,False],2)):
            path.write_text(json.dumps(dict(kafka_home=str(self.home),nodes=nodes)))
            with mock.patch.object(sys,'argv',['KafkaCli','--batch','--config',str(path)]), \
                    mock.patch.object(self.m,'_run_remote_deploy',side_effect=outcomes) as remote:
                try: self.m.main();rc=0
                except SystemExit as error: rc=error.code
            self.assertEqual(rc,0 if expected_count==3 else 1)
            self.assertEqual([c.args[0] for c in remote.call_args_list],
                [n['target_host'] for n in nodes[:expected_count]])
            for c in remote.call_args_list: self.assertFalse(c.kwargs['exit_on_finish'])
        for invalid in ([nodes[0],{}],[nodes[0],'invalid']):
            path.write_text(json.dumps(dict(kafka_home=str(self.home),nodes=invalid)))
            with mock.patch.object(sys,'argv',['KafkaCli','--batch','--config',str(path)]), \
                    mock.patch.object(self.m,'_run_remote_deploy') as remote:
                with self.assertRaises(SystemExit) as error: self.m.main()
                self.assertNotEqual(error.exception.code,0)
                remote.assert_not_called()

    def test_config_defaults_and_explicit_cli_values_select_same_business_values(self):
        config=dict(deploy='standalone',user='config-user',group='config-group',node_id=0,
            advertised_host='node.invalid',log_dirs=str(self.base/'data'),generate_cluster_id=True)
        factory,_=self.routed(config=config)
        self.assertEqual(factory.call_args.kwargs['user'],'config-user')
        self.assertEqual(factory.call_args.kwargs['group'],'config-group')
        factory,_=self.routed('--user','cli-user','--group','cli-group',config=config)
        self.assertEqual(factory.call_args.kwargs['user'],'cli-user')
        self.assertEqual(factory.call_args.kwargs['group'],'cli-group')
        path=self.base/'admin.json';path.write_text(json.dumps(dict(partitions=4,replication_factor=3)))
        _,calls=self.cli('--topic-create','--topic','events','--config',path)
        args=calls[0]['argv']
        self.assertEqual(args[args.index('--partitions')+1],'4')
        self.assertEqual(args[args.index('--replication-factor')+1],'3')
        _,calls=self.cli('--topic-create','--topic','events','--config',path,'--partitions=2')
        self.assertEqual(calls[0]['argv'][calls[0]['argv'].index('--partitions')+1],'2')


if __name__=='__main__':
    unittest.main()
