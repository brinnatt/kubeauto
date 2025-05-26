import shutil
from typing import Optional

from common.logger import setup_logger
from pathlib import Path
from .docker import DockerManager
from .registry import RegistryManager
from common.constants import KubeConstant

logger = setup_logger(__name__)


class DownloadManager:
    def __init__(self):
        # 通过容器镜像来存储所有的组件，方便管理
        self.docker = DockerManager()
        self.registry = RegistryManager()
        self.kube_constant = KubeConstant()
        self.base_path = Path(self.kube_constant.BASE_PATH)
        self.image_dir = Path(self.kube_constant.IMAGE_DIR)
        self.temp_path = Path(self.kube_constant.TEMP_PATH)
        self.kube_bin_dir = Path(self.kube_constant.KUBE_BIN_DIR)

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

        # ensure base_path exists
        self.base_path.mkdir(parents=True, exist_ok=True)

        # check if kubeauto exists
        if (self.base_path / "roles/kube-node").exists():
            logger.warning("kubeauto already exists")
            return

        # Handle kubeauto image
        image_tar = self.image_dir / f"kubeauto_{version}.tar"
        try:
            if not image_tar.exists():
                logger.info(f"Downloading kubeauto: {version}")
                self.docker.pull_image(f"brinnatt/kubeauto:{version}")
                self.docker.save_image(f"brinnatt/kubeauto:{version}", str(image_tar))
            self.docker.load_image(str(image_tar))
        except Exception as e:
            logger.error(f"Failed to handle kubeauto image: {e}")
            raise

        # Creating temporary container and handle files
        container_id = None
        kubeauto_project = None
        try:
            container_id = self.docker.run_container(
                f"brinnatt/kubeauto:{version}",
                "temp_kubeauto",
            )

            kubeauto_project = self.temp_path / "kubeauto"
            if kubeauto_project.exists():
                shutil.rmtree(kubeauto_project)

            self.docker.copy_from_container(
                "temp_kubeauto",
                str(self.base_path),
                str(self.temp_path)
            )

            for item in kubeauto_project.iterdir():
                shutil.move(str(item), str(self.base_path))

        finally:
            # Clean up temporary container
            if container_id:
                try:
                    self.docker.remove_container("temp_kubeauto")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary container: {e}")

            # Clean up temporary kubeauto project files
            if kubeauto_project and kubeauto_project.exists():
                shutil.rmtree(kubeauto_project)

        logger.info("kubeauto has been installed successfully!")

    def get_k8s_bin(self, version: Optional[str] = None) -> None:
        """Download Kubernetes binaries with caching and error handling"""
        version = version or self.kube_constant.v_k8s_bin

        # ensure kube_bin_dir exists
        self.kube_bin_dir.mkdir(parents=True, exist_ok=True)

        # Check if binaries already exist
        if (self.kube_bin_dir / "kubelet").exists():
            logger.warning("Kubernetes binaries already exist")
            return

        # Initialize variables for cleanup
        container_id = None
        k8s_dir = None

        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"k8s_bin_{version}.tar"
            if not image_tar.exists():
                logger.info(f"Downloading Kubernetes binaries: {version}")
                self.docker.pull_image(f"brinnatt/kubeauto-k8s-bin:{version}")
                self.docker.save_image(f"brinnatt/kubeauto-k8s-bin:{version}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_container(
                f"brinnatt/kubeauto-k8s-bin:{version}",
                "temp_k8s_bin"
            )

            # Create temp directory with cleanup safety
            k8s_dir = self.temp_path / "k8s"
            if k8s_dir.exists():
                shutil.rmtree(k8s_dir)

            # Copy binaries from container
            self.docker.copy_from_container(
                "temp_k8s_bin",
                "/k8s",
                str(self.temp_path)
            )

            # Move binaries to target directory
            for item in k8s_dir.iterdir():
                shutil.move(str(item), str(self.kube_bin_dir))

            # Create k8s bin symbolic to src
            for item in self.kube_bin_dir.iterdir():
                item.symlink_to("/usr/local/bin/")

            logger.info("Kubernetes binaries downloaded successfully")

        except Exception as e:
            logger.error(f"Failed to get Kubernetes binaries: {e}")
            raise

        finally:
            # Cleanup resources
            if container_id:
                try:
                    self.docker.remove_container("temp_k8s_bin")
                except Exception as e:
                    logger.warning(f"Failed to remove container: {e}")

            if k8s_dir and k8s_dir.exists():
                shutil.rmtree(k8s_dir)

    def get_ext_bin(self, version:Optional[str] = None) -> None:
        """Download extra binaries with caching and error handling"""
        version = version or self.kube_constant.v_extra_bin

        # ensure kube_bin_dir exists
        self.kube_bin_dir.mkdir(parents=True, exist_ok=True)

        # Check if binaries already exist
        if (self.kube_bin_dir / "etcdctl").exists():
            logger.warning("Extra binaries already exist")
            return

        # Initialize cleanup variables
        container_id = None
        extra_bin_dir = None

        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"ext_bin_{version}.tar"
            if not image_tar.exists():
                logger.info(f"Downloading extra binaries: {version}")
                self.docker.pull_image(f"brinnatt/kubeauto-ext-bin:{version}")
                self.docker.save_image(f"brinnatt/kubeauto-ext-bin:{version}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_container(
                f"brinnatt/kubeauto-ext-bin:{version}",
                "temp_ext_bin"
            )

            # Create temp directory
            extra_bin_dir = self.temp_path / "extra"
            if extra_bin_dir.exists():
                shutil.rmtree(extra_bin_dir)

            # Copy binaries from container
            self.docker.copy_from_container(
                "temp_ext_bin",
                "/extra",
                str(self.temp_path)
            )

            # Move binaries to target directory
            for item in extra_bin_dir.iterdir():
                shutil.move(str(item), str(self.kube_bin_dir))

            logger.info("Extra binaries downloaded successfully")

        except Exception as e:
            logger.error(f"Failed to get extra binaries: {e}")
            raise

        finally:
            # Cleanup resources
            if container_id:
                try:
                    self.docker.remove_container("temp_ext_bin")
                except Exception as e:
                    logger.warning(f"Container cleanup failed: {e}")

            if extra_bin_dir and extra_bin_dir.exists():
                shutil.rmtree(extra_bin_dir)

    def get_default_images(self) -> None:
        """Download default images and upload to local registry"""
        images = [
            f"calico/cni:{self.kube_constant.v_calico}",
            f"calico/kube-controllers:{self.kube_constant.v_calico}",
            f"calico/node:{self.kube_constant.v_calico}",
            f"coredns/coredns:{self.kube_constant.v_coredns}",
            f"brinnatt/k8s-dns-node-cache:{self.kube_constant.v_dnsnodecache}",
            f"kubernetesui/dashboard:{self.kube_constant.v_dashboard}",
            f"kubernetesui/metrics-scraper:{self.kube_constant.v_dashboardmetricsscraper}",
            f"brinnatt/metrics-server:{self.kube_constant.v_metricsserver}",
            f"brinnatt/pause:{self.kube_constant.v_pause}"
        ]

        try:
            self.registry.upload_to_registry(images)
        except Exception as e:
            logger.error(f"Failed to upload images: {e}")

        logger.info(f"Default images uploaded to registry successfully!")

    def get_harbor_offline_pkg(self, version: Optional[str] = None) -> None:
        """Download Harbor offline installer package with caching and error handling"""
        version = version or self.kube_constant.v_harbor

        # Check if package already exists
        harbor_file = self.image_dir / f"harbor-offline-installer-{version}.tgz"
        if harbor_file.exists():
            logger.warning("Harbor offline installer already exists")
            return

        logger.info(f"Downloading Harbor offline installer: {version}")

        # Initialize variables for cleanup
        container_id = None
        tmp_dir = None
        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"harbor_{version}.tar"
            if not image_tar.exists():
                self.docker.pull_image(f"brinnatt/harbor-offline:{version}")
                self.docker.save_image(f"brinnatt/harbor-offline:{version}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_container(
                f"brinnatt/harbor-offline:{version}",
                "temp_harbor"
            )

            # Create temp directory
            tmp_dir = self.temp_path / "harbor_pkg"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            # Copy package from container to temp directory
            self.docker.copy_from_container(
                "temp_harbor",
                f"/harbor-offline-installer-{version}.tgz",
                str(tmp_dir)
            )

            # Verify and move to final location
            tmp_file = tmp_dir / f"harbor-offline-installer-{version}.tgz"
            if not tmp_file.exists():
                raise FileNotFoundError("Harbor package not found in container")

            # Atomic move to final destination
            if harbor_file.exists():
                harbor_file.unlink()
            tmp_file.rename(harbor_file)

            logger.info("Harbor offline installer downloaded successfully")

        except Exception as e:
            logger.error(f"Failed to get Harbor offline installer: {e}")
            # Clean up potentially incomplete files
            if harbor_file.exists():
                harbor_file.unlink()
            raise

        finally:
            # Cleanup resources
            if container_id:
                try:
                    self.docker.remove_container("temp_harbor")
                except Exception as e:
                    logger.warning(f"Failed to remove harbor container: {e}")

            if tmp_dir and tmp_dir.exists():
                try:
                    shutil.rmtree(tmp_dir)
                except Exception as e:
                    logger.warning(f"Failed to clean harbor temp directory: {e}")
