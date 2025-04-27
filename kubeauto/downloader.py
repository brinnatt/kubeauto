import shutil
from common.logger import setup_logger
from pathlib import Path
from .docker import DockerManager
from .registry import RegistryManager
from common.constants import KubeVersion

logger = setup_logger(__name__)


class DownloadManager:
    def __init__(self):
        self.docker = DockerManager()
        self.registry = RegistryManager()
        self.kube_version = KubeVersion()
        self.base_path = Path(self.kube_version.BASE_PATH)
        self.image_dir = Path(self.kube_version.IMAGE_DIR)
        self.temp_path = Path(self.kube_version.TEMP_PATH)
        self.kube_bin_dir = Path(self.kube_version.KUBE_BIN_DIR)

    def download_all(self) -> None:
        """Download all required components"""
        # Create directories
        (self.base_path / "down").mkdir(exist_ok=True)
        self.kube_bin_dir.mkdir(exist_ok=True)

        # Download components
        self.docker.install_docker(self.kube_version.v_docker)
        self.get_kubeauto()
        self.get_k8s_bin()
        self.get_ext_bin()
        self.registry.start_local_registry()
        self.get_default_images()

    def get_kubeauto(self) -> None:
        """Download and setup kubeauto with full directory backup"""
        # check if kubeauto exists
        if (self.base_path / "roles/kube-node").exists():
            logger.warning("kubeauto already exists")
            return

        # Handle kubeauto image
        image_tar = self.image_dir / f"kubeauto_{self.kube_version.v_kubeauto}.tar"
        try:
            if not image_tar.exists():
                logger.info(f"Downloading kubeauto: {self.kube_version.v_kubeauto}")
                self.docker.pull_image(f"brinnatt/kubeauto:{self.kube_version.v_kubeauto}")
                self.docker.save_image(f"brinnatt/kubeauto:{self.kube_version.v_kubeauto}", str(image_tar))
            self.docker.load_image(str(image_tar))
        except Exception as e:
            logger.error(f"Failed to handle kubeauto image: {e}")
            raise

        # Creating temporary container and handle files
        container_id = None
        backup_dir = None
        try:
            container_id = self.docker.run_temp_container(
                f"brinnatt/kubeauto:{self.kube_version.v_kubeauto}",
                "temp_kubeauto",
            )

            # Creating temporary backup directory
            backup_dir = self.temp_path / "kubeauto"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            backup_dir.mkdir(exist_ok=True, parents=True)

            # Backup all original files
            for item in self.base_path.iterdir():
                shutil.move(str(item), str(backup_dir / item.name))

            try:
                # Rebuild all base_path files from container
                self.docker.copy_from_container(
                    "temp_kubeauto",
                    str(self.base_path),
                    str(self.base_path.parent)
                )
            except Exception:
                # If exception occurs, copy back original files
                for item in backup_dir.iterdir():
                    shutil.move(str(item), str(self.base_path / item.name))
                raise

            # Merge backup files, not override new files
            for item in backup_dir.iterdir():
                dest = self.base_path / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))

        finally:
            # Clean up temporary container
            if container_id:
                try:
                    self.docker.remove_container("temp_kubeauto")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary container: {e}")

            # Clean up temporary backup files
            if backup_dir and backup_dir.exists():
                shutil.rmtree(backup_dir)

        logger.info("kubeauto has been installed successfully!")

    def get_k8s_bin(self) -> None:
        """Download Kubernetes binaries with caching and error handling"""
        # Check if binaries already exist
        if (self.kube_bin_dir / "kubelet").exists():
            logger.warning("Kubernetes binaries already exist")
            return

        # Initialize variables for cleanup
        container_id = None
        tmp_dir = None

        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"k8s_bin_{self.kube_version.v_k8s_bin}.tar"
            if not image_tar.exists():
                logger.info(f"Downloading Kubernetes binaries: {self.kube_version.v_k8s_bin}")
                self.docker.pull_image(f"brinnatt/kubeauto-k8s-bin:{self.kube_version.v_k8s_bin}")
                self.docker.save_image(f"brinnatt/kubeauto-k8s-bin:{self.kube_version.v_k8s_bin}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_temp_container(
                f"brinnatt/kubeauto-k8s-bin:{self.kube_version.v_k8s_bin}",
                "temp_k8s_bin"
            )

            # Create temp directory with cleanup safety
            tmp_dir = self.temp_path / "k8s_bin"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(exist_ok=True, parents=True)

            # Copy binaries from container
            self.docker.copy_from_container(
                "temp_k8s_bin",
                "/k8s",
                str(tmp_dir)
            )

            # Move binaries to target directory
            for item in tmp_dir.iterdir():
                dest = self.kube_bin_dir / item.name
                if dest.exists():
                    dest.unlink()  # Remove existing files if any
                item.rename(dest)

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

            if tmp_dir and tmp_dir.exists():
                try:
                    # Remove remaining files if any
                    for item in tmp_dir.iterdir():
                        item.unlink()
                    tmp_dir.rmdir()
                except Exception as e:
                    logger.warning(f"Failed to clean temp directory: {e}")

    def get_ext_bin(self) -> None:
        """Download extra binaries with caching and error handling"""
        # Check if binaries already exist
        if (self.kube_bin_dir / "etcdctl").exists():
            logger.warning("Extra binaries already exist")
            return

        # Initialize cleanup variables
        container_id = None
        tmp_dir = None

        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"ext_bin_{self.kube_version.v_extra_bin}.tar"
            if not image_tar.exists():
                logger.info(f"Downloading extra binaries: {self.kube_version.v_extra_bin}")
                self.docker.pull_image(f"brinnatt/kubeauto-ext-bin:{self.kube_version.v_extra_bin}")
                self.docker.save_image(f"brinnatt/kubeauto-ext-bin:{self.kube_version.v_extra_bin}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_temp_container(
                f"brinnatt/kubeauto-ext-bin:{self.kube_version.v_extra_bin}",
                "temp_ext_bin"
            )

            # Create temp directory
            tmp_dir = self.temp_path / "extra_bin_tmp"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(exist_ok=True, parents=True)

            # Copy binaries from container
            self.docker.copy_from_container(
                "temp_ext_bin",
                "/extra",
                str(tmp_dir)
            )

            # Atomic move to target directory
            for item in tmp_dir.iterdir():
                dest = self.kube_bin_dir / item.name
                if dest.exists():
                    dest.unlink()  # Remove existing files
                item.rename(dest)

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

            if tmp_dir and tmp_dir.exists():
                try:
                    # Ensure directory is empty before removal
                    for item in tmp_dir.iterdir():
                        item.unlink()
                    tmp_dir.rmdir()
                except Exception as e:
                    logger.warning(f"Temp directory cleanup failed: {e}")

    def get_default_images(self) -> None:
        """Download default images and upload to local registry"""
        images = [
            f"calico/cni:{self.kube_version.v_calico}",
            f"calico/kube-controllers:{self.kube_version.v_calico}",
            f"calico/node:{self.kube_version.v_calico}",
            f"coredns/coredns:{self.kube_version.v_coredns}",
            f"brinnatt/k8s-dns-node-cache:{self.kube_version.v_dnsNodeCache}",
            f"kubernetesui/dashboard:{self.kube_version.v_dashboard}",
            f"kubernetesui/metrics-scraper:{self.kube_version.v_dashboardMetricsScraper}",
            f"brinnatt/metrics-server:{self.kube_version.v_metricsServer}",
            f"brinnatt/pause:{self.kube_version.v_pause}"
        ]

        try:
            self.registry.upload_to_registry(images)
        except Exception as e:
            logger.error(f"Failed to upload images: {e}")

        logger.info(f"Default images uploaded to registry successfully!")

    def get_harbor_offline_pkg(self) -> None:
        """Download Harbor offline installer package with caching and error handling"""
        # Check if package already exists
        harbor_file = self.image_dir / f"harbor-offline-installer-{self.kube_version.v_harbor}.tgz"
        if harbor_file.exists():
            logger.warning("Harbor offline installer already exists")
            return

        logger.info(f"Downloading Harbor offline installer: {self.kube_version.v_harbor}")

        # Initialize variables for cleanup
        container_id = None
        tmp_dir = None
        try:
            # Handle container image with caching
            image_tar = self.image_dir / f"harbor_{self.kube_version.v_harbor}.tar"
            if not image_tar.exists():
                self.docker.pull_image(f"brinnatt/harbor-offline:{self.kube_version.v_harbor}")
                self.docker.save_image(f"brinnatt/harbor-offline:{self.kube_version.v_harbor}", str(image_tar))
            self.docker.load_image(str(image_tar))

            # Run temporary container
            container_id = self.docker.run_temp_container(
                f"brinnatt/harbor-offline:{self.kube_version.v_harbor}",
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
                f"/harbor-offline-installer-{self.kube_version.v_harbor}.tgz",
                str(tmp_dir)
            )

            # Verify and move to final location
            tmp_file = tmp_dir / f"harbor-offline-installer-{self.kube_version.v_harbor}.tgz"
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
