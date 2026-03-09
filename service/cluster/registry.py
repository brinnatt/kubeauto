from common.logger import setup_logger, LOG_STDOUT
from pathlib import Path
from typing import List, Optional
from .docker import DockerManager
from common.constants import KubeConstant
from common.exceptions import CommandExecutionError

logger = setup_logger(__name__)


class RegistryManager:
    def __init__(self):
        self.docker = DockerManager()
        self.kube_constant = KubeConstant()
        self.image_dir = Path(self.kube_constant.IMAGE_DIR)
        self.base_data_path = Path(self.kube_constant.BASE_DATA_PATH)

    def start_local_registry(self, version: Optional[str] = None) -> None:
        """Start local Docker registry"""
        version = version or self.kube_constant.v_docker_registry

        if self.docker.container_exists("local_registry"):
            logger.warning("Local registry is already running", extra=LOG_STDOUT)
            return

        # Load registry image if not exists
        registry_tar = self.image_dir / f"registry-{version}.tar"
        if not registry_tar.exists():
            logger.info(f"[REGISTRY] Downloading registry image: registry:{version}", extra=LOG_STDOUT)
            self.docker.pull_image(f"registry:{version}")
            self.docker.save_image(f"registry:{version}", str(registry_tar))
        else:
            logger.info(f"[REGISTRY] Loading registry image from cache: {registry_tar}", extra=LOG_STDOUT)
            self.docker.load_image(str(registry_tar))

        # Create registry directory
        registry_data = self.base_data_path / "registry"
        registry_data.mkdir(parents=True, exist_ok=True)

        # Run registry container
        logger.info(f"Starting local registry: {version}", extra=LOG_STDOUT)
        self.docker.run_container(
            image=f"registry:{version}",
            name="local_registry",
            publish=["5000:5000"],
            restart="always",
            volume=[f"{registry_data}:/var/lib/registry"]
        )

        # Add registry to hosts file
        hosts_file = Path("/etc/hosts")
        content = hosts_file.read_text()
        if "registry.talkschool.cn" not in content:
            with hosts_file.open("a") as f:
                f.write("127.0.0.1  registry.talkschool.cn\n")

    def upload_to_registry(self, images: List[str]) -> None:
        """Upload images to local registry. Logs progress and per-image steps for traceability."""

        if not self.docker.check_container_exists("local_registry"):
            self.start_local_registry()

        total = len(images)
        logger.info(f"[REGISTRY] Uploading {total} image(s) to local registry", extra=LOG_STDOUT)

        for idx, image in enumerate(images, start=1):
            logger.info(f"[REGISTRY] [{idx}/{total}] Image: {image}", extra=LOG_STDOUT)
            try:
                if not self.docker.image_exists(image):
                    logger.info(f"[REGISTRY]   -> Pulling from remote...", extra=LOG_STDOUT)
                    self.docker.pull_image(image)
                else:
                    logger.info(f"[REGISTRY]   -> Already exists locally, skip pull", extra=LOG_STDOUT)

                # Image may contain multiple colons (e.g. host:5000/name:tag); tag is after last colon
                repo, _, tag = image.rpartition(":")
                if not tag:
                    tag = "latest"
                local_image = f"registry.talkschool.cn:5000/{repo}:{tag}"

                logger.info(f"[REGISTRY]   -> Tagging and pushing to local registry...", extra=LOG_STDOUT)
                self.docker.tag_image(image, local_image)
                self.docker.push_image(local_image)
                logger.info(f"[REGISTRY]   -> Done: {image}", extra=LOG_STDOUT)
            except CommandExecutionError as e:
                logger.error(f"[REGISTRY]   -> Failed: {image} — {e}", extra=LOG_STDOUT)
                raise
            except Exception as e:
                logger.error(f"[REGISTRY]   -> Exception for image {image}: {e}", extra=LOG_STDOUT)
                raise

        logger.info(f"[REGISTRY] All {total} image(s) uploaded to local registry successfully", extra=LOG_STDOUT)
