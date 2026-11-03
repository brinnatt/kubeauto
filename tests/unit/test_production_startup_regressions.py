"""Regressions for the current dev prepare and Nacos startup paths."""

import os
import json
import base64
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]


class ProductionStartupRegressions(unittest.TestCase):
    def test_auth_fixture_generates_private_keys_and_stops_on_entropy_failure(self):
        source = (ROOT/'tests/helpers/delivery-gap-retest.sh').read_text()
        function = 'stage_nacos_auth_secret() (' + source.split('stage_nacos_auth_secret() (',1)[1].split('\n)',1)[0]+'\n)\n'
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                kubectl = base/'kubectl'
                kubectl.write_text('''#!/usr/bin/env python3
import base64,os,pathlib,sys
a=sys.argv[1:]; root=pathlib.Path(os.environ['AUTH_TEST_ROOT'])
if 'create' in a:
 for arg in a:
  if not arg.startswith('--from-file='):continue
  name,path=arg.removeprefix('--from-file=').split('=',1); p=pathlib.Path(path)
  data=p.read_bytes(); assert b'\\n' not in data and p.stat().st_mode & 0o077 == 0
  if name=='token':assert len(base64.b64decode(data,validate=True))==48
  else:assert len(data)==(32 if name=='identity-key' else 64)
 print('apiVersion: v1\\nkind: Secret\\nmetadata: {name: nacos-auth}')
else:
 assert 'kind: Secret' in sys.stdin.read()
 (root/'applied').touch()
''')
                kubectl.chmod(0o755)
                if failure:
                    openssl = base/'openssl'; openssl.write_text('#!/bin/sh\nexit 23\n');openssl.chmod(0o755)
                body = function.replace('/var/tmp/kubeauto-nacos-auth.', str(base/'auth.'))
                result = subprocess.run(['bash','-c',body+'if stage_nacos_auth_secret; then exit 0; else exit 17; fi'],
                    capture_output=True,text=True,timeout=10,
                    env={**os.environ,'PATH':str(base)+':'+os.environ['PATH'],'AUTH_TEST_ROOT':str(base)})
                self.assertEqual(result.returncode,17 if failure else 0,result.stdout+result.stderr)
                self.assertEqual((base/'applied').exists(),not failure)
                self.assertEqual(list(base.glob('auth.*')),[])

    def test_auth_secret_preflight_rejects_missing_keys_before_rendering(self):
        executable = ROOT/'.venv/bin/ansible-playbook'
        if not executable.is_file():
            self.skipTest('Ansible is required for the local role contract')
        tasks = yaml.safe_load((ROOT/'roles/cluster-addon/tasks/nacos.yml').read_text())[0]['block'][:4]
        for missing in ('', 'absent', 'token', 'identity-key', 'identity-value'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'yml').mkdir(); (base/'bin').mkdir()
                executable_stub = base/'bin/kubectl'
                executable_stub.write_text('''#!/usr/bin/env python3
import base64,json,os,sys
from pathlib import Path
Path(os.environ['AUTH_TEST_ROOT'],'called').touch()
if os.environ['AUTH_TEST_MISSING']=='absent':sys.exit(1)
keys={k:base64.b64encode(('private-'+k).encode()).decode() for k in ('token','identity-key','identity-value')}
keys.pop(os.environ['AUTH_TEST_MISSING'],None)
print(json.dumps({'data':keys}))
''')
                executable_stub.chmod(0o755)
                values = yaml.safe_load((ROOT/'conf/config.yml').read_text())
                values.update(cluster_dir=str(base), CLUSTER_DNS_DOMAIN='cluster.local',
                              nacos_storage_class='local-path', nacos_auth_secret='customer-auth')
                selected = json.loads(json.dumps(tasks))
                selected[-1]['template']['src'] = str(ROOT/'roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2')
                playbook = base/'playbook.yml'
                playbook.write_text(yaml.safe_dump([{'hosts':'localhost','gather_facts':False,
                                                    'vars':values,'tasks':selected}]))
                result = subprocess.run([str(executable),'-i','localhost,','-c','local',str(playbook)],
                    capture_output=True,text=True,timeout=30,
                    env={**os.environ,'PATH':str(base/'bin')+':'+os.environ['PATH'],
                         'AUTH_TEST_ROOT':str(base),'AUTH_TEST_MISSING':missing})
                self.assertEqual(result.returncode==0, not missing, result.stdout+result.stderr)
                manifest = base/'yml/nacos-sts.yaml'
                self.assertEqual(manifest.exists(), not missing)
                if not missing:
                    self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
                for key in ('token','identity-key','identity-value'):
                    self.assertNotIn(base64.b64encode(('private-'+key).encode()).decode(),result.stdout+result.stderr)

    def test_completion_tasks_create_missing_bashrc_and_preserve_existing_content(self):
        executable = ROOT / '.venv/bin/ansible-playbook'
        if not executable.is_file():
            executable = shutil.which('ansible-playbook')
        if not executable:
            self.skipTest('Ansible is required for the local file-module contract')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bashrc = base / '.bashrc'
            tasks = []
            for role in ('containerd', 'kube-node'):
                for task in yaml.safe_load((ROOT / f'roles/{role}/tasks/main.yml').read_text()):
                    if task.get('name', '').endswith('bash completion'):
                        tasks.append({**task, 'lineinfile': {**task['lineinfile'], 'dest': str(bashrc)}})
            self.assertEqual(len(tasks), 3)
            playbook = base / 'completion.yml'
            playbook.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
            for existing in (False, True):
                if existing:
                    bashrc.write_text('export CUSTOMER_SETTING=retained\n' + bashrc.read_text())
                    bashrc.chmod(0o600)
                result = subprocess.run(
                    [str(executable), '-i', 'localhost,', '-c', 'local',
                     '-e', 'ansible_python_interpreter=' + sys.executable, str(playbook)],
                    capture_output=True, text=True, timeout=45,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                content = bashrc.read_text()
                for command in ('crictl', 'nerdctl', 'kubectl'):
                    self.assertEqual(content.count(f'source <({command} completion bash)'), 1)
                if existing:
                    self.assertIn('export CUSTOMER_SETTING=retained', content)
                    self.assertEqual(bashrc.stat().st_mode & 0o777, 0o600)

    def render_nacos(self, **overrides):
        values = yaml.safe_load((ROOT / "conf/config.yml").read_text())
        values.update(CLUSTER_DNS_DOMAIN="cluster.local", nacos_storage_class="local-path")
        values.update(overrides)
        source = (ROOT / "roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2").read_text()
        environment = Environment(undefined=StrictUndefined)
        environment.filters['to_json'] = json.dumps
        docs = list(yaml.safe_load_all(environment.from_string(source).render(values)))
        return next(doc for doc in docs if doc["kind"] == "StatefulSet")

    def test_credentials_use_secrets_and_preserve_special_password_characters(self):
        values = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        password = 'quote" newline\n\\backslash & value'
        values.update(CLUSTER_DNS_DOMAIN='cluster.local', nacos_storage_class='local-path',
                      nacos_auth_secret='customer-auth', nacos_mysql_password=password)
        environment = Environment(undefined=StrictUndefined)
        environment.filters['to_json'] = json.dumps
        source = (ROOT/'roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2').read_text()
        documents = list(yaml.safe_load_all(environment.from_string(source).render(values)))
        config = next(d for d in documents if d['kind']=='ConfigMap')
        self.assertNotIn('mysql.password', config['data'])
        secret = next(d for d in documents if d['kind']=='Secret')
        self.assertEqual(secret['stringData']['mysql.password'], password)
        pod = next(d for d in documents if d['kind']=='StatefulSet')['spec']['template']['spec']
        env = {e['name']:e for e in pod['containers'][0]['env']}
        for name,key in [('NACOS_AUTH_TOKEN','token'), ('NACOS_AUTH_IDENTITY_KEY','identity-key'),
                         ('NACOS_AUTH_IDENTITY_VALUE','identity-value')]:
            self.assertEqual(env[name]['valueFrom']['secretKeyRef'], {'name':'customer-auth','key':key})
            self.assertNotIn('value',env[name])
        self.assertEqual(env['MYSQL_SERVICE_PASSWORD']['valueFrom']['secretKeyRef'],
                         {'name':'nacos-mysql-client','key':'mysql.password'})
        tasks = yaml.safe_load((ROOT/'roles/cluster-addon/tasks/nacos.yml').read_text())[0]['block']
        template = next(t for t in tasks if 'template' in t)
        self.assertEqual(template['template']['mode'],'0600')
        self.assertTrue(template['no_log'])
        self.assertFalse(template['diff'])

    def test_firewalls_are_stopped_not_uninstalled_on_every_distribution(self):
        for name in ("redhat", "debian", "suse"):
            source = (ROOT / f"roles/prepare/tasks/{name}.yml").read_text()
            self.assertNotRegex(source, r"(?:dnf|yum|apt-get|zypper)\s+(?:remove|purge)")
            self.assertIn("ipset", source)
            self.assertIn("nftables", source)
            self.assertIn("iptables", source)
        tasks = yaml.safe_load((ROOT / "roles/prepare/tasks/firewall.yml").read_text())
        self.assertIn("service_facts", tasks[0])
        stop, mask = tasks[1:3]
        self.assertEqual(stop['systemd'], {'name': 'firewalld.service', 'state': 'stopped', 'enabled': False})
        self.assertEqual(mask['systemd'], {'name': 'firewalld.service', 'masked': True})
        for task in tasks[1:]:
            options = task["systemd"]
            if "state" in options:
                self.assertEqual(options["state"], "stopped")
                self.assertIs(options["enabled"], False)
            self.assertNotIn("failed_when", task)
            self.assertNotIn("ignore_errors", task)
            for services, expected in (({}, False), ({options["name"]: {"status": "enabled"}}, True),
                                       ({options["name"]: {"status": "not-found"}}, False)):
                selected = all(Environment().compile_expression(condition)(ansible_facts={"services": services})
                               for condition in task["when"])
                self.assertEqual(selected, expected)

    def test_prepare_marker_cannot_bypass_firewall_and_executable_checks(self):
        main = yaml.safe_load((ROOT / "roles/prepare/tasks/main.yml").read_text())
        for name in ("firewall.yml", "network-tools.yml"):
            task = next(t for t in main if t.get("import_tasks") == name)
            self.assertEqual(task["when"], "p.stat.isreg | default(false)")
        common = yaml.safe_load((ROOT / "roles/prepare/tasks/common.yml").read_text())
        self.assertEqual([t["import_tasks"] for t in common[:2]], ["firewall.yml", "network-tools.yml"])
        checks = yaml.safe_load((ROOT / "roles/prepare/tasks/network-tools.yml").read_text())
        self.assertEqual(checks[0]["command"], "{{ item }} --version")
        self.assertEqual(checks[0]["loop"], ["ipset", "nft", "iptables", "iptables-save",
                                            "iptables-restore", "ip6tables", "ip6tables-save", "ip6tables-restore"])
        self.assertNotIn("failed_when", checks[0])
        self.assertNotIn("ignore_errors", checks[0])

    def test_peer_finder_receives_real_pod_ip(self):
        pod = self.render_nacos()["spec"]["template"]["spec"]
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        self.assertEqual(env["POD_IP"]["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")

    def test_readiness_is_application_health_and_startup_is_bounded(self):
        container = self.render_nacos()["spec"]["template"]["spec"]["containers"][0]
        for probe in ("startupProbe", "readinessProbe"):
            self.assertEqual(container[probe]["httpGet"],
                             {"path": "/nacos/v1/console/health/readiness", "port": "client-port"})
            self.assertGreater(container[probe]["timeoutSeconds"], 0)
        self.assertLessEqual(container["startupProbe"]["failureThreshold"] * container["startupProbe"]["periodSeconds"], 900)
        self.assertNotIn("livenessProbe", container)

    def test_default_jdbc_verifies_tls_without_dynamic_rsa_key_retrieval(self):
        pod = self.render_nacos()["spec"]["template"]["spec"]
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        jdbc = env["MYSQL_SERVICE_DB_PARAM"]["value"]
        self.assertIn("sslMode=VERIFY_IDENTITY", jdbc)
        self.assertNotIn("useSSL=false", jdbc)
        self.assertNotIn("allowPublicKeyRetrieval=true", jdbc)

    def test_private_ca_is_imported_and_mounted_without_a_private_key(self):
        pod = self.render_nacos(nacos_mysql_ca_secret="mysql-ca")["spec"]["template"]["spec"]
        init = next(c for c in pod["initContainers"] if c["name"] == "mysql-truststore")
        self.assertIn("keytool -importcert", init["args"][0])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["mysql-ca"]["secret"]["secretName"], "mysql-ca")
        self.assertEqual(volumes["mysql-ca"]["secret"]["items"], [{"key": "ca.crt", "path": "ca.crt"}])
        main = pod["containers"][0]
        jdbc = next(e["value"] for e in main["env"] if e["name"] == "MYSQL_SERVICE_DB_PARAM")
        self.assertIn("trustCertificateKeyStoreUrl=file:/home/nacos/mysql-tls/truststore.p12", jdbc)
        self.assertTrue(next(m for m in main["volumeMounts"] if m["name"] == "mysql-truststore")["readOnly"])

    def test_explicit_jdbc_strategy_remains_configurable(self):
        value = "sslMode=DISABLED&serverRSAPublicKeyFile=/keys/mysql.pem"
        pod = self.render_nacos(nacos_mysql_db_param=value)["spec"]["template"]["spec"]
        env = {e["name"]: e for e in pod["containers"][0]["env"]}
        self.assertEqual(env["MYSQL_SERVICE_DB_PARAM"]["value"], value)
        self.assertFalse(any(c["name"] == "mysql-truststore" for c in pod["initContainers"]))

    def test_gate_rejects_unhealthy_instances_and_false_business_success(self):
        source = (ROOT / "tests/helpers/delivery-gap-retest.sh").read_text()

        def function(name):
            body = source.split(name + "(){", 1)[1].split("\n}", 1)[0]
            return name + "(){" + body + "\n}\n"

        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / "kubectl"
            kubectl.write_text('''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if "get" in a:
    if "statefulset" in a: print("mysql")
    elif "pods" in a: print("nacos-0 1/1 Running\\nnacos-1 1/1 Running\\nnacos-2 1/1 Running")
    elif "nacos-0" in a: print("10.70.1.4")
    else: print("mysql-fixture")
elif "exec" in a:
    pod = a[a.index("exec")+1]
    if "mysql" in a: print("2"); sys.exit(0)
    url = next((s for s in a if s.startswith("http://")), "")
    method = a[a.index("-X")+1] if "-X" in a else "GET"
    if url.endswith("/readiness"):
        print("STARTING" if os.getenv("FAIL_HEALTH") == pod else "OK")
    elif url.endswith("/admin"): print(json.dumps({"username":"nacos"}))
    elif url.endswith("/login"): print(json.dumps({"accessToken":"fixture-token-private"})+"\\n200")
    elif url.endswith("/configs"):
        if method == "GET": print("wrong-data" if os.getenv("FAIL_CONFIG") == pod else "nacos-dev-business-ok")
        else: print("false" if os.getenv("FAIL_PUBLISH") else "true")
    elif url.endswith("/instance/list"):
        print(json.dumps({"hosts": [] if os.getenv("FAIL_NAMING") == pod else [{"ip":"10.70.1.4", "port":8848}]}))
    elif url.endswith("/instance"): print("ok")
''')
            kubectl.chmod(0o755)
            sleeper = Path(tmp) / "sleep"
            sleeper.write_text("#!/bin/sh\nexit 0\n")
            sleeper.chmod(0o755)
            env = {**os.environ, "PATH": tmp + ":" + os.environ["PATH"]}
            for variable in (None, "FAIL_HEALTH", "FAIL_CONFIG", "FAIL_NAMING", "FAIL_PUBLISH"):
                with self.subTest(failure=variable):
                    name = "wait_nacos_external_mysql" if variable == "FAIL_HEALTH" else "verify_nacos_business"
                    argument = "1" if name == "wait_nacos_external_mysql" else "initialize"
                    result = subprocess.run(["bash", "-o", "pipefail", "-c", function(name) + name + " " + argument],
                                            env={**env, **({variable: "nacos-1"} if variable else {})},
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, variable is None, result.stdout + result.stderr)
                    self.assertNotIn("fixture-token-private", result.stdout + result.stderr)
                    if variable is None:
                        self.assertEqual(result.stdout.count("NACOS_INSTANCE_BUSINESS_PASS"), 3)

    def test_tls_fixture_stops_and_cleans_on_certificate_failure_in_conditional(self):
        source = (ROOT / "tests/helpers/delivery-gap-retest.sh").read_text()
        body = "stage_nacos_mysql_tls() (" + source.split("stage_nacos_mysql_tls() (", 1)[1].split("\n)", 1)[0] + "\n)\n"
        with tempfile.TemporaryDirectory() as tmp:
            for name, script in {
                "mktemp": '#!/bin/sh\nmkdir "$TEST_DIR/tls"\nprintf "%s\\n" "$TEST_DIR/tls"\n',
                "openssl": "#!/bin/sh\nexit 19\n",
                "kubectl": '#!/bin/sh\ntouch "$TEST_DIR/unexpected-kubectl"\n',
            }.items():
                path = Path(tmp) / name
                path.write_text(script)
                path.chmod(0o755)
            result = subprocess.run(["bash", "-c", body + "if stage_nacos_mysql_tls; then exit 0; else exit 17; fi"],
                                    env={**os.environ, "TEST_DIR": tmp, "PATH": tmp + ":" + os.environ["PATH"]},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertFalse((Path(tmp) / "unexpected-kubectl").exists())
            self.assertFalse((Path(tmp) / "tls").exists())


if __name__ == "__main__":
    unittest.main()
