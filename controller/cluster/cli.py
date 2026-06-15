"""
Command line interface for kubeauto
"""
import argparse
import sys
import re
from pathlib import Path
from typing import Dict, Callable, List
from taskflow.patterns import linear_flow
from taskflow import engines
from common.utils import confirm_action, validate_ip, expand_host_targets, parse_pw_file_hosts
from common.exceptions import KubeautoError, DownloadError, DockerManageError, SystemExecutionError, InstallPrereqError
from common.logger import setup_logger, LOG_STDOUT
from common.constants import KubeConstant
from common.os import SystemProbe

# Commands that take a cluster name as first argument (for completion)
_CLUSTER_COMMANDS = {
    "new", "setup", "list", "checkout", "start", "stop", "upgrade",
    "backup", "restore", "destroy", "add-etcd", "add-master", "add-node",
    "del-etcd", "del-master", "del-node", "kca-renew", "kcfg-adm",
}
# Setup step values (for setup <cluster> <step> completion)
_SETUP_STEPS = [
    "01", "02", "03", "04", "05", "06", "07", "90", "10", "11",
    "prepare", "etcd", "container-runtime", "kube-master", "kube-node",
    "network", "cluster-addon", "all", "ex-lb", "harbor",
]

_BASH_COMPLETION_SCRIPT = r'''# Bash completion for kubecli (supports: python3 kubecli.py / kubecli / kubecli.py)
# Usage: source <(python3 kubecli.py completion bash)   or add to .bashrc
_kubeauto_completion() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  # Invoked as: python3 kubecli.py [args]  -> complete kubecli subcommands/args
  if [[ "${{COMP_WORDS[0]}}" == "python3" ]] && [[ $COMP_CWORD -ge 1 ]] && [[ "${{COMP_WORDS[1]}}" == *"kubecli"* ]]; then
    local cword=$((COMP_CWORD - 2))
    [[ $cword -lt 0 ]] && cword=0
    local words=("${{COMP_WORDS[@]:2}}")
    COMPREPLY=($(compgen -W "$("${{COMP_WORDS[0]}}" "${{COMP_WORDS[1]}}" __complete "$cword" "${{words[@]}}" 2>/dev/null)" -- "$cur"))
    return
  fi
  # Invoked as: kubecli [args] or kubecli.py [args] (direct)
  if [[ "${{COMP_WORDS[0]}}" == *"kubecli"* ]]; then
    local cword=$((COMP_CWORD - 1))
    local words=("${{COMP_WORDS[@]:1}}")
    COMPREPLY=($(compgen -W "$("${{COMP_WORDS[0]}}" __complete "$cword" "${{words[@]}}" 2>/dev/null)" -- "$cur"))
    return
  fi
  COMPREPLY=()
}}
complete -F _kubeauto_completion python3
complete -F _kubeauto_completion {prog}
complete -F _kubeauto_completion kubecli
'''

_ZSH_COMPLETION_SCRIPT = r'''# Zsh completion for {prog}
# Usage: source <({prog} completion zsh)   or add to .zshrc
_kubeauto_completion() {{
  local -a w
  w=("${{(@)words[2,-1]}}")
  local cword=$((CURRENT - 2))
  [[ $cword -lt 0 ]] && cword=0
  local -a reply
  reply=($({prog} __complete "$cword" "${{w[@]}}" 2>/dev/null))
  _describe 'values' reply
}}
compdef _kubeauto_completion {prog}
'''
from service.cluster.manager import ClusterManager, SetupAIO
from service.cluster.downloader import DownloadManager
from service.cluster.docker import DockerManager

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

        # System commands
        self._setup_system_command()

        # Version (no side effects)
        self._setup_version_command()

        # Completion (output script only, no side effects)
        self._setup_completion_command()

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
            help="Add an etcd node to the cluster",
            epilog="""
Examples:
  # Add etcd on a host that is already master/node (nodename optional)
  kubecli add-etcd mycluster 192.168.1.2

  # Add standalone etcd node (nodename required)
  kubecli add-etcd mycluster 192.168.1.10 etcd-01
"""
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
            help="k8s_nodename for standalone etcd (required if etcd is not on a master/node host); optional if same host as master/node"
        )

    def _setup_add_master_command(self) -> None:
        """Setup 'add-master' command"""
        parser = self.subparsers.add_parser(
            "add-master",
            help="Add a master node to the cluster",
            epilog="""
Examples:
  kubecli add-master mycluster 192.168.1.2 master-02
  kubecli add-master prod 10.0.1.5 master-01
"""
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
            help="k8s_nodename for this master (required): lowercase alphanumeric, '-' or '.', start/end with alphanumeric (e.g. master-02)"
        )

    def _setup_add_node_command(self) -> None:
        """Setup 'add-node' command"""
        parser = self.subparsers.add_parser(
            "add-node",
            help="Add a worker node to the cluster",
            epilog="""
Examples:
  kubecli add-node mycluster 192.168.1.5 worker-01
  kubecli add-node prod 10.0.2.10 worker-02
"""
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
            help="k8s_nodename for this node (required): lowercase alphanumeric, '-' or '.', start/end with alphanumeric (e.g. worker-01)"
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
            help="Manage kubeconfig users for the cluster",
            epilog=(
                "Examples:\n"
                "  # Add a new admin user (default expiry 4800h)\n"
                "  kcfg-adm -A -u alice -t admin mycluster\n\n"
                "  # Add a new view-only user with 24h expiry\n"
                "  kcfg-adm -A -u bob -t view -e 24h mycluster\n\n"
                "  # Delete an existing user\n"
                "  kcfg-adm -D -u alice mycluster\n\n"
                "  # List all bound users in the cluster\n"
                "  kcfg-adm -L mycluster\n\n"
                "  # List all users from ssl/users directory\n"
                "  kcfg-adm -L --all mycluster\n\n"
                "  # List only expired users\n"
                "  kcfg-adm -L --expired mycluster\n"
            ),
            formatter_class=argparse.RawTextHelpFormatter
        )
        self._add_common_cluster_args(parser)

        action_group = parser.add_mutually_exclusive_group(required=True)
        action_group.add_argument("-A", "--add", action="store_true", help="Add a new user")
        action_group.add_argument("-D", "--delete", action="store_true", help="Delete an existing user")
        action_group.add_argument("-L", "--list", action="store_true", help="List users")

        parser.add_argument("-e", "--expiry", default="4800h", help="Certificate expiry time (e.g. 24h, 4800h)")
        parser.add_argument("-t", "--type", choices=["admin", "view"], default="admin", help="Type of user to create")
        parser.add_argument("-u", "--user", help="Name of the user (required for delete)")
        parser.add_argument("--all", action="store_true", help="List all users from ssl/users directory")
        parser.add_argument("--expired", action="store_true", help="Only show expired users")

        def validate_args(args):
            if not re.match(r"^[1-9][0-9]*h$", args.expiry):
                parser.error("'-e/--expiry' must be in format like '24h', '4800h', etc.")
            if args.delete and not args.user:
                parser.error("'-u/--user' is required when using '-D/--delete'.")
            if args.list and args.user:
                logger.warning("Note: '-u' is ignored when listing users.", extra=LOG_STDOUT)

        parser.set_defaults(validate=validate_args)

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
            "-a", "--ansible",
            action="store_true",
            help="Download Ansible (default: distro)"
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
            "-e", "--remove-exited",
            action="store_true",
            help="Remove all exited containers"
        )

    def _setup_system_command(self) -> None:
        parser = self.subparsers.add_parser(
            "system",
            help="Manage system environments",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog="""
Examples:
  # 1. Key-only (best practice)
  kubeauto system -a --user root host1 host2

  # 2. Uniform password (NOT recommended)
  kubeauto system -a --user root --password 'pass' host1 host2

  # 3. Interactive per-host
  kubeauto system -a --user root --ask-pass host1 host2

  # 4. Group passwords via JSON file (enterprise)
  kubeauto system -a --user root --pw-file ./pw.json host1 host2 host3

  # 5. IPv4 last-octet range
  kubeauto system -a --user root --password 'pass' 192.168.139.129-134

  Password file format (pw.json):
  {
    "host1": "pass1",
    "host2": "pass2",
    "prod_group": ["host3", "host4"],
    "prod_group_password": "prod_pass"
  }
  Hosts not listed fall back to --password or key-only.
"""
        )
        ssh_parser = parser.add_argument_group("SSH Key Distribution")
        ssh_parser.add_argument(
            "-a", "--ssh-key-distribute",
            action="store_true",
            help="Distribute SSH public key to hosts"
        )
        ssh_parser.add_argument(
            "--user",
            default="root",
            help="SSH username (default: root)"
        )
        ssh_parser.add_argument(
            "--password",
            help="Uniform password for all hosts (insecure)"
        )
        ssh_parser.add_argument(
            "--pw-file",
            metavar="FILE",
            help="JSON file for per-host/group passwords (recommended for enterprise)"
        )
        ssh_parser.add_argument(
            "--ask-pass",
            action="store_true",
            help="Prompt interactively per host (main thread only)"
        )
        ssh_parser.add_argument(
            "--port",
            type=int,
            default=22,
            help="SSH port (default: 22)"
        )
        ssh_parser.add_argument(
            "hosts",
            nargs="*",
            help="Target host IPs/names (supports IPv4 last-octet range, e.g. 192.168.139.129-134)"
        )
        ssh_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done"
        )
        ssh_parser.add_argument(
            "--workers",
            type=int,
            default=10,
            help="Max concurrent workers (default: 10)"
        )

        probe_group = parser.add_argument_group("System Probes")
        probe_group.add_argument(
            "-b", "--disk-usage",
            action="store_true",
            help="Probe disk usage"
        )
        probe_group.add_argument(
            "-c", "--system-load",
            action="store_true",
            help="Probe CPU/memory/swap"
        )
        probe_group.add_argument(
            "-d", "--network-usage",
            action="store_true",
            help="Probe network interfaces"
        )

    def _setup_version_command(self) -> None:
        """Setup 'version' command: print kubeauto version (same as v_kubeauto)."""
        self.subparsers.add_parser(
            "version",
            help="Show kubeauto version"
        )

    def _setup_completion_command(self) -> None:
        """Setup 'completion' command: print shell completion script (bash/zsh)."""
        parser = self.subparsers.add_parser(
            "completion",
            help="Output shell completion script (source it to enable Tab completion)"
        )
        sub = parser.add_subparsers(dest="shell", required=True, metavar="SHELL")
        sub.add_parser("bash", help="Bash completion script")
        sub.add_parser("zsh", help="Zsh completion script")

    def _get_subcommand_names(self) -> List[str]:
        """Return list of top-level subcommand names from parser (for completion)."""
        for action in self.parser._actions:
            if hasattr(action, "choices") and action.choices:
                return sorted(action.choices.keys())
        return []

    def _get_cluster_names(self) -> List[str]:
        """Return cluster names from clusters_dir (read-only, no business logic). Sorted for stable order."""
        clusters_dir = Path(self.kube_constant.BASE_PATH) / "clusters"
        if not clusters_dir.is_dir():
            return []
        return sorted(p.name for p in clusters_dir.iterdir() if p.is_dir())

    def _do_completion(self, argv_after_complete: List[str]) -> None:
        """Print completions one per line for shell. No side effects, read-only.
        argv_after_complete: [cword, word0, word1, ...] where cword is the index
        of the word being completed (0-based), rest are the words from the command line.
        """
        if len(argv_after_complete) < 1:
            return
        try:
            cword = int(argv_after_complete[0])
        except (ValueError, IndexError):
            return
        tokens = argv_after_complete[1:]
        prefix = (tokens[cword] or "") if cword < len(tokens) else ""

        def filter_prefix(candidates: List[str], p: str) -> List[str]:
            if not p:
                return candidates
            return [c for c in candidates if c.startswith(p)]

        subcommands = self._get_subcommand_names()
        out: List[str] = []

        if cword == 0:
            out = filter_prefix(subcommands, prefix)
        elif cword == 1 and tokens and tokens[0] in _CLUSTER_COMMANDS:
            out = filter_prefix(self._get_cluster_names(), prefix)
        elif cword == 2 and len(tokens) >= 2 and tokens[0] == "setup":
            out = filter_prefix(_SETUP_STEPS, prefix)
        else:
            out = []

        for s in out:
            print(s)

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
            "docker": self._handle_docker,

            # System commands
            "system": self._handle_system,

            # Version
            "version": self._handle_version,

            # Completion
            "completion": self._handle_completion
        }

        handler = command_handlers.get(args.command)
        if handler:
            handler(args)
        else:
            self.parser.print_help()
            sys.exit(1)

    def _handle_version(self, args: argparse.Namespace) -> None:
        """Print kubeauto version (from v_kubeauto), no side effects."""
        logger.info(self.kube_constant.v_kubeauto, extra=LOG_STDOUT)

    def _handle_completion(self, args: argparse.Namespace) -> None:
        """Print shell completion script (bash or zsh). No side effects."""
        prog = self.parser.prog or "kubecli"
        if args.shell == "bash":
            print(_BASH_COMPLETION_SCRIPT.format(prog=prog))
        else:
            print(_ZSH_COMPLETION_SCRIPT.format(prog=prog))

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
        logger.info("Managed clusters:", extra=LOG_STDOUT)
        for i, cluster in enumerate(clusters, 1):
            prefix = "* -> " if cluster == current else "  -> "
            logger.info(f"{prefix}{i}: {cluster}", extra=LOG_STDOUT)

    def _handle_checkout(self, args: argparse.Namespace) -> None:
        """Handle 'checkout' command"""
        cm = ClusterManager()
        cm.checkout_cluster(args.cluster)

    def _handle_start_aio(self, args: argparse.Namespace) -> None:
        """Handle 'start-aio' command"""
        flow = linear_flow.Flow("linear").add(
            SetupAIO(name="setup_aio")
        )
        engine = engines.load(flow)
        engine.run()

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
        if hasattr(args, "validate"):
            args.validate(args)

        cm = ClusterManager()
        if args.add:
            cm.kubeconfig_admin(args.cluster, "add", args.user, args.type, args.expiry)
        elif args.delete:
            cm.kubeconfig_admin(args.cluster, "delete", args.user)
        elif args.list:
            cm.kubeconfig_admin(args.cluster, "list", show_all=args.all, expired_only=args.expired)

    def _handle_download(self, args: argparse.Namespace) -> None:
        """Handle download command with version enforcement"""
        dm = DownloadManager()

        # required at least one argument
        if not any([args.all, args.docker, args.ansible, args.k8s_bin, args.ext_bin, args.kubeauto, args.harbor,
                    args.default_images, args.ext_images]):
            self.subparsers.choices["download"].print_help()
            raise DownloadError("Download command requires at least one argument")

        # handle param conflict manually
        if args.all and any([args.docker, args.ansible, args.k8s_bin, args.ext_bin, args.kubeauto, args.harbor,
                             args.default_images, args.ext_images]):
            self.subparsers.choices["download"].print_help()
            raise DownloadError("Download option --all/-D cannot be used with other download options")

        if args.all:
            dm.download_all()
        else:
            if args.docker:
                if self.docker.is_docker_installed:
                    logger.warning(
                        "Docker has been installed, if you want to install another version, "
                        "please confirm to uninstall the old version, not uninstalling may cause docker conflicts!",
                        extra=LOG_STDOUT
                    )
                    if not self.docker.clean_docker_env():
                        logger.warning("You have cancelled cleaning docker environment, "
                                       "abort all installations, "
                                       "please check and try again.")
                        return

                self.docker.install_docker(args.docker)

            if args.ansible:
                dm.get_ansible_env()

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
        if not any([args.set_proxy, args.del_proxy, args.no_proxy, args.remove, args.remove_all, args.remove_exited]):
            self.subparsers.choices["docker"].print_help()
            raise DockerManageError("Docker command requires at least one argument")

        if not docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is not installed or not running. Run 'kubecli download -d' to install, then retry."
            )

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

        if args.remove_exited:
            docker.clean_exited_containers()

    def _handle_system(self, args: argparse.Namespace) -> None:
        """Handle 'system' command"""
        system = SystemProbe()
        if not any([args.ssh_key_distribute, args.disk_usage, args.system_load, args.network_usage]):
            self.subparsers.choices["system"].print_help()
            raise SystemExecutionError("System command requires at least one argument")
        if args.ssh_key_distribute and any([args.disk_usage, args.network_usage, args.system_load]):
            self.subparsers.choices["system"].print_help()
            raise SystemExecutionError("option -a/--ssh-key-distribute cannot be used with other system options")
        if args.ssh_key_distribute:
            if not any([args.password, args.pw_file, args.ask_pass]):
                self.subparsers.choices["system"].print_help()
                raise SystemExecutionError(
                    "option -a/--ssh-key-distribute requires at least one of: --password, --pw-file, --ask-pass"
                )
            target_hosts_set = set()

            # Step 1: Extract all hosts from --pw-file (if provided)
            if args.pw_file:
                import json
                try:
                    with open(args.pw_file, 'r') as f:
                        pw_data = json.load(f)
                except Exception as e:
                    raise SystemExecutionError(f"Failed to load --pw-file '{args.pw_file}': {e}")

                try:
                    target_hosts_set.update(parse_pw_file_hosts(pw_data))
                except ValueError as e:
                    raise SystemExecutionError(str(e)) from e

            # Step 2: Add CLI hosts (only if not already in pw_file)
            try:
                cli_hosts = expand_host_targets(args.hosts) if args.hosts else []
            except ValueError as e:
                raise SystemExecutionError(str(e)) from e
            dup_hosts = set(cli_hosts) & target_hosts_set
            if dup_hosts:
                logger.warning(
                    f"Duplicate hosts: {sorted(dup_hosts)}, password from --pw-file takes precedence",
                    extra=LOG_STDOUT,
                )
            target_hosts_set.update(cli_hosts)

            # Step 3: Validate at least one host
            if not target_hosts_set:
                raise SystemExecutionError("No hosts to distribute keys to. "
                                           "Please specify hosts via --pw-file or positional arguments.")

            target_hosts = sorted(target_hosts_set)
            for h in target_hosts:
                if not validate_ip(h):
                    raise SystemExecutionError(f"Invalid IP: {h}")

            # Step 4: Call distribution (pass original args.pw_file so internal logic resolves passwords correctly)
            results = system.ssh_keys_distribution(
                host_ips=target_hosts,
                username=args.user,
                password=args.password,
                pw_file=args.pw_file,
                port=args.port,
                ask_pass=args.ask_pass,
                dry_run=args.dry_run,
                max_workers=args.workers,
            )
            for host, result in results.items():
                logger.info(f"{host}: {result}", extra=LOG_STDOUT)

        if args.disk_usage:
            disks = list(system.disk_usage())
            header = f"{'Device':<18} {'Mount':<15} {'Total(GB)':<10} {'Used(GB)':<10} {'Free(GB)':<10} {'Use%':<6}"
            logger.info("Disk Usage:", extra=LOG_STDOUT)
            logger.info("-" * len(header), extra=LOG_STDOUT)
            logger.info(header, extra=LOG_STDOUT)
            logger.info("-" * len(header), extra=LOG_STDOUT)
            for disk in disks:
                logger.info(
                    f"{disk['device']:<18} {disk['mount']:<15} "
                    f"{disk['total_gb']:<10.2f} {disk['used_gb']:<10.2f} "
                    f"{disk['free_gb']:<10.2f} {disk['usage_percent']:<6.1f}",
                    extra=LOG_STDOUT
                )

        if args.system_load:
            resources = system.hardware_resources()
            logger.info("System Resources:", extra=LOG_STDOUT)
            logger.info(f"CPU Cores: {resources['cpu_cores']} (Threads: {resources['cpu_threads']})",
                        extra=LOG_STDOUT)
            logger.info(f"CPU Usage: {resources['cpu_usage_percent']:.1f}%", extra=LOG_STDOUT)
            logger.info(
                f"Memory: {resources['memory_available_gb']:.1f}/{resources['memory_total_gb']:.1f} GB ({resources['memory_usage_percent']:.1f}%)",
                extra=LOG_STDOUT)
            logger.info(f"Swap: {resources['swap_used_gb']:.1f}/{resources['swap_total_gb']:.1f} GB",
                        extra=LOG_STDOUT)

        if args.network_usage:
            interfaces = list(system.network_interfaces())
            logger.info("Network Interfaces:", extra=LOG_STDOUT)
            for intf in interfaces:
                logger.info(f"Interface: {intf['interface']}", extra=LOG_STDOUT)
                for family, addr in intf['addresses'].items():
                    logger.info(f"  {family}: {addr}", extra=LOG_STDOUT)
                logger.info(
                    f"  Traffic: ↑ {intf['traffic_mb']['sent']:.2f} MB | ↓ {intf['traffic_mb']['recv']:.2f} MB",
                    extra=LOG_STDOUT
                )

    def run(self) -> None:
        """Run the CLI application"""
        # Completion hook: no parse_args, no business logic (minimal invasiveness)
        if len(sys.argv) >= 2 and sys.argv[1] == "__complete":
            self._do_completion(sys.argv[2:])
            sys.exit(0)

        args = self.parser.parse_args()

        try:
            self._execute_command(args)
        except KubeautoError as e:
            logger.error(str(e), extra=LOG_STDOUT)
            sys.exit(1)
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", extra=LOG_STDOUT)
            sys.exit(1)
