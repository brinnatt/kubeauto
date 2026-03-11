"""
Main cluster operations for kubeauto
"""
# ansible_runner usage: on host use private_data_dir in temp; bubblewrap/docker use
# process_isolation with bwrap or container_image. See ansible-runner docs for details.
import ipaddress
import re
import shutil
import ansible_runner
import yaml
import sys
import tempfile
from taskflow import task
from pathlib import Path
from datetime import datetime, timezone
from typing import Generator, List, Optional, Tuple

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException as K8sApiException

from common.utils import run_command, validate_ip, confirm_action, AnsiColor, get_resource_path, rmrf, copy_file_to_remote
from common.exceptions import (
    ClusterExistsError, ClusterNotFoundError,
    InvalidIPError, NodeExistsError, NodeNotFoundError, ClusterNewError, ClusterSetupError, ClusterManageError,
    InstallPrereqError,
)
from common.logger import setup_logger, LOG_STDOUT
from common.constants import KubeConstant

logger = setup_logger(__name__)

# 节点角色在日志中的可读标签，避免 "Add node node 1.2.3.4" 这类重复
_ROLE_LABEL = {"master": "master node", "node": "worker node", "etcd": "etcd node"}

# setup_cluster 步骤 -> playbook 映射
_PLAYBOOK_MAP_SETUP = {
    "01": "01.prepare.yml", "prepare": "01.prepare.yml",
    "02": "02.etcd.yml", "etcd": "02.etcd.yml",
    "03": "03.runtime.yml", "container-runtime": "03.runtime.yml",
    "04": "04.kube-master.yml", "kube-master": "04.kube-master.yml",
    "05": "05.kube-node.yml", "kube-node": "05.kube-node.yml",
    "06": "06.network.yml", "network": "06.network.yml",
    "07": "07.cluster-addon.yml", "cluster-addon": "07.cluster-addon.yml",
    "90": "90.setup.yml", "all": "90.setup.yml",
    "10": "10.ex-lb.yml", "ex-lb": "10.ex-lb.yml",
    "11": "11.harbor.yml", "harbor": "11.harbor.yml",
}

# cluster_command 命令 -> playbook 映射
_PLAYBOOK_MAP_CLUSTER_COMMAND = {
    "start": "91.start.yml", "stop": "92.stop.yml", "upgrade": "93.upgrade.yml",
    "backup": "94.backup.yml", "restore": "95.restore.yml", "destroy": "99.clean.yml",
}

# add_node / remove_node 角色 -> playbook 映射
_PLAYBOOK_MAP_ADD_NODE = {"etcd": "21.addetcd.yml", "master": "23.addmaster.yml", "node": "22.addnode.yml"}
_PLAYBOOK_MAP_REMOVE_NODE = {"etcd": "31.deletcd.yml", "master": "33.delmaster.yml", "node": "32.delnode.yml"}

# hosts 文件中角色对应的区段 (start_section, end_section)，end_section 为 None 表示到文件末尾
_HOSTS_SECTION_PATTERNS = {
    "etcd": ("[etcd]", "[kube_master]"),
    "master": ("[kube_master]", "[kube_node]"),
    "node": ("[kube_node]", None),
}


def _hosts_section_name(role: str) -> str:
    """Return hosts section header for role, e.g. [etcd] or [kube_master]."""
    return "[etcd]" if role == "etcd" else f"[kube_{role}]"


def _iter_host_entries(hosts_file: Path, role: str) -> Generator[Tuple[str, str], None, None]:
    """Yield (line, first_ip) for each host line in the role's section. Skips comments and non-IP lines."""
    if role not in _HOSTS_SECTION_PATTERNS:
        raise ValueError(f"Invalid node role: {role}")
    start_section, end_section = _HOSTS_SECTION_PATTERNS[role]
    in_section = False
    with hosts_file.open() as f:
        for raw in f:
            line = raw.strip()
            if line == start_section:
                in_section = True
                continue
            if end_section and line == end_section:
                break
            if in_section and line and not line.startswith("#"):
                parts = line.split()
                if parts:
                    try:
                        ipaddress.ip_address(parts[0])
                        yield (line, parts[0])
                    except ValueError:
                        continue


class ClusterManager:
    def __init__(self):
        self.kube_constant = KubeConstant()
        self.base_path = Path(self.kube_constant.BASE_PATH)
        self.kube_bin_dir = Path(self.kube_constant.KUBE_BIN_DIR)
        self.extra_bin_dir = Path(self.kube_constant.EXTRA_BIN_DIR)
        self.clusters_dir = self.base_path / "clusters"

    def list_clusters(self) -> List[str]:
        """List all managed clusters"""
        if not self.clusters_dir.exists():
            raise ClusterNotFoundError("Cluster directory not found, run 'new' first")

        if not (Path.home() / ".kube/config").exists():
            raise ClusterNotFoundError("kubeconfig not found, run 'setup' first")

        clusters = [
            d.name for d in self.clusters_dir.iterdir()
            if d.is_dir() and (d / "kubectl.kubeconfig").exists()
        ]
        return sorted(clusters)

    def get_current_cluster(self) -> Optional[str]:
        """Get current cluster from kubeconfig"""
        current_config = Path.home() / ".kube/config"
        if not current_config.exists():
            return None

        try:
            current_md5 = self._config_md5(current_config)
            for cluster in self.list_clusters():
                cluster_config = self.clusters_dir / cluster / "kubectl.kubeconfig"
                if self._config_md5(cluster_config) == current_md5:
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

        # Require cluster resource templates to exist (from project or install)
        example_hosts_path = get_resource_path("conf", "hosts.multi-node")
        example_config_path = get_resource_path("conf", "config.yml")
        if not Path(example_hosts_path).exists():
            raise InstallPrereqError(
                f"Cluster template not found: {example_hosts_path}. Run 'kubecli download -D' or ensure install is complete."
            )
        if not Path(example_config_path).exists():
            raise InstallPrereqError(
                f"Cluster template not found: {example_config_path}. Run 'kubecli download -D' or ensure install is complete."
            )

        logger.debug(f"Creating cluster directory: {cluster_dir}")
        cluster_dir.mkdir(parents=True, exist_ok=True)

        cluster_hosts = cluster_dir / "hosts"
        cluster_config = cluster_dir / "config.yml"
        try:
            cluster_hosts.write_text(Path(example_hosts_path).read_text())
            cluster_config.write_text(Path(example_config_path).read_text())

            hosts_content = cluster_hosts.read_text().replace("_cluster_name_", name)
            config_content = cluster_config.read_text()
            for placeholder, value in self._get_config_placeholders().items():
                config_content = config_content.replace(placeholder, value)

            cluster_hosts.write_text(hosts_content)
            cluster_config.write_text(config_content)
        except Exception as e:
            raise ClusterNewError(f"Error creating cluster hosts or config: {e}")

        logger.info(f"Cluster {name} created. Next: edit {cluster_hosts} and {cluster_config}, then run setup.", extra=LOG_STDOUT)

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
                playbook=get_resource_path("playbooks", playbook),
                inventory=str(self.clusters_dir / name / "hosts"),
                extravars=self._yaml_to_dict(self.clusters_dir / name / 'config.yml'),
                roles_path=get_resource_path("roles"),
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
        self._validate_for_setup(name)

        playbook = _PLAYBOOK_MAP_SETUP.get(step, "dummy.yml")
        if playbook == "dummy.yml":
            logger.error(f"Invalid setup step: {step}. Use: all, master, node, or etcd.", extra=LOG_STDOUT)
            return

        logger.info(f"Setting up cluster with playbook {playbook}.", extra=LOG_STDOUT)
        self._show_component_versions(name)
        if not confirm_action(f"cluster:{name} setup step:{step} begins"):
            return

        self._run_playbook(
            name,
            playbook,
            cmdline=" ".join(extra_args or []),
            fail_msg=f"Failed to set up the k8s cluster with playbook {playbook}",
            fail_exception=ClusterSetupError,
        )
        logger.info(f"Cluster setup completed (playbook {playbook}).", extra=LOG_STDOUT)

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

    def _get_config_placeholders(self) -> dict:
        """Return placeholder -> value dict for cluster config.yml (version strings from KubeConstant)."""
        kc = self.kube_constant
        return {
            "__k8s_ver__": kc.v_k8s_bin.lstrip("v"),
            "__flannel__": kc.v_flannel,
            "__calico__": kc.v_calico,
            "__cilium__": kc.v_cilium,
            "__kube_ovn__": kc.v_kubeovn,
            "__kube_router__": kc.v_kuberouter,
            "__coredns__": kc.v_coredns,
            "__pause__": kc.v_pause,
            "__dns_node_cache__": kc.v_dnsnodecache,
            "__dashboard__": kc.v_dashboard,
            "__local_path_provisioner__": kc.v_localpathprovisioner,
            "__nfs_provisioner__": kc.v_nfsprovisioner,
            "__prom_chart__": kc.v_promchart,
            "__harbor__": kc.v_harbor,
            "__metrics__": kc.v_metricsserver,
            "__minio_chart__": kc.v_miniooperator,
            "__openebs_ver__": kc.v_openebs,
            "__ingress_nginx_ver__": kc.v_ingressnginx,
        }

    def _run_playbook(
        self,
        cluster: str,
        playbook: str | Path,
        *,
        inventory: Path | None = None,
        extra_vars: dict | None = None,
        cmdline: str | None = None,
        fail_msg: str | None = None,
        fail_exception: type = ClusterManageError,
    ):
        """Run Ansible playbook in a temp dir. When fail_msg is set and rc != 0, log and raise fail_exception."""
        inv = inventory or (self.clusters_dir / cluster / "hosts")
        ev = extra_vars if extra_vars is not None else self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        if isinstance(playbook, Path):
            pb_path = str(playbook)
        elif "/" in str(playbook):
            pb_path = get_resource_path(*str(playbook).split("/"))
        else:
            pb_path = get_resource_path("playbooks", playbook)
        with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="ansible-runner-") as tmp_dir:
            result = ansible_runner.run(
                private_data_dir=tmp_dir,
                playbook=pb_path,
                inventory=str(inv),
                extravars=ev,
                roles_path=get_resource_path("roles"),
                cmdline=cmdline or "",
            )
        if fail_msg and result.rc != 0:
            logger.error(fail_msg, extra=LOG_STDOUT)
            raise fail_exception(fail_msg)
        return result

    def cluster_command(self, name: str, command: str) -> None:
        """Execute cluster-wide command (start, stop, upgrade, backup, restore, destroy)"""
        self._validate_for_setup(name)

        playbook = _PLAYBOOK_MAP_CLUSTER_COMMAND.get(command)
        if not playbook:
            logger.error(f"Invalid cluster command: {command}.", extra=LOG_STDOUT)
            return

        logger.info(f"Cluster {name}: running {command} (playbook {playbook}).", extra=LOG_STDOUT)
        self._show_component_versions(name)
        if not confirm_action(f"Cluster {name}: proceed with {command}?"):
            return

        self._run_playbook(
            name,
            playbook,
            fail_msg=f"Failed to {command} cluster {name} with playbook {playbook}.",
        )
        logger.info(f"Cluster {name}: {command} completed.", extra=LOG_STDOUT)
        # destroy: remove cluster dir so next new/setup or start-aio can run clean
        if command == "destroy":
            rmrf(self.clusters_dir / name)
            logger.info(f"Cluster directory {name} removed.", extra=LOG_STDOUT)

    def checkout_cluster(self, name: str) -> None:
        """Switch to a cluster's kubeconfig"""
        self._validate_cluster(name)

        kubeconfig = self.clusters_dir / name / "kubectl.kubeconfig"
        if not kubeconfig.exists():
            raise ClusterNotFoundError(f"Invalid kubeconfig, run 'setup {name}' first")

        dest_config = Path.home() / ".kube/config"
        dest_config.parent.mkdir(exist_ok=True)

        run_command(["cp", "-f", str(kubeconfig), str(dest_config)])
        logger.info(f"Current kubeconfig set to cluster {name}.", extra=LOG_STDOUT)

    def add_node(self, cluster: str, ip: str, role: str, extra_info: str = "") -> None:
        """Add a node to the cluster"""
        self._validate_for_setup(cluster)
        self._validate_ip(ip)

        hosts_file = self.clusters_dir / cluster / "hosts"
        if not hosts_file.exists():
            raise ClusterNotFoundError(f"Hosts file not found for cluster {cluster}")

        self._check_node_exists(hosts_file, ip, role)

        # add-master/add-node: require and validate k8s_nodename
        # add-etcd: if etcd is on same host as master/node, nodename optional; if standalone etcd, require k8s_nodename
        if role in ("master", "node"):
            self._validate_k8s_nodename(extra_info)
            nodename = extra_info.strip()
            node_line = f"{ip} k8s_nodename='{nodename}'"
        elif role == "etcd":
            if self._is_ip_in_kube_master_or_node(hosts_file, ip):
                node_line = ip
            else:
                self._validate_k8s_nodename(extra_info)
                nodename = extra_info.strip()
                node_line = f"{ip} k8s_nodename='{nodename}'"
        else:
            node_line = f"{ip} {extra_info}".strip() if extra_info else ip
        self._add_to_hosts_section(hosts_file, role, node_line)

        playbook = _PLAYBOOK_MAP_ADD_NODE.get(role)
        if not playbook:
            raise ValueError(f"Invalid role: {role}")

        logger.info(f"Adding {_ROLE_LABEL[role]} {ip} to cluster {cluster}.", extra=LOG_STDOUT)

        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        extra_vars["NODE_TO_ADD"] = ip

        self._run_playbook(
            cluster,
            playbook,
            inventory=hosts_file,
            extra_vars=extra_vars,
            fail_msg=f"Failed to add {_ROLE_LABEL[role]} {ip} to cluster {cluster}.",
        )
        logger.info(f"Added {_ROLE_LABEL[role]} {ip} to cluster {cluster}.", extra=LOG_STDOUT)

        # After adding a new node, we still have to notify related services
        if role == "etcd":
            self._notify_etcd_apiserver(cluster)
        elif role == "master":
            self._restart_load_balancers(cluster)
        elif role == "node":
            pass

    def remove_node(self, cluster: str, ip: str, role: str) -> None:
        """Remove a node from the cluster"""
        self._validate_for_setup(cluster)
        self._validate_ip(ip)

        hosts_file = self.clusters_dir / cluster / "hosts"
        if not hosts_file.exists():
            raise ClusterNotFoundError(f"Hosts file not found for cluster {cluster}")

        self._check_node_not_exists(hosts_file, ip, role)

        playbook = _PLAYBOOK_MAP_REMOVE_NODE.get(role)
        if not playbook:
            raise ValueError(f"Invalid role: {role}")

        logger.info(f"Removing {_ROLE_LABEL[role]} {ip} from cluster {cluster}.", extra=LOG_STDOUT)
        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        extra_vars["NODE_TO_DEL"] = ip
        extra_vars["CLUSTER"] = cluster

        self._run_playbook(
            cluster,
            playbook,
            inventory=hosts_file,
            extra_vars=extra_vars,
            fail_msg=f"Failed to remove {_ROLE_LABEL[role]} {ip} from cluster {cluster}.",
        )
        logger.info(f"Removed {_ROLE_LABEL[role]} {ip} from cluster {cluster}.", extra=LOG_STDOUT)

        # Remove node from hosts file
        self._remove_from_hosts_section(hosts_file, role, ip)

        # After removing a node, we still have to notify related services
        if role == "etcd":
            self._notify_etcd_apiserver(cluster)
        elif role == "master":
            self._reconfigure_kubeconfig(cluster)
            self._restart_load_balancers(cluster)
            self._kubectl_del_node(cluster, ip, role)
        elif role == "node":
            self._kubectl_del_node(cluster, ip, role)

    def renew_ca_certs(self, cluster: str) -> None:
        """Force renew CA certificates and all other certs in the cluster"""
        self._validate_for_setup(cluster)

        logger.warning("This will recreate CA and all cluster certs. Only use if admin.conf was compromised.", extra=LOG_STDOUT)
        if not confirm_action(f"Renew all certs in cluster {cluster}"):
            return

        logger.info(f"Renewing all certificates in cluster {cluster}.", extra=LOG_STDOUT)
        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        extra_vars["CHANGE_CA"] = "true"
        self._run_playbook(
            cluster,
            "96.update-certs.yml",
            extra_vars=extra_vars,
            cmdline="-t force_change_certs",
            fail_msg=f"Failed to renew all certs in cluster {cluster}.",
        )
        logger.info(f"All certificates in cluster {cluster} renewed.", extra=LOG_STDOUT)

    def kubeconfig_admin(self, cluster: str, action: str,
                         user_name: str = None, user_type: str = "admin",
                         expiry: str = "4800h", show_all=False, expired_only=False) -> None:
        self._validate_cluster(cluster)
        kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"

        if action == "add":
            self._validate_cluster_files(cluster)
            self._require_install_prereqs()
            self._add_kcfg(cluster, user_name, user_type, expiry)
        elif action == "delete":
            if not kubeconfig.exists():
                raise ClusterNotFoundError(f"Kubeconfig not found for cluster {cluster}. Run 'setup {cluster}' first.")
            self._del_kcfg(cluster, user_name, kubeconfig)
        elif action == "list":
            if not kubeconfig.exists():
                raise ClusterNotFoundError(f"Kubeconfig not found for cluster {cluster}. Run 'setup {cluster}' first.")
            self._list_kcfg(cluster, kubeconfig, show_all, expired_only)
        else:
            raise ValueError(f"Unknown action: {action}")

    def _add_kcfg(self, cluster, user_name, user_type, expiry):
        if not user_name:
            user_name = f"user-{datetime.now().strftime('%Y%m%d%H%M')}"
        logger.info(f"Adding kubeconfig for user {user_name} in cluster {cluster}.", extra=LOG_STDOUT)
        extra_vars = self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        extra_vars.update(CUSTOM_EXPIRY=expiry, USER_TYPE=user_type, USER_NAME=user_name, ADD_KCFG="true")
        self._run_playbook(
            cluster,
            "roles/deploy/deploy.yml",
            extra_vars=extra_vars,
            cmdline="-t add-kcfg",
            fail_msg=f"Failed to add kubeconfig for user {user_name} in cluster {cluster}.",
        )
        logger.info(f"Kubeconfig for user {user_name} added to cluster {cluster}.", extra=LOG_STDOUT)

    def _k8s_api_client(self, kubeconfig_path: Path):
        """Return ApiClient context manager for the given kubeconfig (official best practice: explicit cleanup).
        Use: with self._k8s_api_client(path) as api_client: ..."""
        configuration = k8s_client.Configuration()
        k8s_config.load_kube_config(config_file=str(kubeconfig_path), client_configuration=configuration)
        return k8s_client.ApiClient(configuration)

    def _del_kcfg(self, cluster, user_name, kubeconfig):
        if not user_name:
            raise ValueError("User name is required for delete action")
        logger.info(f"Removing kubeconfig for user {user_name} from cluster {cluster}.", extra=LOG_STDOUT)

        try:
            with self._k8s_api_client(kubeconfig) as api_client:
                rbac = k8s_client.RbacAuthorizationV1Api(api_client)
                crb_list = rbac.list_cluster_role_binding()
                for crb in crb_list.items:
                    for subj in (crb.subjects or []):
                        if subj.name == user_name:
                            rbac.delete_cluster_role_binding(crb.metadata.name)
                            break
        except K8sApiException as e:
            logger.warning(f"Kubernetes API warning during kubeconfig delete: {e.reason or e.body}")

        cert_pattern = str(self.clusters_dir / cluster / "ssl/users" / f"{user_name}*")
        run_command(f"rm -f {cert_pattern}", shell=True)
        crb_pattern = str(self.clusters_dir / cluster / "ssl/users" / f"crb-{user_name}*")
        run_command(f"rm -f {crb_pattern}", shell=True)
        logger.info(f"Kubeconfig for user {user_name} removed from cluster {cluster}.", extra=LOG_STDOUT)

    def _get_kcfg_users(self, kubeconfig: Path, role_name: Optional[str] = None) -> List[str]:
        """List usernames from ClusterRoleBindings, optionally filtered by role (e.g. cluster-admin, view)."""
        with self._k8s_api_client(kubeconfig) as api_client:
            rbac = k8s_client.RbacAuthorizationV1Api(api_client)
            crb_list = rbac.list_cluster_role_binding()
            names = []
            for crb in crb_list.items:
                if role_name and (not crb.role_ref or crb.role_ref.name != role_name):
                    continue
                for subj in (crb.subjects or []):
                    if subj.name:
                        names.append(subj.name)
            return names

    def _get_cert_expiry_info(self, cert_file: Path) -> Tuple[str, str, bool]:
        """Return (expiry_str, days_left_str, is_expired) for a user cert file. N/A and False if unreadable."""
        expiry, days_left, is_expired = "N/A", "N/A", False
        if not cert_file.exists():
            return expiry, days_left, is_expired
        expiry_cmd = [str(self.extra_bin_dir / "cfssl-certinfo"), "-cert", str(cert_file)]
        expiry_info = run_command(expiry_cmd, shell=False).stdout
        match = re.search(r'"not_after"\s*:\s*"([^"]+)"', expiry_info)
        if match:
            expiry = match.group(1)
            try:
                now = datetime.now(timezone.utc)
                exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                delta_days = (exp_dt - now).days
                days_left = str(delta_days)
                is_expired = exp_dt < now
            except ValueError:
                pass
        return expiry, days_left, is_expired

    def _list_kcfg(self, cluster, kubeconfig, show_all=False, expired_only=False):
        logger.info(f"Listing kubeconfig users in cluster {cluster}.", extra=LOG_STDOUT)
        admins = set(self._get_kcfg_users(kubeconfig, "cluster-admin"))
        views = set(self._get_kcfg_users(kubeconfig, "view"))
        bound_users = set(self._get_kcfg_users(kubeconfig))
        if show_all:
            cert_users = {
                p.stem for p in (self.clusters_dir / cluster / "ssl/users").glob("*.pem")
                if not p.name.endswith("-key.pem")
            }
            all_users = sorted(cert_users)
        else:
            all_users = sorted(bound_users)

        header_fmt = f"{'USER':<30}{'TYPE':<18}{'EXPIRY(+8h if in Asia/Shanghai)':<30}{'DAYS_LEFT'}"
        print("\n" + header_fmt)
        print("-" * len(header_fmt))
        suffix_pattern = re.compile(r".*-\d{12}$")

        for user in all_users:
            role = "cluster-admin" if user in admins else "view" if user in views else "unknown"
            cert_file = self.clusters_dir / cluster / "ssl/users" / f"{user}.pem"
            expiry, days_left, is_expired = self._get_cert_expiry_info(cert_file)
            if expired_only and not is_expired:
                continue
            color = AnsiColor.RESET.value
            if days_left != "N/A":
                days_int = int(days_left)
                color = AnsiColor.RED.value if days_int < 0 else AnsiColor.YELLOW.value if days_int <= 7 else AnsiColor.GREEN.value
            if show_all or suffix_pattern.match(user):
                expired_mark = "*" if is_expired else " "
                print(f"{expired_mark}{user:<29}{role:<18}{expiry:<30}{color}{days_left}{AnsiColor.RESET.value}")
        print("")

    def _require_install_prereqs(self) -> None:
        """Require components from 'kubecli download -D' (Ansible, kube-bin, extra-bin). Raises InstallPrereqError with hint if missing. Used by: setup, start-aio, add/del node, cluster_command, kca-renew, kcfg add."""
        missing = []
        if not shutil.which("ansible"):
            missing.append("Ansible")
        if not (self.kube_bin_dir / "kubelet").exists():
            missing.append("Kubernetes binaries (kube-bin)")
        if not (self.extra_bin_dir / "etcdctl").exists():
            missing.append("extra binaries (extra-bin)")
        if missing:
            raise InstallPrereqError(
                f"Missing required components: {', '.join(missing)}. "
                "Run 'kubecli download -D' to install them, then retry."
            )

    @staticmethod
    def _config_md5(config_path: Path) -> str:
        """Return MD5 of kubeconfig content excluding server field (for comparing current vs cluster config)."""
        md5_cmd = ["sed", "/server/d", str(config_path), "|", "md5sum"]
        return run_command(md5_cmd, shell=True).stdout.split()[0]

    def _validate_cluster(self, name: str) -> None:
        """Validate cluster directory exists"""
        if not (self.clusters_dir / name).exists():
            raise ClusterNotFoundError(f"Cluster {name} not found")

    def _validate_cluster_files(self, name: str) -> None:
        """Validate cluster has hosts and config.yml (required for setup/playbooks). Call after _validate_cluster."""
        cluster_dir = self.clusters_dir / name
        hosts = cluster_dir / "hosts"
        config = cluster_dir / "config.yml"
        if not hosts.exists():
            raise ClusterNotFoundError(f"Hosts file not found for cluster {name}. Run 'new {name}' or fix the cluster directory.")
        if not config.exists():
            raise ClusterNotFoundError(f"Config file not found for cluster {name}. Run 'new {name}' or fix the cluster directory.")

    def _validate_for_setup(self, name: str) -> None:
        """Validate cluster exists, has hosts/config, and install prereqs. Used by setup, cluster_command, add/remove node, renew_ca, kcfg add."""
        self._validate_cluster(name)
        self._validate_cluster_files(name)
        self._require_install_prereqs()

    def _validate_ip(self, ip: str) -> None:
        """Validate IP address"""
        if not validate_ip(ip):
            raise InvalidIPError(f"Invalid IP address: {ip}")

    @staticmethod
    def _validate_k8s_nodename(nodename: str) -> None:
        """Validate k8s_nodename: lowercase alphanumeric, '-' or '.', must start and end with alphanumeric (same as config.yml)."""
        s = (nodename or "").strip()
        if not s:
            raise ValueError("k8s_nodename is required for add-master/add-node (e.g. master-02, worker-01)")
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", s):
            raise ValueError(
                "k8s_nodename must be lowercase alphanumeric, '-' or '.', and start/end with alphanumeric (e.g. master-02)"
            )

    def _is_ip_in_kube_master_or_node(self, hosts_file: Path, ip: str) -> bool:
        """Return True if ip already appears in [kube_master] or [kube_node] (etcd on same host as master/node)."""
        return ip in self._get_kube_master_and_node_ips(hosts_file)

    def _get_first_master_ip(self, hosts_file: Path) -> Optional[str]:
        """Return the first IP in [kube_master] section, or None if section empty/missing."""
        return next((ip for _, ip in _iter_host_entries(hosts_file, "master")), None)

    def _get_kube_master_and_node_ips(self, hosts_file: Path) -> List[str]:
        """Return all IPs in [kube_master] and [kube_node] (order preserved, no duplicate)."""
        seen: set = set()
        result: List[str] = []
        for role in ("master", "node"):
            for _, ip in _iter_host_entries(hosts_file, role):
                if ip not in seen:
                    seen.add(ip)
                    result.append(ip)
        return result

    def _ip_in_hosts_section(self, hosts_file: Path, ip: str, role: str) -> bool:
        """Return True if ip appears in the role's section of hosts file (first token of a line)."""
        return any(first_ip == ip for _, first_ip in _iter_host_entries(hosts_file, role))

    def _check_node_exists(self, hosts_file: Path, ip: str, role: str) -> None:
        """Check node already exists in hosts section (for add: should not exist)."""
        if self._ip_in_hosts_section(hosts_file, ip, role):
            raise NodeExistsError(f"Node {ip} already exists in {role} section")

    def _check_node_not_exists(self, hosts_file: Path, ip: str, role: str) -> None:
        """Check node does not exist in hosts section (for remove: should exist)."""
        if not self._ip_in_hosts_section(hosts_file, ip, role):
            raise NodeNotFoundError(f"Node {ip} not found in {role} section")

    def _add_to_hosts_section(self, hosts_file: Path, role: str, line: str) -> None:
        """Add a line to the end of a specific section using ipaddress library (original logic)."""
        section = _hosts_section_name(role)
        content = hosts_file.read_text().splitlines()
        section_start = -1
        last_ip_line = -1
        for i, l in enumerate(content):
            if l.strip() == section:
                section_start = i
            elif section_start != -1 and l.startswith("[") and l.endswith("]"):
                break
            elif section_start != -1:
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
        """Remove a line from a specific section in hosts file (original logic)."""
        section = _hosts_section_name(role)
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
        new_section = []
        removed = False
        for line in content[section_start + 1 : section_end]:
            parts = line.strip().split()
            if not parts or parts[0] != ip:
                new_section.append(line)
            else:
                removed = True
        if not removed:
            raise NodeNotFoundError(f"Node {ip} not found in {section} section")
        new_content = content[: section_start + 1] + new_section + content[section_end:]
        hosts_file.write_text("\n".join(new_content) + "\n")

    def _notify_etcd_apiserver(self, cluster: str) -> None:
        logger.info("Restarting etcd cluster (membership changed).", extra=LOG_STDOUT)
        self._run_playbook(
            cluster, "02.etcd.yml", cmdline="-t restart_etcd",
            fail_msg="Failed to restart the etcd cluster.",
        )
        logger.info("Etcd cluster restarted.", extra=LOG_STDOUT)
        logger.info("Restarting apiservers to pick up etcd membership change.", extra=LOG_STDOUT)
        self._run_playbook(
            cluster, "04.kube-master.yml", cmdline="-t restart_master",
            fail_msg="Failed to restart the apiservers for the changed etcd cluster.",
        )
        logger.info("Apiservers restarted.", extra=LOG_STDOUT)

    def _restart_load_balancers(self, cluster: str) -> None:
        """Restart kube-lb and ex-lb services"""
        logger.info("Restarting kube-lb (master membership changed).", extra=LOG_STDOUT)
        self._run_playbook(
            cluster, "90.setup.yml", cmdline="-t restart_kube-lb",
            fail_msg="Failed to restart the kube-lb for the changed cluster membership.",
        )
        logger.info("Kube-lb restarted.", extra=LOG_STDOUT)
        logger.info("Restarting ex-lb (master membership changed).", extra=LOG_STDOUT)
        self._run_playbook(
            cluster, "10.ex-lb.yml", cmdline="-t restart_lb",
            fail_msg="Failed to restart the ex-lb for the changed cluster membership.",
        )
        logger.info("Ex-lb restarted.", extra=LOG_STDOUT)

    def _kubectl_del_node(self, cluster: str, ip: str, role: str) -> None:
        """Delete node from cluster by IP using Kubernetes API (official client, no shell grep/awk)."""
        kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"
        try:
            with self._k8s_api_client(kubeconfig) as api_client:
                v1 = k8s_client.CoreV1Api(api_client)
                nodes = v1.list_node()
                node_name = None
                for node in nodes.items:
                    for addr in (node.status.addresses or []):
                        if addr.address == ip:
                            node_name = node.metadata.name
                            break
                    if node_name:
                        break
                if not node_name:
                    logger.warning(f"No node with IP {ip} found in cluster; skipping API delete.", extra=LOG_STDOUT)
                    return
                logger.info(f"Deleting {_ROLE_LABEL[role]} {node_name} from cluster.", extra=LOG_STDOUT)
                v1.delete_node(node_name)
                logger.info(f"Node {node_name} removed from cluster.", extra=LOG_STDOUT)
        except K8sApiException as e:
            logger.error(f"Failed to delete node from cluster: {e.reason or e.body}", extra=LOG_STDOUT)
            raise ClusterManageError(f"Failed to delete node from cluster: {e.reason or str(e)}")

    def _reconfigure_kubeconfig(self, cluster: str) -> None:
        """Update kubeconfig server to the new first master after removal (so later steps use a live API)."""
        logger.info("Reconfiguring kubeconfig after master node removal.", extra=LOG_STDOUT)
        hosts_file = self.clusters_dir / cluster / "hosts"
        kubeconfig_path = self.clusters_dir / cluster / "kubectl.kubeconfig"
        first_master = self._get_first_master_ip(hosts_file)
        if not first_master:
            raise ClusterManageError("No kube_master left in hosts; cannot reconfigure kubeconfig.")
        if not kubeconfig_path.exists():
            raise ClusterNotFoundError(f"Kubeconfig not found: {kubeconfig_path}")
        config_vars = self._yaml_to_dict(self.clusters_dir / cluster / "config.yml")
        port = config_vars.get("SECURE_PORT", "6443")
        new_server = f"https://{first_master}:{port}"
        data = yaml.safe_load(kubeconfig_path.read_text()) or {}
        for c in data.get("clusters", []):
            if "cluster" in c:
                c["cluster"]["server"] = new_server
        kubeconfig_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
        # Sync to all remaining master/node so each node's /root/.kube/config points to new first master
        for ip in self._get_kube_master_and_node_ips(hosts_file):
            copy_file_to_remote(kubeconfig_path, "/root/.kube/config", host=ip, mode=0o400)
        logger.info("Kubeconfig reconfigured.", extra=LOG_STDOUT)

    def _is_cluster_live(self, kubeconfig_path: Path) -> bool:
        """Check if cluster has at least one Ready node (Kubernetes API). Used for aio precondition/revert safety."""
        if not kubeconfig_path.exists():
            return False
        try:
            with self._k8s_api_client(kubeconfig_path) as api_client:
                v1 = k8s_client.CoreV1Api(api_client)
                nodes = v1.list_node()
                for node in nodes.items:
                    for cond in (node.status.conditions or []):
                        if getattr(cond, "type", None) == "Ready" and getattr(cond, "status", None) == "True":
                            return True
                return False
        except (K8sApiException, Exception):
            return False

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

        logger.info("Component versions (kubernetes / etcd / network):", extra=LOG_STDOUT)
        logger.info(f"  kubernetes: {v_kube}, etcd: {v_etcd}, {network_plugin}: {v_network}", extra=LOG_STDOUT)


class SetupAIO(task.Task):
    """All-in-one cluster setup task; uses ClusterManager and Kubernetes API for safe re-entry and revert."""

    AIO_CLUSTER = "aio"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cluster_manager = ClusterManager()

    def execute(self) -> None:
        """Start an all-in-one cluster with default settings. Idempotent: skip if aio already live (K8s API)."""
        from common.utils import get_host_ip, ssh_localhost

        m = self.cluster_manager
        m._require_install_prereqs()

        aio_dir = m.clusters_dir / self.AIO_CLUSTER
        aio_kubeconfig = aio_dir / "kubectl.kubeconfig"

        # Precondition: if aio cluster already exists and is live, do not re-install (enterprise idempotent)
        if aio_dir.exists() and aio_kubeconfig.exists():
            if m._is_cluster_live(aio_kubeconfig):
                logger.info(
                    "All-in-one cluster already exists and is live; skipping install (idempotent).",
                    extra=LOG_STDOUT,
                )
                return
            # Dir exists but cluster not live: partial/failed state, do not overwrite blindly
            raise ClusterExistsError(
                f"Cluster {self.AIO_CLUSTER} directory exists but cluster is not live. "
                "Remove it manually if you want to retry, or fix the cluster."
            )

        logger.info("Initializing all-in-one cluster environment.", extra=LOG_STDOUT)
        host_ip = get_host_ip()
        ssh_localhost()

        m.new_cluster(self.AIO_CLUSTER)
        aio_hosts = aio_dir / "hosts"
        aio_hosts.write_text(
            Path(get_resource_path("conf", "hosts.allinone")).read_text()
            .replace("192.168.1.1", host_ip)
            .replace("_cluster_name_", self.AIO_CLUSTER)
        )
        logger.info("All-in-one cluster environment initialized.", extra=LOG_STDOUT)

        try:
            logger.info("Creating all-in-one cluster.", extra=LOG_STDOUT)
            m.setup_cluster(self.AIO_CLUSTER, "all")
            logger.info("All-in-one cluster created successfully.", extra=LOG_STDOUT)
        except Exception as e:
            logger.error("All-in-one cluster creation failed.", extra=LOG_STDOUT)
            raise e

    def revert(self, result, flow_failures, **kwargs) -> None:
        """Revert only when cluster is not live (K8s API). Refuse to tear down a live cluster."""
        m = self.cluster_manager
        aio_dir = m.clusters_dir / self.AIO_CLUSTER
        aio_kubeconfig = aio_dir / "kubectl.kubeconfig"

        # Safety: do not revert if cluster has Ready nodes (production protection)
        if aio_kubeconfig.exists() and m._is_cluster_live(aio_kubeconfig):
            logger.error(
                "Refusing to revert: all-in-one cluster is live (has Ready nodes). Revert would destroy it.",
                extra=LOG_STDOUT,
            )
            raise ClusterManageError(
                "Refuse to revert: cluster is live. Revert is only for failed installs."
            )

        logger.info(
            f"Reverting failed all-in-one install (task failed: {result.exception_str}).",
            extra=LOG_STDOUT,
        )
        with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="ansible-runner-") as tmp_dir:
            run_result = ansible_runner.run(
                private_data_dir=tmp_dir,
                playbook=get_resource_path("playbooks", "99.clean.yml"),
                inventory=str(m.clusters_dir / self.AIO_CLUSTER / "hosts"),
                extravars=m._yaml_to_dict(m.clusters_dir / self.AIO_CLUSTER / "config.yml"),
                roles_path=get_resource_path("roles"),
            )
        if run_result.rc != 0:
            logger.error(f"Revert playbook failed (exit code {run_result.rc}).", extra=LOG_STDOUT)
            sys.exit(run_result.rc)
        rmrf(aio_dir)
        logger.info("All-in-one install reverted; cluster directory removed.", extra=LOG_STDOUT)
