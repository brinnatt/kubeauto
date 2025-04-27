import json
import re
from pathlib import Path
from typing import Optional
from common.constants import KubeVersion
from common.utils import run_command
from common.exceptions import CommandExecutionError
from common.logger import setup_logger

logger = setup_logger(__name__)


class DockerManager:
    def __init__(self):
        self.kube_version = KubeVersion()
        self.base_path = Path(self.kube_version.BASE_PATH)
        self.image_dir = Path(self.kube_version.IMAGE_DIR)
        self.docker_bin_dir = Path(self.kube_version.DOCKER_BIN_DIR)
        self.base_data_path = Path(self.kube_version.BASE_DATA_PATH)
        self.temp_path = Path(self.kube_version.TEMP_PATH)

    @property
    def is_docker_installed(self) -> bool:
        """Check if Docker is installed and running"""
        try:
            run_command(["docker", "info"])
            return True
        except CommandExecutionError:
            return False

    def install_docker(self, version: Optional[str] = None) -> None:
        """Install Docker"""
        # Download Docker binaries
        version = version or self.kube_version.v_docker

        self._download_docker(version)

        # Install Docker binaries
        self._install_docker_binaries(version)

        # Configure Docker
        self._configure_docker(version)

        # Start Docker service
        self._start_docker_service(version)

    def _download_docker(self, version: str) -> None:
        """Download Docker binaries"""
        docker_tgz = self.image_dir / f"docker-{version}.tgz"
        if docker_tgz.exists():
            logger.warning("Docker binaries already exist")
            return

        logger.info(f"Downloading Docker binaries, version: {version}")

        docker_bin_url = self.kube_version.docker_bin_url(version)

        try:
            run_command(["wget", "-c", "--no-check-certificate", docker_bin_url, "-O", str(docker_tgz)])
        except CommandExecutionError:
            run_command(["curl", "-k", "-C-", "-o", str(docker_tgz), docker_bin_url])

        logger.info(f"Docker binaries have been downloaded successfully!")

    def _install_docker_binaries(self, version) -> None:
        """Install Docker binaries"""
        logger.info(f"Installing Docker binaries, version: {version}")

        self.temp_path.mkdir(parents=True, exist_ok=True)
        self.docker_bin_dir.mkdir(parents=True, exist_ok=True)

        run_command(["tar", "xf", str(self.image_dir / f"docker-{version}.tgz"), "-C", str(self.temp_path)])
        run_command(["cp", "-f", str(self.temp_path / "docker" / "*"), str(self.docker_bin_dir)])
        run_command(["ln", "-svf", str(self.docker_bin_dir / "*"), "/usr/local/bin/"])

        run_command(["rm", "-rf", str(self.temp_path / "docker")])
        logger.info(f"Docker binaries have been installed successfully!")

    def _configure_docker(self, version: str) -> None:
        """Configure Docker daemon"""
        logger.info(f"Configuring Docker daemon, version: {version}")

        # Create systemd service file
        service_file = Path("/etc/systemd/system/docker.service")
        service_file.write_text("""
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com/
[Service]
Environment="PATH=/usr/local/bin:/bin:/sbin:/usr/bin:/usr/sbin"
ExecStart=/usr/local/bin/dockerd
ExecStartPost=/sbin/iptables -I FORWARD -s 0.0.0.0/0 -j ACCEPT
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure
RestartSec=5
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
Delegate=yes
KillMode=process
[Install]
WantedBy=multi-user.target
""")

        # Create daemon.json config
        v_docker_main = int(version.split('.')[0])
        cgroup_driver = "systemd" if v_docker_main >= 20 else "cgroupfs"

        data_docker = self.base_data_path / "docker"
        data_docker.mkdir(parents=True, exist_ok=True)

        config = {
            "exec-opts": [f"native.cgroupdriver={cgroup_driver}"],
            "insecure-registries": ["http://registry.talkschool.cn:5000"],
            "max-concurrent-downloads": 10,
            "log-driver": "json-file",
            "log-level": "warn",
            "log-opts": {
                "max-size": "10m",
                "max-file": "3"
            },
            "data-root": f"{data_docker}"
        }

        daemon_json = Path("/etc/docker/daemon.json")
        daemon_json.write_text(json.dumps(config, indent=2))

        # Disable SELinux if present
        selinux_config = Path("/etc/selinux/config")
        if selinux_config.exists():
            logger.debug("Disabling SELinux")
            run_command(["setenforce", "0"])
            content = selinux_config.read_text()
            content = re.sub(r'^SELINUX=.*$', 'SELINUX=disabled', content, flags=re.MULTILINE)
            selinux_config.write_text(content)

        logger.info(f"Docker daemon has been configured successfully!")

    def _start_docker_service(self, version) -> None:
        """Start and enable Docker service"""
        logger.info(f"Starting Docker service, version: {version}")

        run_command(["systemctl", "enable", "docker"])
        run_command(["systemctl", "daemon-reload"])
        run_command(["systemctl", "restart", "docker"])

        logger.info("Docker service has been started successfully!")

    def container_exists(self, name: str) -> bool:
        """Check if a container exists"""
        try:
            run_command(["docker", "ps", "-a", "--format={{.Names}}", "--filter", f"name={name}"])
            return True
        except CommandExecutionError:
            return False

    def remove_container(self, name: str) -> None:
        """Remove a container"""
        if self.container_exists(name):
            logger.debug(f"Removing container: {name}")
            run_command(["docker", "rm", "-f", name])

    def run_temp_container(self, image: str, name: str, **kwargs) -> str:
        """Run a temporary container and return its ID"""
        self.remove_container(name)

        cmd = ["docker", "run", "-d", "--name", name]
        for k, v in kwargs.items():
            if v is not None:
                cmd.extend([f"--{k.replace('_', '-')}", str(v)])
        cmd.append(image)

        result = run_command(cmd)
        return result.stdout.strip()

    def copy_from_container(self, container: str, src: str, dest: str) -> None:
        """Copy files from container to host"""
        run_command(["docker", "cp", f"{container}:{src}", dest])

    def pull_image(self, image: str) -> None:
        """Pull a Docker image"""
        run_command(["docker", "pull", image])

    def save_image(self, image: str, output: str) -> None:
        """Save Docker image to tar file"""
        run_command(["docker", "save", "-o", output, image])

    def load_image(self, input_file: str) -> None:
        """Load Docker image from tar file"""
        run_command(["docker", "load", "-i", input_file])

    def tag_image(self, src: str, dest: str) -> None:
        """Tag a Docker image"""
        run_command(["docker", "tag", src, dest])

    def push_image(self, image: str) -> None:
        """Push Docker image to registry"""
        run_command(["docker", "push", image])