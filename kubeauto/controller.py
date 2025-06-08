"""
Main cluster operations for kubeauto
"""

from pathlib import Path
from datetime import datetime
from typing import List, Optional
from common.utils import run_command, validate_ip, confirm_action
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
        self.clusters_dir = self.base_path / "clusters"
        self.playbooks_dir = self.base_path / "playbooks"

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
            hosts_content = cluster_hosts.read_text()
            hosts_content = hosts_content.replace("_cluster_name_", name)
            cluster_hosts.write_text(hosts_content)
        except Exception as e:
            raise ClusterNewError(f"Error creating cluster hosts or config: {e}")

        # TODO: Set versions in config.yml as in original script
        logger.info(f"-> Cluster {name} created. Next steps:", extra={"to_stdout": True})
        logger.info(f"1. Configure {cluster_hosts}", extra={"to_stdout": True})
        logger.info(f"2. Configure {cluster_config}", extra={"to_stdout": True})

    def setup_cluster(self, name: str, step: str, extra_args: Optional[list[str]] = None) -> None:
        """
        Set up a cluster with specific step

        name: Cluster name
        step: Setup step (01-07, 10, 11, 90 or step name)
        extra_args: Additional arguments to pass to ansible-playbook
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
            logger.error(f"Invalid setup step: {step}")
            return

        extra_args = extra_args or []

        cmd = [
            "ansible-playbook",
            "-i", str(self.clusters_dir / name / "hosts"),
            "-e", f"@{self.clusters_dir / name / 'config.yml'}",
            *extra_args,
            str(self.playbooks_dir / playbook)
        ]

        logger.info(f"Running command: {' '.join(cmd)}")

        # Show component versions
        self._show_component_versions(name)

        if not confirm_action(f"cluster:{name} setup step:{step} begins"):
            return

        run_command(cmd)

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

        cmd = [
            "ansible-playbook",
            "-i", str(self.clusters_dir / name / "hosts"),
            "-e", f"@{self.clusters_dir / name / 'config.yml'}",
            str(self.playbooks_dir / playbook)
        ]

        logger.info(f"Running command: {' '.join(cmd)}")

        if not confirm_action(f"cluster:{name} {command} begins"):
            return

        run_command(cmd)

    def checkout_cluster(self, name: str) -> None:
        """Switch to a cluster's kubeconfig"""
        self._validate_cluster(name)

        kubeconfig = self.clusters_dir / name / "kubectl.kubeconfig"
        if not kubeconfig.exists():
            raise ClusterNotFoundError(f"Invalid kubeconfig, run 'setup {name}' first")

        dest_config = Path.home() / ".kube/config"
        dest_config.parent.mkdir(exist_ok=True)

        run_command(["cp", "-f", str(kubeconfig), str(dest_config)])
        logger.info(f"Set default kubeconfig: cluster {name} (current)")

    def start_aio_cluster(self) -> None:
        """Start an all-in-one cluster with default settings"""
        from common.utils import get_host_ip, setup_ssh_keys

        host_ip = get_host_ip()
        logger.info(f"Using host IP: {host_ip}")

        setup_ssh_keys()

        # Create cluster
        self.new_cluster("default")

        # Copy all-in-one hosts file
        aio_hosts = self.base_path / "example/hosts.allinone"
        cluster_hosts = self.clusters_dir / "default/hosts"
        cluster_hosts.write_text(aio_hosts.read_text())

        # Update hosts file with actual IP
        hosts_content = cluster_hosts.read_text()
        hosts_content = hosts_content.replace("192.168.1.1", host_ip)
        cluster_hosts.write_text(hosts_content)

        # Setup cluster
        self.setup_cluster("default", "all")

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

        cmd = [
            "ansible-playbook",
            "-i", str(hosts_file),
            "-e", f"NODE_TO_ADD={ip}",
            "-e", f"@{self.clusters_dir / cluster / 'config.yml'}",
            str(self.playbooks_dir / playbook)
        ]

        logger.info(f"Adding {role} node {ip} to cluster {cluster}")
        run_command(cmd)

        # Additional steps for master nodes
        if role == "master":
            logger.info("Reconfiguring and restarting load balancers")
            self._restart_load_balancers(cluster)

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

        cmd = [
            "ansible-playbook",
            "-i", str(hosts_file),
            "-e", f"NODE_TO_DEL={ip}",
            "-e", f"CLUSTER={cluster}",
            "-e", f"@{self.clusters_dir / cluster / 'config.yml'}",
            str(self.playbooks_dir / playbook)
        ]

        logger.info(f"Removing {role} node {ip} from cluster {cluster}")
        run_command(cmd)

        # Additional steps for master nodes
        if role == "master":
            logger.info("Reconfiguring kubeconfig and load balancers")
            self._reconfigure_kubeconfig(cluster)
            self._restart_load_balancers(cluster)

        # Remove node from hosts file
        self._remove_from_hosts_section(hosts_file, role, ip)

    def renew_ca_certs(self, cluster: str) -> None:
        """Force renew CA certificates and all other certs in the cluster"""
        self._validate_cluster(cluster)

        logger.warning("WARNING: This will recreate CA certs and all other certs in the cluster",
                       extra={"to_stdout": True})
        logger.warning("Only use this if the admin.conf has been compromised", extra={"to_stdout": True})

        if not confirm_action(f"Renew all certs in cluster {cluster}"):
            return

        cmd = [
            "ansible-playbook",
            "-i", str(self.clusters_dir / cluster / "hosts"),
            "-e", f"@{self.clusters_dir / cluster / 'config.yml'}",
            "-e", "CHANGE_CA=true",
            str(self.playbooks_dir / "96.update-certs.yml"),
            "-t", "force_change_certs"
        ]

        run_command(cmd)

    def kubeconfig_admin(self, cluster: str, action: str, user_name: str = None,
                         user_type: str = "admin", expiry: str = "4800h") -> None:
        """Manage kubeconfig users"""
        self._validate_cluster(cluster)

        if action == "add":
            if not user_name:
                user_name = f"user-{datetime.now().strftime('%Y%m%d%H%M')}"

            cmd = [
                "ansible-playbook",
                "-i", str(self.clusters_dir / cluster / "hosts"),
                "-e", f"@{self.clusters_dir / cluster / 'config.yml'}",
                "-e", f"CUSTOM_EXPIRY={expiry}",
                "-e", f"USER_TYPE={user_type}",
                "-e", f"USER_NAME={user_name}",
                "-e", "ADD_KCFG=true",
                "-t", "add-kcfg",
                str(self.base_path / "roles/deploy/deploy.yml")
            ]

            logger.info(f"Adding user {user_name} ({user_type}) to cluster {cluster}")
            run_command(cmd)

        elif action == "delete":
            if not user_name:
                raise ValueError("User name is required for delete action")

            # Get cluster role binding
            kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"
            crb_cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "get", "clusterrolebindings",
                "-ojsonpath=\"{.items[?(@.subjects[0].name == '{user_name}')].metadata.name}\""
            ]

            crb = run_command(crb_cmd, shell=True).stdout.strip('"')

            if crb:
                # Delete cluster role binding
                delete_cmd = [
                    str(self.kube_bin_dir / "kubectl"),
                    "--kubeconfig", str(kubeconfig),
                    "delete", "clusterrolebindings", crb
                ]
                run_command(delete_cmd)

            # Remove user certs
            user_certs = self.clusters_dir / cluster / "ssl/users" / f"{user_name}*"
            run_command(["rm", "-f", str(user_certs)], shell=True)
            logger.info(f"Deleted user {user_name} from cluster {cluster}")

        elif action == "list":
            kubeconfig = self.clusters_dir / cluster / "kubectl.kubeconfig"

            # Get all users
            admins_cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "get", "clusterrolebindings",
                "-ojsonpath='{.items[?(@.roleRef.name == \"cluster-admin\")].subjects[*].name}'"
            ]
            admins = run_command(admins_cmd, shell=True).stdout.strip("'").split()

            views_cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "get", "clusterrolebindings",
                "-ojsonpath='{.items[?(@.roleRef.name == \"view\")].subjects[*].name}'"
            ]
            views = run_command(views_cmd, shell=True).stdout.strip("'").split()

            all_cmd = [
                str(self.kube_bin_dir / "kubectl"),
                "--kubeconfig", str(kubeconfig),
                "get", "clusterrolebindings",
                "-ojsonpath='{.items[*].subjects[*].name}'"
            ]
            all_users = run_command(all_cmd, shell=True).stdout.strip("'").split()

            # Print user list
            print("\n%-30s %-15s %-20s" % ("USER", "TYPE", "EXPIRY(+8h if in Asia/Shanghai)"))
            print("---------------------------------------------------------------------------------")

            for user in admins:
                if user.endswith("-" + datetime.now().strftime('%Y%m%d%H%M')):
                    cert_file = self.clusters_dir / cluster / "ssl/users" / f"{user}.pem"
                    if cert_file.exists():
                        expiry_cmd = [
                            str(self.kube_bin_dir / "cfssl-certinfo"),
                            "-cert", str(cert_file),
                            "|", "grep", "not_after", "|", "awk", "'{print $2}'", "|",
                            "sed", "'s/\"//g'", "|", "sed", "'s/,//g'"
                        ]
                        expiry = run_command(expiry_cmd, shell=True).stdout.strip()
                        print("%-30s %-15s %-20s" % (user, "cluster-admin", expiry))

            for user in views:
                if user.endswith("-" + datetime.now().strftime('%Y%m%d%H%M')):
                    cert_file = self.clusters_dir / cluster / "ssl/users" / f"{user}.pem"
                    if cert_file.exists():
                        expiry_cmd = [
                            str(self.kube_bin_dir / "cfssl-certinfo"),
                            "-cert", str(cert_file),
                            "|", "grep", "not_after", "|", "awk", "'{print $2}'", "|",
                            "sed", "'s/\"//g'", "|", "sed", "'s/,//g'"
                        ]
                        expiry = run_command(expiry_cmd, shell=True).stdout.strip()
                        print("%-30s %-15s %-20s" % (user, "view", expiry))

            for user in all_users:
                if user.endswith("-" + datetime.now().strftime('%Y%m%d%H%M')):
                    if user not in admins and user not in views:
                        cert_file = self.clusters_dir / cluster / "ssl/users" / f"{user}.pem"
                        if cert_file.exists():
                            expiry_cmd = [
                                str(self.kube_bin_dir / "cfssl-certinfo"),
                                "-cert", str(cert_file),
                                "|", "grep", "not_after", "|", "awk", "'{print $2}'", "|",
                                "sed", "'s/\"//g'", "|", "sed", "'s/,//g'"
                            ]
                            expiry = run_command(expiry_cmd, shell=True).stdout.strip()
                            print("%-30s %-15s %-20s" % (user, "unknown", expiry))
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
        """Check if node already exists in hosts file"""
        section_start = f"[kube_{role}]" if role != "etcd" else "[etcd]"
        section_end = f"[kube_{'node' if role == 'master' else 'master'}]" if role != "etcd" else "[kube_master]"

        in_section = False
        with hosts_file.open() as f:
            for line in f:
                line = line.strip()
                if line.startswith(section_start):
                    in_section = True
                elif line.startswith(section_end):
                    in_section = False
                elif in_section and (line.startswith(ip) or f" {ip} " in line):
                    raise NodeExistsError(f"Node {ip} already exists in {role} section")

    def _check_node_not_exists(self, hosts_file: Path, ip: str, role: str) -> None:
        """Check if node doesn't exist in hosts file"""
        section_start = f"[kube_{role}]" if role != "etcd" else "[etcd]"
        section_end = f"[kube_{'node' if role == 'master' else 'master'}]" if role != "etcd" else "[kube_master]"

        in_section = False
        found = False
        with hosts_file.open() as f:
            for line in f:
                line = line.strip()
                if line.startswith(section_start):
                    in_section = True
                elif line.startswith(section_end):
                    in_section = False
                elif in_section and (line.startswith(ip) or f" {ip} " in line):
                    found = True
                    break

        if not found:
            raise NodeNotFoundError(f"Node {ip} not found in {role} section")

    def _add_to_hosts_section(self, hosts_file: Path, role: str, line: str) -> None:
        """Add a line to a specific section in hosts file"""
        section = f"[kube_{role}]" if role != "etcd" else "[etcd]"

        content = hosts_file.read_text().splitlines()
        section_line = -1

        for i, l in enumerate(content):
            if l.strip() == section:
                section_line = i
                break

        if section_line == -1:
            raise ValueError(f"Section {section} not found in hosts file")

        # Insert after the section header
        content.insert(section_line + 1, line)
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

    def _restart_load_balancers(self, cluster: str) -> None:
        """Restart kube-lb and ex-lb services"""
        hosts_file = self.clusters_dir / cluster / "hosts"
        config_file = self.clusters_dir / cluster / "config.yml"

        # Restart kube-lb
        cmd = [
            "ansible-playbook",
            "-i", str(hosts_file),
            "-e", f"@{config_file}",
            "-t", "restart_kube-lb",
            str(self.playbooks_dir / "90.setup.yml")
        ]
        run_command(cmd)

        # Restart ex-lb
        cmd = [
            "ansible-playbook",
            "-i", str(hosts_file),
            "-e", f"@{config_file}",
            "-t", "restart_lb",
            str(self.playbooks_dir / "10.ex-lb.yml")
        ]
        run_command(cmd)

    def _reconfigure_kubeconfig(self, cluster: str) -> None:
        """Reconfigure kubeconfig after master node removal"""
        hosts_file = self.clusters_dir / cluster / "hosts"
        config_file = self.clusters_dir / cluster / "config.yml"

        cmd = [
            "ansible-playbook",
            "-i", str(hosts_file),
            "-e", f"@{config_file}",
            "-t", "create_kctl_cfg",
            str(self.base_path / "roles/deploy/deploy.yml")
        ]
        run_command(cmd)

    def _show_component_versions(self, cluster: str) -> None:
        """Show component versions before setup"""
        v_kube = run_command([str(self.kube_bin_dir / "kube-apiserver"), "--version"]).stdout.split()[1]
        v_etcd = "v" + run_command([str(self.kube_bin_dir / "etcd"), "--version"]).stdout.split()[2]

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
