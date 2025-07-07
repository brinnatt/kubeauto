import shutil, os
from typing import Optional

from common.exceptions import DownloadError
from common.utils import rmrf

from common.logger import setup_logger
from pathlib import Path
from .docker import DockerManager
from .registry import RegistryManager
from common.constants import KubeConstant

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

        self.get_kubeauto()
        self.get_k8s_bin()
        self.get_ext_bin()
        self.registry.start_local_registry()
        self.get_default_images()

    def get_kubeauto(self, version: Optional[str] = None) -> None:
        """Download and setup kubeauto with full directory backup"""
        version = version or self.kube_constant.v_kubeauto

        if self.__check_file_exists(self.base_path, "roles/kube-node"):
            logger.warning("kubeauto already exists", extra={"to_stdout": True})
            return

        self.__handle_image(self.image_dir, f"kubeauto_{version}.tar", f"brinnatt/kubeauto:{version}")

        self.__handle_files(f"brinnatt/kubeauto:{version}", "/usr/local/kubeauto", self.base_path)

        logger.info("kubeauto has been installed successfully!", extra={'to_stdout': True})

    def get_k8s_bin(self, version: Optional[str] = None) -> None:
        """Download Kubernetes binaries with caching and error handling"""
        version = version or self.kube_constant.v_k8s_bin

        if self.__check_file_exists(self.kube_bin_dir, "kubelet"):
            logger.warning("Kubernetes binaries already exist", extra={"to_stdout": True})
            return

        self.__handle_image(self.image_dir, f"k8s_bin_{version}.tar", f"brinnatt/kubeauto-k8s-bin:{version}")

        self.__handle_files(f"brinnatt/kubeauto-k8s-bin:{version}", "/k8s", self.kube_bin_dir, create_symlink=False)

        logger.info("k8s_bin has been installed successfully!", extra={'to_stdout': True})

    def get_ext_bin(self, version: Optional[str] = None) -> None:
        """Download extra binaries with caching and error handling"""
        version = version or self.kube_constant.v_extra_bin

        if self.__check_file_exists(self.extra_bin_dir, "etcdctl"):
            logger.warning("Extra binaries already exist", extra={"to_stdout": True})
            return

        self.__handle_image(self.image_dir, f"ext_bin_{version}.tar", f"brinnatt/kubeauto-ext-bin:{version}")

        self.__handle_files(f"brinnatt/kubeauto-ext-bin:{version}", "/extra", self.extra_bin_dir, create_symlink=False)

        logger.info("ext_bin has been installed successfully!", extra={'to_stdout': True})

    def get_harbor_offline_pkg(self, version: Optional[str] = None) -> None:
        """Download Harbor offline installer package with caching and error handling"""
        version = version or self.kube_constant.v_harbor

        if self.__check_file_exists(self.image_dir, f"harbor-offline-installer-{version}.tgz"):
            logger.warning("Harbor offline installer already exist", extra={"to_stdout": True})
            return

        self.__handle_image(self.image_dir, f"harbor_{version}.tar", f"brinnatt/harbor-offline:{version}")

        self.__handle_files(f"brinnatt/harbor-offline:{version}", "/harbor", self.image_dir)

        logger.info("harbor_offline_pkg has been installed successfully!", extra={'to_stdout': True})

    def get_default_images(self) -> None:
        """Download default images and upload to local registry"""
        images = [
            f"calico/cni:{self.kube_constant.v_calico}",
            f"calico/kube-controllers:{self.kube_constant.v_calico}",
            f"calico/node:{self.kube_constant.v_calico}",
            f"coredns/coredns:{self.kube_constant.v_coredns}",
            f"brinnatt/k8s-dns-node-cache:{self.kube_constant.v_dnsnodecache}",
            f"brinnatt/metrics-server:{self.kube_constant.v_metricsserver}",
            f"brinnatt/pause:{self.kube_constant.v_pause}"
        ]

        try:
            self.registry.upload_to_registry(images)
        except Exception as e:
            raise DownloadError(f"Failed to upload images: {e}")

        logger.info(f"Default images uploaded to registry successfully!", extra={'to_stdout': True})

    def get_extra_images(self, component: str) -> None:
        """Download extra images for specified component and upload to local registry"""
        if component not in self.kube_constant.component_images:
            logger.error(f"Invalid component: {component}")
            return

        logger.info(f"Downloading images for {component}, then uploading to local registry")

        try:
            self.registry.upload_to_registry(self.kube_constant.component_images[component])
            logger.info(f"{component} images uploaded to registry successfully!")
        except Exception as e:
            raise DownloadError(f"Failed to upload {component} images: {e}")

    def __check_file_exists(self, directory: Path, filename: str) -> bool:
        """Check if file exists"""
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return True
        return False

    def __handle_image(self, directory: Path, image_tar: str, image: str) -> None:
        """Check if image exists"""
        path = directory / image_tar
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if not path.exists():
                logger.info(f"Downloading {image}")
                self.docker.pull_image(f"{image}")
                self.docker.save_image(f"{image}", str(path))
            self.docker.load_image(str(path))
        except Exception as e:
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
            raise DownloadError(f"Failed to copy image files to dest: {e}")

        finally:
            if container_id:
                try:
                    self.docker.remove_container(container_id)
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary container: {e}")

            if temp_carrier and temp_carrier.exists():
                rmrf(temp_carrier)
