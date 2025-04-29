"""
Command line interface for kubeauto
"""
import argparse
import sys
from common.utils import confirm_action
from common.exceptions import KubeautoError
from common.logger import setup_logger
from common.constants import KubeConstant
from .controller import ClusterManager
from .downloader import DownloadManager
from .docker import DockerManager

logger = setup_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Kubeauto - Kubernetes cluster management tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Cluster setup commands
    setup_parser = subparsers.add_parser("setup", help="Setup a cluster")
    setup_parser.add_argument("cluster", help="Cluster name")
    setup_parser.add_argument("step", help="Setup step (01-07, 10, 11, 90 or name)")
    setup_parser.add_argument("extra_args", nargs=argparse.REMAINDER, help="Extra arguments for ansible")

    new_parser = subparsers.add_parser("new", help="Create a new cluster configuration")
    new_parser.add_argument("cluster", help="Cluster name")

    list_parser = subparsers.add_parser("list", help="List all clusters")

    checkout_parser = subparsers.add_parser("checkout", help="Switch to a cluster's kubeconfig")
    checkout_parser.add_argument("cluster", help="Cluster name")

    start_aio_parser = subparsers.add_parser("start-aio", help="Start an all-in-one cluster")

    # Cluster operation commands
    start_parser = subparsers.add_parser("start", help="Start cluster services")
    start_parser.add_argument("cluster", help="Cluster name")

    stop_parser = subparsers.add_parser("stop", help="Stop cluster services")
    stop_parser.add_argument("cluster", help="Cluster name")

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade cluster")
    upgrade_parser.add_argument("cluster", help="Cluster name")

    backup_parser = subparsers.add_parser("backup", help="Backup cluster state")
    backup_parser.add_argument("cluster", help="Cluster name")

    restore_parser = subparsers.add_parser("restore", help="Restore cluster from backup")
    restore_parser.add_argument("cluster", help="Cluster name")

    destroy_parser = subparsers.add_parser("destroy", help="Destroy cluster")
    destroy_parser.add_argument("cluster", help="Cluster name")

    # Node operation commands
    add_etcd_parser = subparsers.add_parser("add-etcd", help="Add etcd node")
    add_etcd_parser.add_argument("cluster", help="Cluster name")
    add_etcd_parser.add_argument("ip", help="Node IP address")
    add_etcd_parser.add_argument("extra_info", nargs="?", default="", help="Extra node information")

    add_master_parser = subparsers.add_parser("add-master", help="Add master node")
    add_master_parser.add_argument("cluster", help="Cluster name")
    add_master_parser.add_argument("ip", help="Node IP address")
    add_master_parser.add_argument("extra_info", nargs="?", default="", help="Extra node information")

    add_node_parser = subparsers.add_parser("add-node", help="Add worker node")
    add_node_parser.add_argument("cluster", help="Cluster name")
    add_node_parser.add_argument("ip", help="Node IP address")
    add_node_parser.add_argument("extra_info", nargs="?", default="", help="Extra node information")

    del_etcd_parser = subparsers.add_parser("del-etcd", help="Remove etcd node")
    del_etcd_parser.add_argument("cluster", help="Cluster name")
    del_etcd_parser.add_argument("ip", help="Node IP address")

    del_master_parser = subparsers.add_parser("del-master", help="Remove master node")
    del_master_parser.add_argument("cluster", help="Cluster name")
    del_master_parser.add_argument("ip", help="Node IP address")

    del_node_parser = subparsers.add_parser("del-node", help="Remove worker node")
    del_node_parser.add_argument("cluster", help="Cluster name")
    del_node_parser.add_argument("ip", help="Node IP address")

    # Extra commands
    renew_ca_parser = subparsers.add_parser("kca-renew", help="Renew CA certificates")
    renew_ca_parser.add_argument("cluster", help="Cluster name")

    kcfg_parser = subparsers.add_parser("kcfg-adm", help="Manage kubeconfig users")
    kcfg_parser.add_argument("cluster", help="Cluster name")
    kcfg_group = kcfg_parser.add_mutually_exclusive_group(required=True)
    kcfg_group.add_argument("-A", "--add", action="store_true", help="Add a user")
    kcfg_group.add_argument("-D", "--delete", action="store_true", help="Delete a user")
    kcfg_group.add_argument("-L", "--list", action="store_true", help="List users")
    kcfg_parser.add_argument("-e", "--expiry", default="4800h", help="Certificate expiry time")
    kcfg_parser.add_argument("-t", "--type", choices=["admin", "view"], default="admin", help="User type")
    kcfg_parser.add_argument("-u", "--user", help="User name")

    # Download commands
    kube_constant = KubeConstant()
    download_parser = subparsers.add_parser("download", help="Download components")
    download_parser.add_argument("-D", "--all", action="store_true", help="Download all components")
    download_parser.add_argument("-R", "--harbor", action="store_true", help="Download Harbor offline installer")
    download_parser.add_argument("-X", "--extra-image", help="Download extra images")
    download_parser.add_argument("-d", "--v-docker", default=kube_constant.v_docker, help="Docker version")
    download_parser.add_argument("-e", "--v-ext-bin", default=kube_constant.v_extra_bin, help="Extra binaries version")
    download_parser.add_argument("-k", "--v-k8s-bin", default=kube_constant.v_k8s_bin, help="Kubernetes binaries version")
    download_parser.add_argument("-m", "--registry-mirror", help="Registry mirror")
    download_parser.add_argument("-z", "--v-kubeauto", default=kube_constant.v_kubeauto, help="Kubeauto version")

    # Docker commands
    docker_parser = subparsers.add_parser("docker", help="Docker operations")
    docker_parser.add_argument("-C", "--clean", action="store_true", help="Clean all containers")

    args = parser.parse_args()

    try:
        if args.command in ["setup", "new", "list", "checkout", "start-aio",
                            "start", "stop", "upgrade", "backup", "restore", "destroy",
                            "add-etcd", "add-master", "add-node",
                            "del-etcd", "del-master", "del-node",
                            "kca-renew", "kcfg-adm"]:
            cm = ClusterManager()

            if args.command == "setup":
                cm.setup_cluster(args.cluster, args.step, args.extra_args)
            elif args.command == "new":
                cm.new_cluster(args.cluster)
            elif args.command == "list":
                clusters = cm.list_clusters()
                current = cm.get_current_cluster()
                print("Managed clusters:")
                for i, cluster in enumerate(clusters, 1):
                    prefix = "==> " if cluster == current else "    "
                    print(f"{prefix}{i}: {cluster}")
            elif args.command == "checkout":
                cm.checkout_cluster(args.cluster)
            elif args.command == "start-aio":
                cm.start_aio_cluster()
            elif args.command == "start":
                cm.cluster_command(args.cluster, "start")
            elif args.command == "stop":
                cm.cluster_command(args.cluster, "stop")
            elif args.command == "upgrade":
                cm.cluster_command(args.cluster, "upgrade")
            elif args.command == "backup":
                cm.cluster_command(args.cluster, "backup")
            elif args.command == "restore":
                cm.cluster_command(args.cluster, "restore")
            elif args.command == "destroy":
                cm.cluster_command(args.cluster, "destroy")
            elif args.command == "add-etcd":
                cm.add_node(args.cluster, args.ip, "etcd", args.extra_info)
            elif args.command == "add-master":
                cm.add_node(args.cluster, args.ip, "master", args.extra_info)
            elif args.command == "add-node":
                cm.add_node(args.cluster, args.ip, "node", args.extra_info)
            elif args.command == "del-etcd":
                cm.remove_node(args.cluster, args.ip, "etcd")
            elif args.command == "del-master":
                cm.remove_node(args.cluster, args.ip, "master")
            elif args.command == "del-node":
                cm.remove_node(args.cluster, args.ip, "node")
            elif args.command == "kca-renew":
                cm.renew_ca_certs(args.cluster)
            elif args.command == "kcfg-adm":
                if args.add:
                    cm.kubeconfig_admin(args.cluster, "add", args.user, args.type, args.expiry)
                elif args.delete:
                    if not args.user:
                        parser.error("User name is required for delete action")
                    cm.kubeconfig_admin(args.cluster, "delete", args.user)
                elif args.list:
                    cm.kubeconfig_admin(args.cluster, "list")

        elif args.command == "download":
            dm = DownloadManager()
            if args.all:
                dm.download_all()
            elif args.harbor:
                dm.get_harbor_offline_pkg()
            elif args.extra_image:
                dm.get_default_images()

        elif args.command == "docker":
            docker = DockerManager()
            if args.clean:
                if confirm_action("Clean all running containers"):
                    docker.clean_containers()

    except KubeautoError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
