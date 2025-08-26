"""
Main cluster operations for kubeauto
"""
import ipaddress
import re
import ansible_runner
import yaml
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional
from common.utils import run_command, validate_ip, confirm_action, AnsiColor
from common.exceptions import (
    ClusterExistsError, ClusterNotFoundError,
    InvalidIPError, NodeExistsError, NodeNotFoundError, ClusterNewError,
)
from common.logger import setup_logger
from common.constants import KubeConstant

logger = setup_logger(__name__)


class ClusterManager:
    def __init__(self):
        self.kube_constant = KubeConstant()
        self.base_path = Path(self.kube_constant.BASE_PATH)
        self.kube_bin_dir = Path(self.kube_constant.KUBE_BIN_DIR)
        self.extra_bin_dir = Path(self.kube_constant.EXTRA_BIN_DIR)
        self.clusters_dir = self.base_path / "clusters"
        self.playbooks_dir = self.base_path / "playbooks"
        self.roles_dir = self.base_path / "roles"

    def list_clusters(self) -> List[str]:
        """List all managed clusters"""
        if not self.clusters_dir.exists():
            raise ClusterNotFoundError("Cluster directory not found, run 'new' first")

        if not (Path.home() / ".kube/config").exists():
            raise ClusterNotFoundError("kubeconfig not found, run 'setup' first")

        clusters = []
        for cluster_dir in self.clusters_dir.iterdir():
            if cluster_dir.is_dir() and (cluster_dir / "kubectl.kubeconfig").exists():
                clusters.append(cluster_dir.name)

        return clusters

    def get_current_cluster(self) -> Optional[str]:
        """Get current cluster from kubeconfig"""
        current_config = Path.home() / ".kube/config"
        if not current_config.exists():
            return None

        try:
            # Get MD5 of current config (excluding server field)
            md5_cmd = ["sed", "/server/d", str(current_config), "|", "md5sum"]
            current_md5 = run_command(md5_cmd, shell=True).stdout.split()[0]

            # Compare with cluster configs
            for cluster in self.list_clusters():
                cluster_config = self.clusters_dir / cluster / "kubectl.kubeconfig"
                cluster_md5_cmd = ["sed", "/server/d", str(cluster_config), "|", "md5sum"]
                cluster_md5 = run_command(cluster_md5_cmd, shell=True).stdout.split()[0]

                if cluster_md5 == current_md5:
                    return cluster

            return None
        except Exception as e:
            logger.error(f"Error getting current cluster: {e}")
            return None

    def new_cluster(self, name: str) -> None:
        """Create a new cluster configuration"""
        cluster_dir = self.clusters_dir / name
        if cluster_dir.exists():
            raise ClusterExistsError(f"Cluster {name} already exists")

        logger.debug(f"Creating cluster directory: {cluster_dir}")
        cluster_dir.mkdir(parents=True, exist_ok=True)

        # Copy example files
        example_hosts = self.base_path / "example/hosts.multi-node"
        example_config = self.base_path / "example/config.yml"
        cluster_hosts = cluster_dir / "hosts"
        cluster_config = cluster_dir / "config.yml"
        try:
            cluster_hosts.write_text(example_hosts.read_text())
            cluster_config.write_text(example_config.read_text())

            # Replace placeholders
            hosts_content = cluster_hosts.read_text().replace("_cluster_name_", name)
            config_content = (
                cluster_config.read_text().replace("__k8s_ver__", self.kube_constant.v_k8s_bin.lstrip("v"))
                .replace("__flannel__", self.kube_constant.v_flannel)
                .replace("__calico__", self.kube_constant.v_calico)
                .replace("__cilium__", self.kube_constant.v_cilium)
                .replace("__kube_ovn__", self.kube_constant.v_kubeovn)
                .replace("__kube_router__", self.kube_constant.v_kuberouter)
                .replace("__coredns__", self.kube_constant.v_coredns)
                .replace("__pause__", self.kube_constant.v_pause)
                .replace("__dns_node_cache__", self.kube_constant.v_dnsnodecache)
                .replace("__dashboard__", self.kube_constant.v_dashboard)
                .replace("__local_path_provisioner__", self.kube_constant.v_localpathprovisioner)
                .replace("__nfs_provisioner__", self.kube_constant.v_nfsprovisioner)
                .replace("__prom_chart__", self.kube_constant.v_promchart)
                .replace("__kubeapps_chart__", self.kube_constant.v_kubeapps)
                .replace("__harbor__", self.kube_constant.v_harbor)
                .replace("__metrics__", self.kube_constant.v_metricsserver)
            )

            cluster_hosts.write_text(hosts_content)
            cluster_config.write_text(config_content)
        except Exception as e:
            raise ClusterNewError(f"Error creating cluster hosts or config: {e}")

        logger.info(f"-> Cluster {name} created. Next steps:", extra={"to_stdout": True})
        logger.info(f"1. Configure {cluster_hosts}", extra={"to_stdout": True})
        logger.info(f"2. Configure {cluster_config}", extra={"to_stdout": True})

    def setup_cluster(self, name: str, step: str, extra_args: Optional[list[str]] = None) -> None:
        """
        Set up a cluster with specific step

        name: Cluster name
        step: Setup step (01-07, 10, 11, 90 or step name)
        extra_args: Additional arguments to pass to ansible_runner

        ansible_runner methods:
        method one: on host
            ansible_runner.run(
                private_data_dir="", # if on host, ansible_runner may not recommend to specify this directory as multi ansible-runners generates envs affecting each other
                playbook=str(self.playbooks_dir / playbook),
                inventory=str(self.clusters_dir / name / "hosts"),
                extravars=self._yaml_to_dict(self.clusters_dir / name / 'config.yml'),
                roles_path=str(self.roles_dir),
                cmdline=" ".join(extra_args if extra_args else [])
            )

        method two: bubblewrap(rpm)
            ansible_runner.run(
                private_data_dir="/root/runner-test", # /root/runner-test/{artifacts,inventory,project}
                playbook="site.yml",
                process_isolation=True,
                process_isolation_executable="bwrap",
                directory_isolation_base_path="/tmp", # runner will create runner_di_* directory under /tmp, you don't have to specify it. runner will handle this by default.
                process_isolation_show_paths=["/root/.ssh", "/tmp"] # if base_path specified, you must specify show_paths which will be bound to bwrap sandbox, or you got bwrap: Can't chdir to /tmp/runner_di_jg3co5vt: No such file or directory
            )

        method three: docker
            ansible_runner.run(
                private_data_dir="/root/runner-test", # /root/runner-test/{artifacts,inventory,project}
                playbook="site.yml",
                process_isolation=True,
                process_isolation_executable="docker",  # the same as podman(default)
                container_image="quay.io/ansible/ansible-runner:latest",
                container_volume_mounts=["/root/.ssh:/root/.ssh:ro"],
                container_options=["--rm", "--network", "none"]
            )
        """
        self._validate_cluster(name)

        playbook_map = {
            "01": "01.prepare.yml",
            "prepare": "01.prepare.yml",
            "02": "02.etcd.yml",
            "etcd": "02.etcd.yml",
            "03": "03.runtime.yml",
            "container-runtime": "03.runtime.yml",
            "04": "04.kube-master.yml",
            "kube-master": "04.kube-master.yml",
            "05": "05.kube-node.yml",
            "kube-node": "05.kube-node.yml",
            "06": "06.network.yml",
            "network": "06.network.yml",
            "07": "07.cluster-addon.yml",
            "cluster-addon": "07.cluster-addon.yml",
            "90": "90.setup.yml",
            "all": "90.setup.yml",
            "10": "10.ex-lb.yml",
            "ex-lb": "10.ex-lb.yml",
            "11": "11.harbor.yml",
            "harbor": "11.harbor.yml"
        }

        playbook = playbook_map.get(step, "dummy.yml")
        if playbook == "dummy.yml":
            logger.error(f"Invalid setup step: {step}", extra={"to_stdout": True})
            return

        logger.info(f"Setup k8s cluster with playbook {playbook}", extra={"to_stdout": True})
        self._show_component_versions(name)
        if not confirm_action(f"cluster:{name} setup step:{step} begins"):
            return

        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / playbook),
                inventory=str(self.clusters_dir / name / "hosts"),
                extravars=self._yaml_to_dict(self.clusters_dir / name / 'config.yml'),
                roles_path=str(self.roles_dir),
                cmdline=" ".join(extra_args if extra_args else [])
            )
            if result.rc != 0:
                logger.error(f"Failed to set up the k8s cluster with playbook '{playbook}'. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Setup k8s cluster with playbook {playbook} successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Setting up k8s cluster with playbook {playbook} failed", extra={"to_stdout": True})
            raise e

    def _yaml_to_dict(self, path: Path) -> dict:
        """
        transform YAML file to Python dict for ansible_runner usage
        :param path: Path type -> YAML file
        :return: dict
        """
        if not path.exists():
            raise FileNotFoundError(f"YAML file does not exist: {path}")

        with path.open("r", encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise ValueError(f"Failed to resolve YAML file: {path}, error: {e}")

        if data is None:
            return {}
        if not isinstance(data, dict):
            raise TypeError(f"YAML root must be a dictionary for yaml_to_dict usage, got {type(data).__name__}")

        return data

    def cluster_command(self, name: str, command: str) -> None:
        """Execute cluster-wide command (start, stop, upgrade, backup, restore, destroy)"""
        self._validate_cluster(name)

        playbook_map = {
            "start": "91.start.yml",
            "stop": "92.stop.yml",
            "upgrade": "93.upgrade.yml",
            "backup": "94.backup.yml",
            "restore": "95.restore.yml",
            "destroy": "99.clean.yml"
        }

        playbook = playbook_map.get(command)
        if not playbook:
            logger.error(f"Invalid command: {command}", extra={"to_stdout": True})
            return

        logger.info(f"cluster:{name} {command} with playbook {playbook}", extra={"to_stdout": True})
        self._show_component_versions(name)
        if not confirm_action(f"cluster:{name} {command} begins"):
            return

        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / playbook),
                inventory=str(self.clusters_dir / name / "hosts"),
                extravars=self._yaml_to_dict(self.clusters_dir / name / 'config.yml'),
                roles_path=str(self.roles_dir)
            )
            if result.rc != 0:
                logger.error(f"Failed to {command} cluster {name} with playbook '{playbook}'. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Succeed to {command} cluster {name} with playbook {playbook}!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Failed to {command} cluster {name} with playbook '{playbook}'", extra={"to_stdout": True})
            raise e

    def checkout_cluster(self, name: str) -> None:
        """Switch to a cluster's kubeconfig"""
        self._validate_cluster(name)

        kubeconfig = self.clusters_dir / name / "kubectl.kubeconfig"
        if not kubeconfig.exists():
            raise ClusterNotFoundError(f"Invalid kubeconfig, run 'setup {name}' first")

        dest_config = Path.home() / ".kube/config"
        dest_config.parent.mkdir(exist_ok=True)

        run_command(["cp", "-f", str(kubeconfig), str(dest_config)])
        logger.info(f"Set default kubeconfig: cluster {name} (current)", extra={"to_stdout": True})

    def start_aio_cluster(self) -> None:
        """Start an all-in-one cluster with default settings"""
        from common.utils import get_host_ip, ssh_localhost

        try:
            logger.info("Start initializing allinone cluster environment...", extra={"to_stdout": True})

            # ssh myself based on ssh key
            host_ip = get_host_ip()
            ssh_localhost()

            # Create the aio cluster
            self.new_cluster("aio")

            # Copy all-in-one example host file with actual IP and cluster name
            aio_example_hosts = self.base_path / "example/hosts.allinone"
            aio_hosts = self.clusters_dir / "aio" / "hosts"
            aio_hosts.write_text(aio_example_hosts.read_text().replace("192.168.1.1", host_ip)
                                 .replace("_cluster_name_", "aio"))

            logger.info("Allinone cluster environment has been initialized successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Allinone cluster environment failed to be initialized!", extra={"to_stdout": True})
            raise e

        try:
            # Setup cluster
            logger.info("Start creating allinone cluster...", extra={"to_stdout": True})
            self.setup_cluster("aio", "all")
            logger.info("Allinone cluster has been established successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Allinone cluster failed to be created!", extra={"to_stdout": True})
            raise e

    def add_node(self, cluster: str, ip: str, role: str, extra_info: str = "") -> None:
        """Add a node to the cluster"""
        self._validate_cluster(cluster)
        self._validate_ip(ip)

        hosts_file = self.clusters_dir / cluster / "hosts"
        if not hosts_file.exists():
            raise ClusterNotFoundError(f"Hosts file not found for cluster {cluster}")

        # Check if node already exists
        self._check_node_exists(hosts_file, ip, role)

        # Add node to hosts file
        node_line = f"{ip} {extra_info}".strip()
        self._add_to_hosts_section(hosts_file, role, node_line)

        # Run appropriate playbook
        playbook = {
            "etcd": "21.addetcd.yml",
            "master": "23.addmaster.yml",
            "node": "22.addnode.yml"
        }.get(role)

        if not playbook:
            raise ValueError(f"Invalid role: {role}")

        logger.info(f"Add {role} node {ip} to cluster {cluster}", extra={"to_stdout": True})

        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / 'config.yml')
        extra_vars["NODE_TO_ADD"] = ip
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / playbook),
                inventory=str(self.clusters_dir / cluster / "hosts"),
                extravars=extra_vars,
                roles_path=str(self.roles_dir)
            )
            if result.rc != 0:
                logger.error(f"Failed to add {role} node {ip} to cluster {cluster}. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Add {role} node {ip} to cluster {cluster} successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Failed to add {role} node {ip} to cluster {cluster}", extra={"to_stdout": True})
            raise e

        # After adding a new node, we still have to notify related services
        if role == "etcd":
            self._notify_etcd_apiserver(cluster)
        elif role == "master":
            self._restart_load_balancers(cluster)
        elif role == "node":
            pass

    def remove_node(self, cluster: str, ip: str, role: str) -> None:
        """Remove a node from the cluster"""
        self._validate_cluster(cluster)
        self._validate_ip(ip)

        hosts_file = self.clusters_dir / cluster / "hosts"
        if not hosts_file.exists():
            raise ClusterNotFoundError(f"Hosts file not found for cluster {cluster}")

        # Check if node exists
        self._check_node_not_exists(hosts_file, ip, role)

        # Run appropriate playbook
        playbook = {
            "etcd": "31.deletcd.yml",
            "master": "33.delmaster.yml",
            "node": "32.delnode.yml"
        }.get(role)

        if not playbook:
            raise ValueError(f"Invalid role: {role}")

        logger.info(f"Remove {role} node {ip} from cluster {cluster}", extra={"to_stdout": True})
        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / 'config.yml')
        extra_vars["NODE_TO_DEL"] = ip
        extra_vars["CLUSTER"] = cluster
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / playbook),
                inventory=str(hosts_file),
                extravars=extra_vars,
                roles_path=str(self.roles_dir)
            )
            if result.rc != 0:
                logger.error(f"Failed to remove {role} node {ip} from cluster {cluster}. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Remove {role} node {ip} from cluster {cluster} successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Failed to remove {role} node {ip} from cluster {cluster}", extra={"to_stdout": True})
            raise e

        # Remove node from hosts file
        self._remove_from_hosts_section(hosts_file, role, ip)

        # After removing a node, we still have to notify related services
        if role == "etcd":
            self._notify_etcd_apiserver(cluster)
        elif role == "master":
            self._reconfigure_kubeconfig(cluster)
            self._restart_load_balancers(cluster)
            self._kubectl_del_node(cluster, ip)
        elif role == "node":
            self._kubectl_del_node(cluster, ip)

    def renew_ca_certs(self, cluster: str) -> None:
        """Force renew CA certificates and all other certs in the cluster"""
        self._validate_cluster(cluster)

        logger.warning("WARNING: This will recreate CA certs and all other certs in the cluster",
                       extra={"to_stdout": True})
        logger.warning("Only use this if the admin.conf has been compromised", extra={"to_stdout": True})

        if not confirm_action(f"Renew all certs in cluster {cluster}"):
            return

        logger.info(f"Renew all certs in cluster {cluster}", extra={"to_stdout": True})
        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / 'config.yml')
        extra_vars["CHANGE_CA"] = "true"
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / "96.update-certs.yml"),
                inventory=str(self.clusters_dir / cluster / "hosts"),
                extravars=extra_vars,
                roles_path=str(self.roles_dir),
                cmdline="-t force_change_certs"
            )
            if result.rc != 0:
                logger.error(f"Failed to renew all certs in cluster {cluster}. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Renew all certs in cluster {cluster} successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Failed to renew all certs in cluster {cluster}", extra={"to_stdout": True})
            raise e

    def kubeconfig_admin(self, cluster: str, action: str,
                         user_name: str = None, user_type: str = "admin",
                         expiry: str = "4800h", show_all=False, expired_only=False) -> None:
        self._validate_cluster(cluster)
        kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"

        if action == "add":
            self._add_kcfg(cluster, user_name, user_type, expiry)
        elif action == "delete":
            self._del_kcfg(cluster, user_name, kubeconfig)
        elif action == "list":
            self._list_kcfg(cluster, kubeconfig, show_all, expired_only)
        else:
            raise ValueError(f"Unknown action: {action}")

    def _add_kcfg(self, cluster, user_name, user_type, expiry):
        if not user_name:
            user_name = f"user-{datetime.now().strftime('%Y%m%d%H%M')}"
        logger.info(f"Add kcfg in cluster:{cluster} with user:{user_name}", extra={"to_stdout": True})

        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / 'config.yml')
        extra_vars["CUSTOM_EXPIRY"] = expiry
        extra_vars["USER_TYPE"] = user_type
        extra_vars["USER_NAME"] = user_name
        extra_vars["ADD_KCFG"] = "true"
        try:
            result = ansible_runner.run(
                playbook=str(self.base_path / "roles/deploy/deploy.yml"),
                inventory=str(self.clusters_dir / cluster / "hosts"),
                extravars=extra_vars,
                roles_path=str(self.roles_dir),
                cmdline="-t add-kcfg"
            )
            if result.rc != 0:
                logger.error(f"Failed to add kcfg in cluster:{cluster} with user:{user_name}. Exit code: {result.rc}",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info(f"Add kcfg in cluster:{cluster} with user:{user_name} successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error(f"Failed to add kcfg in cluster:{cluster} with user:{user_name}", extra={"to_stdout": True})
            raise e

    def _del_kcfg(self, cluster, user_name, kubeconfig):
        if not user_name:
            raise ValueError("User name is required for delete action")
        logger.info(f"Del kcfg in cluster:{cluster} with user:{user_name}", extra={"to_stdout": True})

        crb_cmd = [
            str(self.kube_bin_dir / "kubectl"),
            "--kubeconfig", str(kubeconfig),
            "get", "clusterrolebindings",
            f"-ojsonpath={{.items[?(@.subjects[0].name == '{user_name}')].metadata.name}}"
        ]
        crb = run_command(crb_cmd, shell=False).stdout.strip()
        if crb:
            delete_cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "delete", "clusterrolebindings", crb
            ]
            run_command(delete_cmd, capture_output=False)

        cert_pattern = str(self.clusters_dir / cluster / "ssl/users" / f"{user_name}*")
        run_command(f"rm -f {cert_pattern}", shell=True)

        # delete CRB YAML
        crb_pattern = str(self.clusters_dir / cluster / "ssl/users" / f"crb-{user_name}*")
        run_command(f"rm -f {crb_pattern}", shell=True)
        logger.info(f"Deleting kcfg in cluster:{cluster} with user:{user_name} has been finished successfully",
                    extra={"to_stdout": True})

    def _list_kcfg(self, cluster, kubeconfig, show_all=False, expired_only=False):
        def get_users(role_name=None):
            if role_name:
                jsonpath = f"{{.items[?(@.roleRef.name == \"{role_name}\")].subjects[*].name}}"
            else:
                jsonpath = "{.items[*].subjects[*].name}"
            cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "get", "clusterrolebindings",
                f"-ojsonpath='{jsonpath}'"
            ]
            return run_command(cmd, shell=True).stdout.strip("'").split()

        logger.info(f"List kcfg in cluster:{cluster}", extra={"to_stdout": True})

        admins = set(get_users("cluster-admin"))
        views = set(get_users("view"))
        bound_users = set(get_users())

        if show_all:
            cert_users = {p.stem for p in (self.clusters_dir / cluster / "ssl/users").glob("*.pem") if
                          not p.name.endswith("-key.pem")}
            all_users = sorted(cert_users)
        else:
            all_users = sorted(bound_users)

        header_fmt = f"{'USER':<30}{'TYPE':<18}{'EXPIRY(+8h if in Asia/Shanghai)':<30}{'DAYS_LEFT'}"
        print("\n" + header_fmt)
        print("-" * len(header_fmt))

        suffix_pattern = re.compile(r".*-\d{12}$")
        now = datetime.now(timezone.utc)

        for user in all_users:
            role = (
                "cluster-admin" if user in admins else
                "view" if user in views else
                "unknown"
            )
            cert_file = self.clusters_dir / cluster / "ssl/users" / f"{user}.pem"
            expiry = "N/A"
            days_left = "N/A"
            is_expired = False

            if cert_file.exists():
                expiry_cmd = [
                    str(self.extra_bin_dir / "cfssl-certinfo"),
                    "-cert", str(cert_file)
                ]
                expiry_info = run_command(expiry_cmd, shell=False).stdout
                match = re.search(r'"not_after"\s*:\s*"([^"]+)"', expiry_info)
                if match:
                    expiry = match.group(1)
                    try:
                        exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                        delta_days = (exp_dt - now).days
                        days_left = str(delta_days)
                        is_expired = exp_dt < now
                    except ValueError:
                        pass

            if expired_only and not is_expired:
                continue

            # 颜色选择
            color = AnsiColor.RESET.value
            if days_left != "N/A":
                days_int = int(days_left)
                if days_int < 0:
                    color = AnsiColor.RED.value
                elif days_int <= 7:
                    color = AnsiColor.YELLOW.value
                else:
                    color = AnsiColor.GREEN.value

            if show_all or suffix_pattern.match(user):
                expired_mark = "*" if is_expired else " "
                print(f"{expired_mark}{user:<29}{role:<18}{expiry:<30}{color}{days_left}{AnsiColor.RESET.value}")

        print("")

    def _validate_cluster(self, name: str) -> None:
        """Validate cluster exists"""
        if not (self.clusters_dir / name).exists():
            raise ClusterNotFoundError(f"Cluster {name} not found")

    def _validate_ip(self, ip: str) -> None:
        """Validate IP address"""
        if not validate_ip(ip):
            raise InvalidIPError(f"Invalid IP address: {ip}")

    def _check_node_exists(self, hosts_file: Path, ip: str, role: str) -> None:
        """Optimized version for large files using line-by-line reading"""
        section_patterns = {
            'etcd': ('[etcd]', '[kube_master]'),
            'master': ('[kube_master]', '[kube_node]'),
            'node': ('[kube_node]', None)  # node section is the last one
        }

        if role not in section_patterns:
            raise ValueError(f"Invalid node role: {role}")

        start_section, end_section = section_patterns[role]
        in_section = False

        with hosts_file.open() as f:
            for line in f:
                line = line.strip()

                if line == start_section:
                    in_section = True
                    continue

                if end_section and line == end_section:
                    break  # break in advance, no need to scan the whole file

                if in_section and line and not line.startswith('#'):
                    if line.startswith(ip) or f" {ip} " in line:
                        raise NodeExistsError(f"Node {ip} already exists in {role} section")

    def _check_node_not_exists(self, hosts_file: Path, ip: str, role: str) -> None:
        """Optimized version for checking node absence in large files"""
        section_patterns = {
            'etcd': ('[etcd]', '[kube_master]'),
            'master': ('[kube_master]', '[kube_node]'),
            'node': ('[kube_node]', None)  # node section is the last one
        }

        if role not in section_patterns:
            raise ValueError(f"Invalid node role: {role}")

        start_section, end_section = section_patterns[role]
        in_section = False

        with hosts_file.open() as f:
            for line in f:
                line = line.strip()

                if line == start_section:
                    in_section = True
                    continue

                if end_section and line == end_section:
                    break  # break in advance, no need to scan the whole file

                if in_section and line and not line.startswith('#'):
                    if line.startswith(ip) or f" {ip} " in line:
                        return  # If node exists, return immediately, no raise

        # Looked up the whole section, not found, raise
        raise NodeNotFoundError(f"Node {ip} not found in {role} section")

    def _add_to_hosts_section(self, hosts_file: Path, role: str, line: str) -> None:
        """Add a line to the end of a specific section using ipaddress library"""
        section = f"[kube_{role}]" if role != "etcd" else "[etcd]"

        content = hosts_file.read_text().splitlines()
        section_start = -1
        last_ip_line = -1

        for i, l in enumerate(content):
            if l.strip() == section:
                section_start = i
            elif section_start != -1 and l.startswith('[') and l.endswith(']'):
                break  # Next section found
            elif section_start != -1:
                # Try to parse first token as IP
                parts = l.split()
                if parts:
                    try:
                        ipaddress.ip_address(parts[0])
                        last_ip_line = i
                    except ValueError:
                        continue

        if section_start == -1:
            raise ValueError(f"Section {section} not found in hosts file")

        insert_pos = last_ip_line + 1 if last_ip_line != -1 else section_start + 1
        content.insert(insert_pos, line)
        hosts_file.write_text("\n".join(content) + "\n")

    def _remove_from_hosts_section(self, hosts_file: Path, role: str, ip: str) -> None:
        """Remove a line from a specific section in hosts file"""
        section = f"[kube_{role}]" if role != "etcd" else "[etcd]"

        content = hosts_file.read_text().splitlines()
        section_start = -1
        section_end = -1

        for i, line in enumerate(content):
            if line.strip() == section:
                section_start = i
            elif section_start != -1 and line.startswith("[") and section_end == -1:
                section_end = i
                break

        if section_start == -1:
            raise ValueError(f"Section {section} not found in hosts file")

        if section_end == -1:
            section_end = len(content)

        # Find and remove the line with the IP
        new_section = []
        removed = False

        for line in content[section_start + 1:section_end]:
            if not (line.startswith(ip) or f" {ip} " in line):
                new_section.append(line)
            else:
                removed = True

        if not removed:
            raise NodeNotFoundError(f"Node {ip} not found in {section} section")

        # Rebuild content
        new_content = content[:section_start + 1] + new_section + content[section_end:]
        hosts_file.write_text("\n".join(new_content) + "\n")

    def _notify_etcd_apiserver(self, cluster: str) -> None:
        hosts_file = self.clusters_dir / cluster / "hosts"
        config_file = self.clusters_dir / cluster / "config.yml"

        # Restart the etcd cluster
        logger.info("Restart the etcd cluster after adding or removing an etcd node", extra={"to_stdout": True})
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / "02.etcd.yml"),
                inventory=str(hosts_file),
                extravars=self._yaml_to_dict(config_file),
                roles_path=str(self.roles_dir),
                cmdline="-t restart_etcd"
            )
            if result.rc != 0:
                logger.error(f"Failed to restart the etcd cluster. Exit code: {result.rc}.",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info("Restart etcd cluster successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Failed to restart the etcd cluster.", extra={"to_stdout": True})
            raise e

        # Restart the apiservers to use the new etcd cluster
        logger.info("Restart the apiservers to adapt to the changed etcd cluster", extra={"to_stdout": True})
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / "04.kube-master.yml"),
                inventory=str(hosts_file),
                extravars=self._yaml_to_dict(config_file),
                roles_path=str(self.roles_dir),
                cmdline="-t restart_master"
            )
            if result.rc != 0:
                logger.error(f"Failed to restart the apiservers for the changed etcd cluster. Exit code: {result.rc}.",
                             extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info("Restart the apiservers for the changed etcd cluster successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Failed to restart the apiservers for the changed etcd cluster.", extra={"to_stdout": True})
            raise e

    def _restart_load_balancers(self, cluster: str) -> None:
        """Restart kube-lb and ex-lb services"""
        hosts_file = self.clusters_dir / cluster / "hosts"
        config_file = self.clusters_dir / cluster / "config.yml"

        # Restart kube-lb
        logger.info("Restart the kube-lb after adding or removing a master node", extra={"to_stdout": True})
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / "90.setup.yml"),
                inventory=str(hosts_file),
                extravars=self._yaml_to_dict(config_file),
                roles_path=str(self.roles_dir),
                cmdline="-t restart_kube-lb"
            )
            if result.rc != 0:
                logger.error(f"Failed to restart the kube-lb. Exit code: {result.rc}.", extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info("Restart the kube-lb successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Failed to restart the kube-lb.", extra={"to_stdout": True})
            raise e

        logger.info("Restart the ex-lb after adding or removing a master node", extra={"to_stdout": True})
        try:
            result = ansible_runner.run(
                playbook=str(self.playbooks_dir / "10.ex-lb.yml"),
                inventory=str(hosts_file),
                extravars=self._yaml_to_dict(config_file),
                roles_path=str(self.roles_dir),
                cmdline="-t restart_lb"
            )
            if result.rc != 0:
                logger.error(f"Failed to restart the ex-lb. Exit code: {result.rc}.", extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info("Restart the ex-lb successfully!", extra={"to_stdout": True})
        except Exception as e:
            logger.error("Failed to restart the ex-lb.", extra={"to_stdout": True})
            raise e

    def _kubectl_del_node(self, cluster: str, ip: str) -> None:
        kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"
        cmd = [f"kubectl --kubeconfig={kubeconfig} get node -o wide", "|", f"grep {ip}", "|", "awk '{print $1}'"]
        nodename = run_command(cmd, shell=True, capture_output=True).stdout.strip()

        logger.info(f"Deleting a master node {nodename}...", extra={"to_stdout": True})
        cmd = [str(self.kube_bin_dir / "kubectl"), "--kubeconfig", str(kubeconfig), "delete", "node", f"{nodename}"]
        run_command(cmd, shell=True, capture_output=False)
        logger.info("A master node has been deleted successfully!", extra={"to_stdout": True})

    def _reconfigure_kubeconfig(self, cluster: str) -> None:
        """Reconfigure kubeconfig after master node removal"""
        hosts_file = self.clusters_dir / cluster / "hosts"
        config_file = self.clusters_dir / cluster / "config.yml"

        logger.info("Reconfigure the kubeconfig after a master node removal.", extra={"to_stdout": True})
        try:
            result = ansible_runner.run(
                playbook=str(self.base_path / "roles/deploy/deploy.yml"),
                inventory=str(hosts_file),
                extravars=self._yaml_to_dict(config_file),
                roles_path=str(self.roles_dir),
                cmdline="-t create_kctl_cfg"
            )
            if result.rc != 0:
                logger.error(
                    f"Failed to reconfigure the kubeconfig after a master node removal. Exit code: {result.rc}.",
                    extra={"to_stdout": True})
                sys.exit(result.rc)
            logger.info("Reconfigure the kubeconfig after a master node removal successfully!",
                        extra={"to_stdout": True})
        except Exception as e:
            logger.error("Failed to reconfigure the kubeconfig after a master node removal.", extra={"to_stdout": True})
            raise e

    def _show_component_versions(self, cluster: str) -> None:
        """Show component versions before setup"""
        v_kube = run_command([str(self.kube_bin_dir / "kube-apiserver"), "--version"]).stdout.split()[1]
        v_etcd = "v" + run_command([str(self.extra_bin_dir / "etcd"), "--version"]).stdout.split()[2]

        # Get network plugin from hosts file
        hosts_content = (self.clusters_dir / cluster / "hosts").read_text()
        network_line = [l for l in hosts_content.splitlines() if l.startswith("CLUSTER_NETWORK=")]
        if network_line:
            network_plugin = network_line[0].split('"')[1].replace("-", "")
            v_network = getattr(self.kube_constant, f"v_{network_plugin.lower()}", "unknown")
        else:
            network_plugin = "unknown"
            v_network = "unknown"

        logger.info("*** Component Version *********************", extra={'to_stdout': True})
        logger.info("*******************************************", extra={'to_stdout': True})
        logger.info(f"*   kubernetes: {v_kube}", extra={'to_stdout': True})
        logger.info(f"*   etcd: {v_etcd}", extra={'to_stdout': True})
        logger.info(f"*   {network_plugin}: {v_network}", extra={'to_stdout': True})
        logger.info("*******************************************", extra={'to_stdout': True})
