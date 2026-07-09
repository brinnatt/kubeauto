import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from common.constants import KubeConstant
from common.mirrors import install_ansible_with_system_pm, download_k8s_bins, normalize_k8s_version
from common.exceptions import DownloadError, InstallPrereqError
from common.logger import setup_logger, LOG_STDOUT
from common.utils import rmrf, run_command, ensure_kubeauto_clusters_dir

from .docker import DockerManager
from .registry import RegistryManager

logger = setup_logger(__name__)


class DownloadManager:
    def __init__(self):
        self.docker = DockerManager()
        self.registry = RegistryManager()
        self.kube_constant = KubeConstant()
        self.base_path = Path(self.kube_constant.BASE_PATH)
        self.image_dir = Path(self.kube_constant.IMAGE_DIR)
        self.temp_path = Path(self.kube_constant.TEMP_PATH)
        self.kube_bin_dir = Path(self.kube_constant.KUBE_BIN_DIR)
        self.extra_bin_dir = Path(self.kube_constant.EXTRA_BIN_DIR)
        self.sys_bin_dir = Path(self.kube_constant.SYS_BIN_DIR)

    def download_all(self) -> None:
        """Download all required components"""
        if not self.docker.is_docker_installed:
            self.docker.install_docker()

        self.get_ansible_env()
        self.get_kubeauto()
        self.get_k8s_bin()
        self.get_ext_bin()
        self.registry.start_local_registry()
        self.get_default_images()
        ensure_kubeauto_clusters_dir(self.base_path)
        logger.info(f"Cluster config directory ready: {self.base_path / 'clusters'}", extra=LOG_STDOUT)

    def get_ansible_env(self):
        """
        Install ansible from system package manager.
        Supports: RHEL/CentOS/Rocky, Ubuntu/Debian, SUSE.
        """
        if shutil.which("ansible"):
            logger.info("Ansible already installed; skipping.", extra=LOG_STDOUT)
            return

        logger.info("Installing Ansible (system package manager, Huawei mirror).", extra=LOG_STDOUT)

        try:
            install_ansible_with_system_pm()
            logger.info("Ansible installed.", extra=LOG_STDOUT)

        except Exception as e:
            logger.warning(
                f"Failed to install ansible env: {e}, we suggest you install ansible tools manually and continue!",
                extra=LOG_STDOUT)
            sys.exit(1)

    def get_kubeauto(self, version: Optional[str] = None) -> None:
        """Download and setup kubeauto with full directory backup"""
        if not self.docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is required to pull and extract images. Run 'kubecli download -d' first."
            )
        version = version or self.kube_constant.v_kubeauto

        if self.__check_file_exists(self.base_path, "roles/kube-node"):
            logger.warning("Kubeauto already installed; skipping.", extra=LOG_STDOUT)
            return

        self.__handle_image(self.image_dir, f"kubeauto_{version}.tar", f"brinnatt/kubeauto:{version}")

        self.__handle_files(f"brinnatt/kubeauto:{version}", "/usr/local/kubeauto", self.base_path)

        logger.info("Kubeauto installed.", extra=LOG_STDOUT)

    def get_k8s_bin(self, version: Optional[str] = None) -> None:
        """Download Kubernetes binaries from Huawei Cloud mirrors (no Docker image required)."""
        version = version or self.kube_constant.v_k8s_bin
        target = normalize_k8s_version(version)

        installed = self._installed_k8s_version()
        if installed == target and self.__check_file_exists(self.kube_bin_dir, "kubelet"):
            if (self.sys_bin_dir / "kubelet").is_symlink():
                logger.warning(
                    f"Kubernetes binaries {target} already installed; skipping.",
                    extra=LOG_STDOUT,
                )
                return

        logger.info(
            f"[DOWNLOAD] Kubernetes binaries {target} from Huawei mirrors.",
            extra=LOG_STDOUT,
        )
        try:
            download_k8s_bins(target, self.kube_bin_dir, self.kube_constant.arch)
            for item in self.kube_bin_dir.iterdir():
                if not item.is_file() or not os.access(item, os.X_OK):
                    continue
                target_link = self.sys_bin_dir / item.name
                tmp_link = self.sys_bin_dir / f".{item.name}.new"
                rmrf(tmp_link)
                tmp_link.symlink_to(item)
                tmp_link.replace(target_link)
            logger.info("Kubernetes binaries installed.", extra=LOG_STDOUT)
        except Exception as e:
            logger.error(f"[DOWNLOAD] Failed to download Kubernetes binaries — {e}", extra=LOG_STDOUT)
            raise DownloadError(f"Failed to download Kubernetes binaries: {e}")

    def _installed_k8s_version(self) -> Optional[str]:
        apiserver = self.kube_bin_dir / "kube-apiserver"
        if not apiserver.exists():
            return None
        try:
            out = run_command([str(apiserver), "--version"], capture=True).stdout.strip()
            return out.split()[1]
        except Exception:
            return None

    def get_ext_bin(self, version: Optional[str] = None) -> None:
        """Download extra binaries with caching and error handling"""
        if not self.docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is required to pull and extract images. Run 'kubecli download -d' first."
            )
        version = version or self.kube_constant.v_extra_bin

        if self.__check_file_exists(self.extra_bin_dir, "etcdctl"):
            logger.warning("Extra binaries already installed; skipping.", extra=LOG_STDOUT)
            return

        self.__handle_image(self.image_dir, f"ext_bin_{version}.tar", f"brinnatt/kubeauto-ext-bin:{version}")

        self.__handle_files(f"brinnatt/kubeauto-ext-bin:{version}", "/extra", self.extra_bin_dir, create_symlink=False)

        logger.info("Extra binaries installed.", extra=LOG_STDOUT)

    def get_harbor_offline_pkg(self, version: Optional[str] = None) -> None:
        """Download Harbor offline installer package with caching and error handling"""
        if not self.docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is required to pull and extract images. Run 'kubecli download -d' first."
            )
        version = version or self.kube_constant.v_harbor

        if self.__check_file_exists(self.image_dir, f"harbor-offline-installer-{version}.tgz"):
            logger.warning("Harbor offline installer already exists; skipping.", extra=LOG_STDOUT)
            return

        self.__handle_image(self.image_dir, f"harbor_{version}.tar", f"brinnatt/harbor-offline:{version}")

        self.__handle_files(f"brinnatt/harbor-offline:{version}", "/harbor", self.image_dir)

        logger.info("Harbor offline package installed.", extra=LOG_STDOUT)

    def get_default_images(self) -> None:
        """Download default images and upload to local registry"""
        if not self.docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is required to download and push images. Run 'kubecli download -d' first, or install Docker manually."
            )
        images = [
            f"calico/cni:{self.kube_constant.v_calico}",
            f"calico/kube-controllers:{self.kube_constant.v_calico}",
            f"calico/node:{self.kube_constant.v_calico}",
            f"coredns/coredns:{self.kube_constant.v_coredns}",
            f"brinnatt/k8s-dns-node-cache:{self.kube_constant.v_dnsnodecache}",
            f"brinnatt/metrics-server:{self.kube_constant.v_metricsserver}",
            f"brinnatt/pause:{self.kube_constant.v_pause}"
        ]
        logger.info(f"[DOWNLOAD] Default images: uploading {len(images)} image(s) to local registry.", extra=LOG_STDOUT)
        try:
            self.registry.upload_to_registry(images)
        except Exception as e:
            logger.error(f"[DOWNLOAD] Failed to upload default images — {e}", extra=LOG_STDOUT)
            raise DownloadError(f"Failed to upload images: {e}")
        logger.info(f"[DOWNLOAD] Default images: {len(images)} image(s) uploaded to local registry.", extra=LOG_STDOUT)

    def get_extra_images(self, component: str) -> None:
        """Download extra images for specified component and upload to local registry"""
        if not self.docker.is_docker_installed:
            raise InstallPrereqError(
                "Docker is required to download and push images. Run 'kubecli download -d' first, or install Docker manually."
            )
        if component not in self.kube_constant.component_images:
            logger.error(f"[DOWNLOAD] Invalid component: {component}.", extra=LOG_STDOUT)
            raise DownloadError(f"Invalid component: {component}. Choose from: {list(self.kube_constant.component_images.keys())}.")

        if component == "cilium":
            self._ensure_cilium_helm_chart()

        images = self.kube_constant.component_images[component]
        logger.info(f"[DOWNLOAD] Component {component}: uploading {len(images)} image(s) to local registry.", extra=LOG_STDOUT)
        try:
            self.registry.upload_to_registry(images)
        except Exception as e:
            logger.error(f"[DOWNLOAD] Failed to upload {component} images — {e}", extra=LOG_STDOUT)
            raise DownloadError(f"Failed to upload {component} images: {e}")
        logger.info(f"[DOWNLOAD] Component {component}: {len(images)} image(s) uploaded.", extra=LOG_STDOUT)

    def _ensure_cilium_helm_chart(self) -> None:
        """Pull Cilium Helm chart matching v_cilium into roles/cilium/files/."""
        version = self.kube_constant.v_cilium.lstrip("v")
        chart_name = f"cilium-{version}.tgz"
        for roles_dir in (self.base_path / "roles" / "cilium" / "files", Path("/opt/kubeauto/roles/cilium/files")):
            roles_dir.mkdir(parents=True, exist_ok=True)
            chart_path = roles_dir / chart_name
            if chart_path.exists() and chart_path.stat().st_size > 10000:
                logger.info(f"[DOWNLOAD] Cilium chart already present: {chart_path}", extra=LOG_STDOUT)
                continue
            tmp_dir = self.temp_path / f"cilium-chart-{version}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            helm_bin = self.extra_bin_dir / "helm"
            logger.info(f"[DOWNLOAD] Pulling Cilium Helm chart {version} from quay.io/cilium/charts.", extra=LOG_STDOUT)
            run_command([
                str(helm_bin), "pull", "oci://quay.io/cilium/charts/cilium",
                "--version", version, "-d", str(tmp_dir),
            ])
            pulled = tmp_dir / chart_name
            if not pulled.exists():
                raise DownloadError(f"Helm pull did not produce {chart_name} under {tmp_dir}")
            shutil.copy2(pulled, chart_path)
            logger.info(f"[DOWNLOAD] Cilium chart installed: {chart_path}", extra=LOG_STDOUT)

    def __check_file_exists(self, directory: Path, filename: str) -> bool:
        """Check if file exists"""
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return True
        return False

    def __handle_image(self, directory: Path, image_tar: str, image: str) -> None:
        """Pull/save/load image; use cache if tar exists. Logs each step for traceability."""
        path = directory / image_tar
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if not path.exists():
                logger.info(f"[DOWNLOAD] Image {image}: pulling and saving to cache.", extra=LOG_STDOUT)
                self.docker.pull_image(image)
                self.docker.save_image(image, str(path))
            else:
                logger.info(f"[DOWNLOAD] Image {image}: loading from cache.", extra=LOG_STDOUT)
            self.docker.load_image(str(path))
            logger.info(f"[DOWNLOAD] Image ready: {image}.", extra=LOG_STDOUT)
        except Exception as e:
            logger.error(f"[DOWNLOAD] Failed to process image {image} — {e}", extra=LOG_STDOUT)
            raise DownloadError(f"Failed to pull, save, or load {image}: {e}")

    def __handle_files(self, image: str, image_carrier: str, destination: Path, create_symlink=False) -> None:
        """Handle files"""
        # Creating temporary container and handle files
        container_id = None
        temp_carrier = None
        try:
            image_carrier = Path(image_carrier)
            container_id = self.docker.run_container(image, f"temp_{image_carrier.name}")

            temp_carrier = self.temp_path / f"{image_carrier.name}"
            if temp_carrier.exists():
                shutil.rmtree(str(temp_carrier))

            self.docker.copy_from_container(
                container_id,
                str(image_carrier),
                str(self.temp_path)
            )

            for item in temp_carrier.iterdir():
                dest = destination / item.name
                rmrf(dest)
                shutil.move(str(item), str(destination))

            if create_symlink:
                # recurse destination to find executable binary file and make symlink to /usr/local/bin
                for item in destination.rglob('*'):
                    if item.is_file() and os.access(item, os.X_OK):
                        target_link = self.sys_bin_dir / item.name
                        rmrf(target_link)
                        target_link.symlink_to(item)

        except Exception as e:
            raise DownloadError(f"Failed to copy image files to dest — {e}")

        finally:
            if container_id:
                try:
                    self.docker.remove_container(container_id)
                except Exception as e:
                    logger.warning(f"Failed to remove temporary container: {e}")

            if temp_carrier and temp_carrier.exists():
                rmrf(temp_carrier)
