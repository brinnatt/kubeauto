"""
Command line interface for kubeauto
"""
import argparse
import sys
from typing import Dict, Callable

from common.utils import confirm_action
from common.exceptions import KubeautoError, DownloadError, DockerManageError
from common.logger import setup_logger
from common.constants import KubeConstant
from .controller import ClusterManager
from .downloader import DownloadManager
from .docker import DockerManager

logger = setup_logger(__name__)


class KubeautoCLI:
    """Main CLI application class"""

    def __init__(self):
        self.docker = DockerManager()
        self.parser = argparse.ArgumentParser(
            description="Kubeauto - Kubernetes cluster management tool",
            formatter_class=argparse.RawTextHelpFormatter
        )
        self.subparsers = self.parser.add_subparsers(
            dest="command",
            required=True,
            title="available commands",
            metavar="COMMAND"
        )
        self.kube_constant = KubeConstant()
        self._setup_commands()

    def _setup_commands(self) -> None:
        """Initialize all CLI commands"""
        # Cluster setup commands
        self._setup_new_command()
        self._setup_setup_command()
        self._setup_list_command()
        self._setup_checkout_command()
        self._setup_start_aio_command()

        # Cluster operation commands
        self._setup_start_command()
        self._setup_stop_command()
        self._setup_upgrade_command()
        self._setup_backup_command()
        self._setup_restore_command()
        self._setup_destroy_command()

        # Node operation commands
        self._setup_add_etcd_command()
        self._setup_add_master_command()
        self._setup_add_node_command()
        self._setup_del_etcd_command()
        self._setup_del_master_command()
        self._setup_del_node_command()

        # Extra commands
        self._setup_kca_renew_command()
        self._setup_kcfg_adm_command()

        # Download commands
        self._setup_download_command()

        # Docker commands
        self._setup_docker_command()

    def _add_common_cluster_args(self, parser: argparse.ArgumentParser) -> None:
        """Add common cluster arguments to a parser"""
        parser.add_argument(
            "cluster",
            help="Name of the cluster to operate on"
        )

    def _setup_new_command(self) -> None:
        """
        Create a new cluster configuration
        """
        parser = self.subparsers.add_parser(
            "new",
            help="Create a new cluster configuration"
        )
        self._add_common_cluster_args(parser)

    def _setup_setup_command(self) -> None:
        """Setup 'setup' command"""
        parser = self.subparsers.add_parser(
            "setup",
            help="Setup a cluster with specific step"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "step",
            help="""Setup step:
  01/prepare       Prepare CA/certs & system settings
  02/etcd          Setup etcd cluster
  03/runtime       Setup container runtime
  04/kube-master   Setup master nodes
  05/kube-node     Setup worker nodes
  06/network       Setup network plugin
  07/cluster-addon Setup cluster addons
  90/all           Run all setup steps
  10/ex-lb         Install external load balancer
  11/harbor        Install Harbor registry"""
        )
        parser.add_argument(
            "extra_args",
            nargs=argparse.REMAINDER,
            help="Extra arguments to pass to ansible-playbook"
        )

    def _setup_list_command(self) -> None:
        """Setup 'list' command"""
        self.subparsers.add_parser(
            "list",
            help="List all managed clusters"
        )

    def _setup_checkout_command(self) -> None:
        """Setup 'checkout' command"""
        parser = self.subparsers.add_parser(
            "checkout",
            help="Switch to a cluster's kubeconfig"
        )
        self._add_common_cluster_args(parser)

    def _setup_start_aio_command(self) -> None:
        """Setup 'start-aio' command"""
        self.subparsers.add_parser(
            "start-aio",
            help="Quickly setup an all-in-one cluster with default settings"
        )

    def _setup_start_command(self) -> None:
        """Setup 'start' command"""
        parser = self.subparsers.add_parser(
            "start",
            help="Start all cluster services"
        )
        self._add_common_cluster_args(parser)

    def _setup_stop_command(self) -> None:
        """Setup 'stop' command"""
        parser = self.subparsers.add_parser(
            "stop",
            help="Stop all cluster services"
        )
        self._add_common_cluster_args(parser)

    def _setup_upgrade_command(self) -> None:
        """Setup 'upgrade' command"""
        parser = self.subparsers.add_parser(
            "upgrade",
            help="Upgrade the cluster components"
        )
        self._add_common_cluster_args(parser)

    def _setup_backup_command(self) -> None:
        """Setup 'backup' command"""
        parser = self.subparsers.add_parser(
            "backup",
            help="Backup cluster state (etcd snapshot)"
        )
        self._add_common_cluster_args(parser)

    def _setup_restore_command(self) -> None:
        """Setup 'restore' command"""
        parser = self.subparsers.add_parser(
            "restore",
            help="Restore cluster from backup"
        )
        self._add_common_cluster_args(parser)

    def _setup_destroy_command(self) -> None:
        """Setup 'destroy' command"""
        parser = self.subparsers.add_parser(
            "destroy",
            help="Destroy the cluster"
        )
        self._add_common_cluster_args(parser)

    def _setup_add_etcd_command(self) -> None:
        """Setup 'add-etcd' command"""
        parser = self.subparsers.add_parser(
            "add-etcd",
            help="Add an etcd node to the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the new etcd node"
        )
        parser.add_argument(
            "extra_info",
            nargs="?",
            default="",
            help="Additional node information (optional)"
        )

    def _setup_add_master_command(self) -> None:
        """Setup 'add-master' command"""
        parser = self.subparsers.add_parser(
            "add-master",
            help="Add a master node to the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the new master node"
        )
        parser.add_argument(
            "extra_info",
            nargs="?",
            default="",
            help="Additional node information (optional)"
        )

    def _setup_add_node_command(self) -> None:
        """Setup 'add-node' command"""
        parser = self.subparsers.add_parser(
            "add-node",
            help="Add a worker node to the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the new worker node"
        )
        parser.add_argument(
            "extra_info",
            nargs="?",
            default="",
            help="Additional node information (optional)"
        )

    def _setup_del_etcd_command(self) -> None:
        """Setup 'del-etcd' command"""
        parser = self.subparsers.add_parser(
            "del-etcd",
            help="Remove an etcd node from the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the etcd node to remove"
        )

    def _setup_del_master_command(self) -> None:
        """Setup 'del-master' command"""
        parser = self.subparsers.add_parser(
            "del-master",
            help="Remove a master node from the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the master node to remove"
        )

    def _setup_del_node_command(self) -> None:
        """Setup 'del-node' command"""
        parser = self.subparsers.add_parser(
            "del-node",
            help="Remove a worker node from the cluster"
        )
        self._add_common_cluster_args(parser)
        parser.add_argument(
            "ip",
            help="IP address of the worker node to remove"
        )

    def _setup_kca_renew_command(self) -> None:
        """Setup 'kca-renew' command"""
        parser = self.subparsers.add_parser(
            "kca-renew",
            help="Force renew CA certificates and all other certs"
        )
        self._add_common_cluster_args(parser)

    def _setup_kcfg_adm_command(self) -> None:
        """Setup 'kcfg-adm' command"""
        parser = self.subparsers.add_parser(
            "kcfg-adm",
            help="Manage kubeconfig users for the cluster"
        )
        self._add_common_cluster_args(parser)

        action_group = parser.add_mutually_exclusive_group(required=True)
        action_group.add_argument(
            "-A", "--add",
            action="store_true",
            help="Add a new user"
        )
        action_group.add_argument(
            "-D", "--delete",
            action="store_true",
            help="Delete an existing user"
        )
        action_group.add_argument(
            "-L", "--list",
            action="store_true",
            help="List all users"
        )

        parser.add_argument(
            "-e", "--expiry",
            default="4800h",
            help="Certificate expiry time (e.g. 24h, 4800h)"
        )
        parser.add_argument(
            "-t", "--type",
            choices=["admin", "view"],
            default="admin",
            help="Type of user to create"
        )
        parser.add_argument(
            "-u", "--user",
            help="Name of the user (required for add/delete)"
        )

    def _setup_download_command(self) -> None:
        """Setup 'download' command with strict version control"""
        parser = self.subparsers.add_parser(
            "download",
            help="Download required components with version control"
        )

        parser.add_argument(
            "-D", "--all",
            action="store_true",
            help="Download ALL components with DEFAULT versions: "
                 f"Docker({self.kube_constant.v_docker}), "
                 f"K8s({self.kube_constant.v_k8s_bin}), "
                 f"Extra({self.kube_constant.v_extra_bin}), "
                 f"Kubeauto({self.kube_constant.v_kubeauto})"
        )

        component_group = parser.add_argument_group("component options")
        component_group.add_argument(
            "-d", "--docker",
            metavar="VERSION",
            nargs='?',
            const=self.kube_constant.v_docker,
            help=f"Download Docker (default: {self.kube_constant.v_docker})"
        )
        component_group.add_argument(
            "-k", "--k8s-bin",
            metavar="VERSION",
            nargs='?',
            const=self.kube_constant.v_k8s_bin,
            help=f"Download Kubernetes binaries (default: {self.kube_constant.v_k8s_bin})"
        )
        component_group.add_argument(
            "-e", "--ext-bin",
            metavar="VERSION",
            nargs='?',
            const=self.kube_constant.v_extra_bin,
            help=f"Download extra binaries (default: {self.kube_constant.v_extra_bin})"
        )
        component_group.add_argument(
            "-z", "--kubeauto",
            metavar="VERSION",
            nargs='?',
            const=self.kube_constant.v_kubeauto,
            help=f"Download Kubeauto (default: {self.kube_constant.v_kubeauto})"
        )
        component_group.add_argument(
            "-R", "--harbor",
            metavar="VERSION",
            nargs='?',
            const=self.kube_constant.v_harbor,
            help=f"Download Harbor offline installer (default: {self.kube_constant.v_harbor})"
        )
        component_group.add_argument(
            "-X", "--default-images",
            action="store_true",
            help="Download extra multiple container images (default versions)"
        )

        component_group.add_argument(
            "-E", "--ext-images",
            metavar="COMPONENT",
            help="Download specific extra component (required specific component)"
        )

    def _setup_docker_command(self) -> None:
        """Setup 'docker' command"""
        parser = self.subparsers.add_parser(
            "docker",
            help="Manage Docker containers"
        )
        parser.add_argument(
            "-f", "--force",
            action="store_true",
            help="Force to execute command with other options"
        )

        proxy_group = parser.add_argument_group("proxy options")
        proxy_group.add_argument(
            "-a", "--set-proxy",
            nargs=2,
            metavar=("HOST", "PORT"),
            help="Configure Docker proxy (provide HOST PORT to set)"
        )
        proxy_group.add_argument(
            "-b", "--del-proxy",
            action="store_true",
            help="Delete Docker proxy (clean configuration file)"
        )
        proxy_group.add_argument(
            "-c", "--no-proxy",
            nargs="+",
            metavar="HOST",
            help="Additional no-proxy hosts"
        )

        docker_container_group = parser.add_argument_group("docker container management options")
        docker_container_group.add_argument(
            "-d", "--remove",
            metavar="CONTAINER",
            help="Remove a specific container"
        )
        docker_container_group.add_argument(
            "-D", "--remove-all",
            action="store_true",
            help="Remove all containers including running containers"
        )
        docker_container_group.add_argument(
            "-e", "--remove-existed",
            action="store_true",
            help="Remove all existed containers"
        )

    def _execute_command(self, args: argparse.Namespace) -> None:
        """Execute the appropriate command based on parsed arguments"""
        command_handlers: Dict[str, Callable[[argparse.Namespace], None]] = {
            # Cluster setup commands
            "new": self._handle_new,
            "setup": self._handle_setup,
            "list": self._handle_list,
            "checkout": self._handle_checkout,
            "start-aio": self._handle_start_aio,

            # Cluster operation commands
            "start": self._handle_start,
            "stop": self._handle_stop,
            "upgrade": self._handle_upgrade,
            "backup": self._handle_backup,
            "restore": self._handle_restore,
            "destroy": self._handle_destroy,

            # Node operation commands
            "add-etcd": self._handle_add_etcd,
            "add-master": self._handle_add_master,
            "add-node": self._handle_add_node,
            "del-etcd": self._handle_del_etcd,
            "del-master": self._handle_del_master,
            "del-node": self._handle_del_node,

            # Extra commands
            "kca-renew": self._handle_kca_renew,
            "kcfg-adm": self._handle_kcfg_adm,

            # Download commands
            "download": self._handle_download,

            # Docker commands
            "docker": self._handle_docker
        }

        handler = command_handlers.get(args.command)
        if handler:
            handler(args)
        else:
            self.parser.print_help()
            sys.exit(1)

    def _handle_new(self, args: argparse.Namespace) -> None:
        """Handle 'new' command"""
        cm = ClusterManager()
        cm.new_cluster(args.cluster)

    def _handle_setup(self, args: argparse.Namespace) -> None:
        """Handle 'setup' command"""
        cm = ClusterManager()
        cm.setup_cluster(args.cluster, args.step, args.extra_args)

    def _handle_list(self, args: argparse.Namespace) -> None:
        """Handle 'list' command"""
        cm = ClusterManager()
        clusters = cm.list_clusters()
        current = cm.get_current_cluster()
        logger.info("Managed clusters:", extra={"to_stdout": True})
        for i, cluster in enumerate(clusters, 1):
            prefix = "==> " if cluster == current else "    "
            logger.info(f"{prefix}{i}: {cluster}", extra={"to_stdout": True})

    def _handle_checkout(self, args: argparse.Namespace) -> None:
        """Handle 'checkout' command"""
        cm = ClusterManager()
        cm.checkout_cluster(args.cluster)

    def _handle_start_aio(self, args: argparse.Namespace) -> None:
        """Handle 'start-aio' command"""
        cm = ClusterManager()
        cm.start_aio_cluster()

    def _handle_start(self, args: argparse.Namespace) -> None:
        """Handle 'start' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "start")

    def _handle_stop(self, args: argparse.Namespace) -> None:
        """Handle 'stop' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "stop")

    def _handle_upgrade(self, args: argparse.Namespace) -> None:
        """Handle 'upgrade' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "upgrade")

    def _handle_backup(self, args: argparse.Namespace) -> None:
        """Handle 'backup' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "backup")

    def _handle_restore(self, args: argparse.Namespace) -> None:
        """Handle 'restore' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "restore")

    def _handle_destroy(self, args: argparse.Namespace) -> None:
        """Handle 'destroy' command"""
        cm = ClusterManager()
        cm.cluster_command(args.cluster, "destroy")

    def _handle_add_etcd(self, args: argparse.Namespace) -> None:
        """Handle 'add-etcd' command"""
        cm = ClusterManager()
        cm.add_node(args.cluster, args.ip, "etcd", args.extra_info)

    def _handle_add_master(self, args: argparse.Namespace) -> None:
        """Handle 'add-master' command"""
        cm = ClusterManager()
        cm.add_node(args.cluster, args.ip, "master", args.extra_info)

    def _handle_add_node(self, args: argparse.Namespace) -> None:
        """Handle 'add-node' command"""
        cm = ClusterManager()
        cm.add_node(args.cluster, args.ip, "node", args.extra_info)

    def _handle_del_etcd(self, args: argparse.Namespace) -> None:
        """Handle 'del-etcd' command"""
        cm = ClusterManager()
        cm.remove_node(args.cluster, args.ip, "etcd")

    def _handle_del_master(self, args: argparse.Namespace) -> None:
        """Handle 'del-master' command"""
        cm = ClusterManager()
        cm.remove_node(args.cluster, args.ip, "master")

    def _handle_del_node(self, args: argparse.Namespace) -> None:
        """Handle 'del-node' command"""
        cm = ClusterManager()
        cm.remove_node(args.cluster, args.ip, "node")

    def _handle_kca_renew(self, args: argparse.Namespace) -> None:
        """Handle 'kca-renew' command"""
        cm = ClusterManager()
        cm.renew_ca_certs(args.cluster)

    def _handle_kcfg_adm(self, args: argparse.Namespace) -> None:
        """Handle 'kcfg-adm' command"""
        cm = ClusterManager()
        if args.add:
            cm.kubeconfig_admin(args.cluster, "add", args.user, args.type, args.expiry)
        elif args.delete:
            if not args.user:
                self.parser.error("User name is required for delete action")
            cm.kubeconfig_admin(args.cluster, "delete", args.user)
        elif args.list:
            cm.kubeconfig_admin(args.cluster, "list")

    def _handle_download(self, args: argparse.Namespace) -> None:
        """Handle download command with version enforcement"""
        dm = DownloadManager()

        # required at least one argument
        if not any([args.all, args.docker, args.k8s_bin, args.ext_bin, args.kubeauto, args.harbor,
                             args.default_images, args.ext_images]):
            self.subparsers.choices["download"].print_help()
            raise DownloadError("Download command requires at least one argument")

        # handle param conflict manually
        if args.all and any([args.docker, args.k8s_bin, args.ext_bin, args.kubeauto, args.harbor,
                             args.default_images, args.ext_images]):
            self.subparsers.choices["download"].print_help()
            raise DownloadError("Download option --all/-D cannot be used with other download options")

        if args.all:
            dm.download_all()
        else:
            if args.docker:
                if self.docker.is_docker_installed:
                    logger.info("Docker has been installed, you don't have to install once again")
                    return

                self.docker.install_docker(args.docker)

            if args.k8s_bin:
                dm.get_k8s_bin(args.k8s_bin)

            if args.ext_bin:
                dm.get_ext_bin(args.ext_bin)

            if args.kubeauto:
                dm.get_kubeauto(args.kubeauto)

            if args.harbor:
                dm.get_harbor_offline_pkg(args.harbor)

            if args.default_images:
                dm.get_default_images()

            if args.ext_images:
                dm.get_extra_images(args.ext_images)

    def _handle_docker(self, args: argparse.Namespace) -> None:
        """Handle 'docker' command"""
        docker = DockerManager()

        # required at least one argument
        if not any([args.set_proxy, args.del_proxy, args.no_proxy, args.remove, args.remove_all, args.remove_existed]):
            self.subparsers.choices["docker"].print_help()
            raise DockerManageError("Docker command requires at least one argument")

        if args.set_proxy:
            docker.set_docker_proxy(args.set_proxy[0], args.set_proxy[1])
        elif args.del_proxy:
            docker.unset_docker_proxy()
        elif args.no_proxy and not args.set_proxy:
            self.subparsers.choices["docker"].print_help()
            raise DockerManageError("--no-proxy requires --set-proxy to be specified")

        if args.remove:
            docker.remove_container(args.remove)

        if args.remove_all:
            if confirm_action("Clean all containers including running containers with --force"):
                docker.clean_all_containers(force=args.force)

        if args.remove_existed:
            docker.clean_exited_containers()

    def run(self) -> None:
        """Run the CLI application"""
        args = self.parser.parse_args()

        try:
            self._execute_command(args)
        except KubeautoError as e:
            logger.error(str(e), extra={'to_stdout': True})
            sys.exit(1)
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", extra={'to_stdout': True})
            sys.exit(1)
