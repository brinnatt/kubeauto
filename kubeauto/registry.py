from common.logger import setup_logger
from pathlib import Path
from typing import List, Optional
from .docker import DockerManager
from common.constants import KubeVersion
from common.exceptions import CommandExecutionError

logger = setup_logger(__name__)


class RegistryManager:
    def __init__(self):
        self.docker = DockerManager()
        self.kube_version = KubeVersion()
        self.image_dir = Path(self.kube_version.IMAGE_DIR)
        self.base_data_path = Path(self.kube_version.BASE_DATA_PATH)

    def start_local_registry(self, version: Optional[str] = None) -> None:
        """Start local Docker registry"""
        version = version or self.kube_version.v_docker_registry

        if self.docker.container_exists("local_registry"):
            logger.warning("Local registry is already running")
            return

        # Load registry image if not exists
        registry_tar = self.image_dir / f"registry-{version}.tar"
        if not registry_tar.exists():
            logger.info(f"Downloading registry:{version} image")
            self.docker.pull_image(f"registry:{version}")
            self.docker.save_image(f"registry:{version}", str(registry_tar))
        else:
            self.docker.load_image(str(registry_tar))

        # Create registry directory
        registry_data = self.base_data_path / "registry"
        registry_data.mkdir(parents=True, exist_ok=True)

        # Run registry container
        logger.info(f"Starting local registry: {version}")
        self.docker.run_temp_container(
            image=f"registry:{version}",
            name="local_registry",
            network="host",
            restart="always",
            volume=f"{registry_data}:/var/lib/registry"
        )

        # Add registry to hosts file
        hosts_file = Path("/etc/hosts")
        content = hosts_file.read_text()
        if "registry.talkschool.cn" not in content:
            with hosts_file.open("a") as f:
                f.write("127.0.0.1  registry.talkschool.cn\n")

    def upload_to_registry(self, images: List[str]) -> None:
        """Upload images to local registry"""
        self.start_local_registry()

        for image in images:
            # Pull image if not exists locally
            try:
                self.docker.pull_image(image)
            except CommandExecutionError:
                logger.warning(f"Failed to pull image {image}, skipping")
                continue

            # Tag and push to local registry
            parts = image.split(':')
            repo = parts[0]
            tag = parts[1] if len(parts) > 1 else "latest"
            local_image = f"registry.talkschool.cn:5000/{repo}:{tag}"

            self.docker.tag_image(image, local_image)
            self.docker.push_image(local_image)
            logger.info(f"Uploaded {image} to local registry successfully!")
