from common.logger import setup_logger, LOG_STDOUT
from pathlib import Path
from typing import List, Optional
from .docker import DockerManager
from common.constants import KubeConstant
from common.exceptions import CommandExecutionError

logger = setup_logger(__name__)

# ghcr.io images often mirror Docker Hub; try these when direct ghcr pull fails (CN networks).
_GHCR_PULL_FALLBACKS = {
    "ghcr.io/flannel-io/flannel": ["flannel/flannel", "ghcr.dockerproxy.com/flannel-io/flannel"],
    "ghcr.io/flannel-io/flannel-cni-plugin": [
        "flannel/flannel-cni-plugin",
        "ghcr.dockerproxy.com/flannel-io/flannel-cni-plugin",
    ],
}

_BRINATT_PREFIX = "brinnatt/"


def _talkedu_mirror(image: str, registry: str) -> Optional[str]:
    """Map brinnatt/<name>:<tag> → <registry>/<name>:<tag> (CN private mirror).

    Pull order for brinnatt/* images (CN production):
      1. hub.talkedu.cn/kubeauto/<name>:<tag>   (preferred)
      2. brinnatt/<name>:<tag>                 (Docker Hub fallback)
    """
    if ":" in image:
        repo, _, tag = image.rpartition(":")
    else:
        repo, tag = image, "latest"
    if not tag:
        tag = "latest"
    if repo.startswith(_BRINATT_PREFIX) and repo.count("/") == 1:
        return f"{registry}/{repo[len(_BRINATT_PREFIX):]}:{tag}"
    return None


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
            logger.warning("[REGISTRY] Local registry already running; skipping.", extra=LOG_STDOUT)
            return

        # Load registry image if not exists
        registry_tar = self.image_dir / f"registry-{version}.tar"
        if not registry_tar.exists():
            logger.info(f"[REGISTRY] Pulling registry image registry:{version}.", extra=LOG_STDOUT)
            self.docker.pull_image(f"registry:{version}")
            self.docker.save_image(f"registry:{version}", str(registry_tar))
        else:
            logger.info(f"[REGISTRY] Loading registry image from cache.", extra=LOG_STDOUT)
            self.docker.load_image(str(registry_tar))

        # Create registry directory
        registry_data = self.base_data_path / "registry"
        registry_data.mkdir(parents=True, exist_ok=True)

        # Run registry container
        logger.info(f"[REGISTRY] Starting local registry (image registry:{version}).", extra=LOG_STDOUT)
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

        if not self.docker.container_exists("local_registry"):
            self.start_local_registry()

        total = len(images)
        logger.info(f"[REGISTRY] Uploading {total} image(s) to local registry.", extra=LOG_STDOUT)

        for idx, image in enumerate(images, start=1):
            logger.info(f"[REGISTRY] [{idx}/{total}] Image: {image}", extra=LOG_STDOUT)
            try:
                self._ensure_image_local(image)

                # Image may contain multiple colons (e.g. host:5000/name:tag); tag is after last colon
                repo, _, tag = image.rpartition(":")
                if not tag:
                    tag = "latest"
                local_image = f"registry.talkschool.cn:5000/{repo}:{tag}"

                logger.info(f"[REGISTRY]   -> Tagging and pushing to local registry.", extra=LOG_STDOUT)
                self.docker.tag_image(image, local_image)
                self.docker.push_image(local_image)
                logger.info(f"[REGISTRY]   -> Done: {image}.", extra=LOG_STDOUT)
            except CommandExecutionError as e:
                logger.error(f"[REGISTRY]   -> Failed: {image} — {e}", extra=LOG_STDOUT)
                raise
            except Exception as e:
                logger.error(f"[REGISTRY]   -> Failed: {image} — {e}", extra=LOG_STDOUT)
                raise

        logger.info(f"[REGISTRY] All {total} image(s) uploaded to local registry.", extra=LOG_STDOUT)

    def _ensure_image_local(self, image: str) -> None:
        """Pull image if missing.

        Order for brinnatt/* (aligned with CI dual-push to hub.talkedu.cn + Docker Hub):
          1. hub.talkedu.cn/kubeauto/<name>:<tag>
          2. brinnatt/<name>:<tag> (Docker Hub)
        ghcr.io images keep existing Docker Hub / proxy fallbacks after the primary ref.
        """
        if self.docker.image_exists(image):
            logger.info(f"[REGISTRY]   -> Image exists locally; skipping pull.", extra=LOG_STDOUT)
            return

        talkedu = _talkedu_mirror(image, self.kube_constant.v_talkedu_registry)
        # Prefer CN private registry; Docker Hub (original image ref) is fallback.
        candidates = [talkedu, image] if talkedu else [image]

        repo, _, tag = image.rpartition(":")
        if not tag:
            tag = "latest"
        if repo in _GHCR_PULL_FALLBACKS:
            for alt in _GHCR_PULL_FALLBACKS[repo]:
                alt_ref = f"{alt}:{tag}"
                if alt_ref not in candidates:
                    candidates.append(alt_ref)

        last_err = None
        total = len(candidates)
        for idx, candidate in enumerate(candidates):
            is_last = idx == total - 1
            try:
                if idx > 0:
                    logger.info(f"[REGISTRY]   -> Retry pull via fallback: {candidate}", extra=LOG_STDOUT)
                else:
                    logger.info(f"[REGISTRY]   -> Pulling from remote: {candidate}", extra=LOG_STDOUT)
                self.docker._execute_pull(candidate)
                if candidate != image:
                    self.docker.tag_image(candidate, image)
                return
            except CommandExecutionError as e:
                last_err = e
                if is_last:
                    logger.error(
                        f"[REGISTRY]   -> All pull sources failed for {image} "
                        f"(tried {total} source(s), last: {candidate}).",
                        extra=LOG_STDOUT,
                    )
                else:
                    logger.warning(
                        f"[REGISTRY]   -> Pull failed ({candidate}), trying next source.",
                        extra=LOG_STDOUT,
                    )
        raise last_err
