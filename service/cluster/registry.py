from common.logger import setup_logger, LOG_STDOUT
from pathlib import Path
from typing import List, Optional
import time
import urllib.error
import urllib.request

from .docker import DockerManager
from common.constants import KubeConstant
from common.exceptions import CommandExecutionError

logger = setup_logger(__name__)

# Legacy ghcr.io refs kept for any out-of-tree callers; flannel now uses brinnatt/*.
_GHCR_PULL_FALLBACKS = {
    "ghcr.io/flannel-io/flannel": ["flannel/flannel", "ghcr.dockerproxy.com/flannel-io/flannel"],
    "ghcr.io/flannel-io/flannel-cni-plugin": [
        "flannel/flannel-cni-plugin",
        "ghcr.dockerproxy.com/flannel-io/flannel-cni-plugin",
    ],
}

# Upstream origins for newly migrated brinnatt/* images.
# Used only when talkedu + Docker Hub brinnatt miss (before CI dual-push publishes).
# Remove entries once the corresponding ext-images CI tags are live everywhere.
_BRINATT_UPSTREAM_FALLBACKS = {
    "brinnatt/calico-cni": "calico/cni",
    "brinnatt/calico-node": "calico/node",
    "brinnatt/calico-kube-controllers": "calico/kube-controllers",
    "brinnatt/coredns": "coredns/coredns",
    "brinnatt/registry": "registry",
    # Cilium publishes these tags on quay.io (hubble-ui:v0.13.5 is NOT on docker.io).
    "brinnatt/cilium": "quay.io/cilium/cilium",
    "brinnatt/cilium-operator-generic": "quay.io/cilium/operator-generic",
    "brinnatt/hubble-relay": "quay.io/cilium/hubble-relay",
    "brinnatt/hubble-ui": "quay.io/cilium/hubble-ui",
    "brinnatt/hubble-ui-backend": "quay.io/cilium/hubble-ui-backend",
    "brinnatt/flannel": "flannel/flannel",
    "brinnatt/flannel-cni-plugin": "flannel/flannel-cni-plugin",
    "brinnatt/dashboard-api": "kubernetesui/dashboard-api",
    "brinnatt/dashboard-auth": "kubernetesui/dashboard-auth",
    "brinnatt/dashboard-metrics-scraper": "kubernetesui/dashboard-metrics-scraper",
    "brinnatt/dashboard-web": "kubernetesui/dashboard-web",
    "brinnatt/kong": "kong",
    "brinnatt/minio-operator": "quay.io/minio/operator",
    "brinnatt/minio-operator-sidecar": "quay.io/minio/operator-sidecar",
    "brinnatt/minio": "quay.io/minio/minio",
    "brinnatt/nacos-server": "nacos/nacos-server",
    "brinnatt/nacos-peer-finder-plugin": "nacos/nacos-peer-finder-plugin",
    # openebs-kubectl: no docker-image upstream. Built in ext-images from the
    # official kubectl binary at dl.k8s.io. Pull order: talkedu → Hub brinnatt only.
    "brinnatt/provisioner-localpv": "openebs/provisioner-localpv",
    "brinnatt/linux-utils": "openebs/linux-utils",
    "brinnatt/lvm-driver": "openebs/lvm-driver",
    "brinnatt/rocketmq-operator": "apache/rocketmq-operator",
    "brinnatt/rocketmq-broker": "apacherocketmq/rocketmq-broker",
    "brinnatt/rocketmq-nameserver": "apacherocketmq/rocketmq-nameserver",
    "brinnatt/rocketmq-console": "apacherocketmq/rocketmq-console",
    "brinnatt/kube-ovn": "kubeovn/kube-ovn",
    "brinnatt/kube-router": "cloudnativelabs/kube-router",
    "brinnatt/local-path-provisioner": "rancher/local-path-provisioner",
    "brinnatt/grafana": "grafana/grafana",
    "brinnatt/k8s-sidecar": "quay.io/kiwigrid/k8s-sidecar",
    "brinnatt/prometheus-config-reloader": "quay.io/prometheus-operator/prometheus-config-reloader",
    "brinnatt/prometheus-operator": "quay.io/prometheus-operator/prometheus-operator",
    "brinnatt/alertmanager": "quay.io/prometheus/alertmanager",
    "brinnatt/node-exporter": "quay.io/prometheus/node-exporter",
    "brinnatt/prometheus": "quay.io/prometheus/prometheus",
    "brinnatt/prometheus-webhook-dingtalk": "timonwong/prometheus-webhook-dingtalk",
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

    def _registry_data_path(self) -> Path:
        """Return a bind-mount source visible to the active Docker daemon.

        Strictly confined Snap Docker exposes its persistent ``SNAP_COMMON``
        tree at ``/var/snap/docker/common``.  A bind source such as ``/data``
        can otherwise become a private tmpfs inside the container, so deleting
        the host path does not reset registry blobs.
        """
        try:
            result = self.docker._run_docker(
                ["info", "--format", "{{.DockerRootDir}}"]
            )
            docker_root = Path(result.stdout.strip())
        except (CommandExecutionError, AttributeError, TypeError, ValueError):
            docker_root = Path()

        snap_common = Path("/var/snap/docker/common")
        if docker_root.is_relative_to(snap_common):
            return snap_common / "kubeauto-registry"
        return self.base_data_path / "registry"

    def _registry_http_ok(self) -> bool:
        """Return True if local registry answers Registry HTTP API V2 on loopback :5000."""
        # Probe 127.0.0.1 (not the hostname) so readiness does not depend on /etc/hosts.
        url = "http://127.0.0.1:5000/v2/"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                # 200 OK (anonymous) or 401 (auth required) both mean the daemon is listening.
                return resp.status in (200, 401)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _wait_for_registry_ready(self, timeout: float = 30.0, interval: float = 0.5) -> bool:
        """Poll until local registry accepts GET /v2/ (same pattern as Docker daemon readiness)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._registry_http_ok():
                logger.debug("Local registry is ready (GET /v2/ ok).")
                return True
            time.sleep(interval)
        logger.warning(
            f"[REGISTRY] Local registry not ready after {timeout}s (GET http://127.0.0.1:5000/v2/).",
            extra=LOG_STDOUT,
        )
        return False

    def _ensure_local_registry_hostname(self, hosts_file: Path = Path("/etc/hosts")) -> None:
        """Make the control host resolve its local registry name to loopback."""
        try:
            lines = hosts_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CommandExecutionError(f"Unable to read {hosts_file}: {exc}", 1) from exc

        retained = [line for line in lines if "registry.talkschool.cn" not in line]
        retained.append("127.0.0.1  registry.talkschool.cn")
        self.docker._write_privileged_file(hosts_file, "\n".join(retained) + "\n")

    def start_local_registry(self, version: Optional[str] = None) -> None:
        """Start local Docker registry (create if missing, start if stopped).

        Always waits until the Registry HTTP API responds before returning, so
        callers (upload / download -D) do not race a just-started container.
        """
        version = version or self.kube_constant.v_docker_registry
        # A former test may have left this name pointing at a retired control IP.
        # Pushes originate locally, so normalize it even if the container is running.
        self._ensure_local_registry_hostname()

        if self.docker.is_container_running("local_registry"):
            logger.warning("[REGISTRY] Local registry already running; skipping.", extra=LOG_STDOUT)
        elif self.docker.container_exists("local_registry"):
            logger.info("[REGISTRY] Local registry container exists but is stopped; starting.", extra=LOG_STDOUT)
            self.docker.start_container("local_registry")
        else:
            # Load registry image if not exists (brinnatt/registry → talkedu first, Docker Hub fallback)
            registry_image = f"brinnatt/registry:{version}"
            legacy_image = f"registry:{version}"
            registry_tar = self.image_dir / f"registry-{version}.tar"
            if not registry_tar.exists():
                logger.info(f"[REGISTRY] Pulling registry image {registry_image}.", extra=LOG_STDOUT)
                self._ensure_image_local(registry_image)
                self.docker.save_image(registry_image, str(registry_tar))
            else:
                logger.info(f"[REGISTRY] Loading registry image from cache.", extra=LOG_STDOUT)
                self.docker.load_image(str(registry_tar))
                # Legacy caches saved registry:2; retag to brinnatt/registry for talkedu alignment
                if not self.docker.image_exists(registry_image):
                    if self.docker.image_exists(legacy_image):
                        self.docker.tag_image(legacy_image, registry_image)
                    else:
                        self._ensure_image_local(registry_image)

            # Create registry directory
            registry_data = self._registry_data_path()
            registry_data.mkdir(parents=True, exist_ok=True)

            # Run registry container
            logger.info(f"[REGISTRY] Starting local registry (image {registry_image}).", extra=LOG_STDOUT)
            self.docker.run_container(
                image=registry_image,
                name="local_registry",
                publish=["5000:5000"],
                restart="always",
                volume=[f"{registry_data}:/var/lib/registry"]
            )

        if not self._wait_for_registry_ready():
            raise CommandExecutionError(
                "Local registry is not ready on :5000 after start. "
                "Check: docker ps -a --filter name=local_registry && "
                "curl -v http://127.0.0.1:5000/v2/",
                1,
            )

    def upload_to_registry(self, images: List[str], *, fail_fast: bool = True) -> None:
        """Upload images to local registry. Logs progress and per-image steps for traceability.

        fail_fast=True: abort on first failure (defaults / critical paths).
        fail_fast=False: continue; raise only if every image failed (extra components).
        """

        self.start_local_registry()

        total = len(images)
        logger.info(f"[REGISTRY] Uploading {total} image(s) to local registry.", extra=LOG_STDOUT)
        failures: List[str] = []
        ok = 0

        for idx, image in enumerate(images, start=1):
            logger.info(f"[REGISTRY] [{idx}/{total}] Image: {image}", extra=LOG_STDOUT)
            try:
                self._ensure_image_local(image)

                # Image may contain multiple colons (e.g. host:5000/name:tag); tag is after last colon
                repo, _, tag = image.rpartition(":")
                if not tag:
                    tag = "latest"
                # Pushes originate on the registry host itself. Use loopback so
                # a stale DNS/hosts cache cannot send the control-plane upload
                # to a retired lab address. The registry stores the same
                # ``brinnatt/<name>:<tag>`` repository which nodes later pull
                # through registry.talkschool.cn:5000.
                local_image = f"127.0.0.1:5000/{repo}:{tag}"

                logger.info(f"[REGISTRY]   -> Tagging and pushing to local registry.", extra=LOG_STDOUT)
                self.docker.tag_image(image, local_image)
                self.docker.push_image(local_image)
                logger.info(f"[REGISTRY]   -> Done: {image}.", extra=LOG_STDOUT)
                ok += 1
            except CommandExecutionError as e:
                logger.error(f"[REGISTRY]   -> Failed: {image} — {e}", extra=LOG_STDOUT)
                if fail_fast:
                    raise
                failures.append(str(image))
            except Exception as e:
                logger.error(f"[REGISTRY]   -> Failed: {image} — {e}", extra=LOG_STDOUT)
                if fail_fast:
                    raise
                failures.append(str(image))

        if failures:
            if ok == 0:
                raise CommandExecutionError(
                    f"Failed to upload all {total} image(s): {', '.join(failures)}",
                    1,
                )
            logger.warning(
                f"[REGISTRY] Partial upload: {ok}/{total} ok; failed: {', '.join(failures)}",
                extra=LOG_STDOUT,
            )
            return

        logger.info(f"[REGISTRY] All {total} image(s) uploaded to local registry.", extra=LOG_STDOUT)

    def _ensure_image_local(self, image: str) -> None:
        """Pull image if missing.

        Order for brinnatt/* (aligned with CI dual-push to hub.talkedu.cn + Docker Hub):
          1. hub.talkedu.cn/kubeauto/<name>:<tag>
          2. brinnatt/<name>:<tag> (Docker Hub)
          3. upstream origin (migration bridge until CI publishes the brinnatt tag)
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
        upstream = _BRINATT_UPSTREAM_FALLBACKS.get(repo)
        if upstream:
            upstream_ref = f"{upstream}:{tag}"
            if upstream_ref not in candidates:
                candidates.append(upstream_ref)
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
