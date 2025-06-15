import json
import re
from pathlib import Path
from typing import Optional, Dict, List
import docker
from docker.errors import DockerException, APIError, ImageNotFound
from common.constants import KubeConstant
from common.utils import run_command
from common.exceptions import CommandExecutionError
from common.logger import setup_logger

logger = setup_logger(__name__)


class DockerManager:
    def __init__(self):
        self.kube_constant = KubeConstant()
        self.base_path = Path(self.kube_constant.BASE_PATH)
        self.image_dir = Path(self.kube_constant.IMAGE_DIR)
        self.docker_bin_dir = Path(self.kube_constant.DOCKER_BIN_DIR)
        self.base_data_path = Path(self.kube_constant.BASE_DATA_PATH)
        self.sys_bin_dir = Path(self.kube_constant.SYS_BIN_DIR)
        self.temp_path = Path(self.kube_constant.TEMP_PATH)
        self.docker_proxy_dir = Path(self.kube_constant.DOCKER_PROXY_DIR)

        # Initialize Docker SDK client
        self._client = None
        self._initialize_docker_client()

    def _initialize_docker_client(self):
        """Initialize Docker SDK client"""
        try:
            self._client = docker.from_env()
            # verify docker sdk connection
            self._client.ping()
            logger.debug("Docker SDK client initialized successfully!")
        except DockerException as e:
            logger.debug(f"Failed to initialize Docker SDK client: {str(e)}")
            self._client = None

    @property
    def client(self):
        """Get Docker client，if SDK is unavailable return None"""
        return self._client

    @property
    def is_docker_installed(self) -> bool:
        """Check if Docker was installed and running"""
        if self.client is not None:
            return True

        try:
            run_command(["docker", "info"])
            return True
        except CommandExecutionError:
            return False

    def install_docker(self, version: Optional[str] = None) -> None:
        """Install Docker"""
        version = version or self.kube_constant.v_docker
        self._download_docker(version)
        self._install_docker_binaries(version)
        self._configure_docker(version)
        self._start_docker_service(version)

        # Initialize Docker SDK after installing docker
        self._initialize_docker_client()

    def uninstall_docker(self, assume_yes: bool = False) -> None:
        """
        Uninstall Docker
        :param assume_yes: user confirm(Default False)
        """
        if not self.is_docker_installed:
            logger.warning("Docker has not been installed, no need to uninstall", extra={'to_stdout': True})
            return

        docker_version = "Unknown Version"
        try:
            if self.client is not None:
                docker_version = self.client.version()["Version"]
            else:
                result = run_command(["docker", "--version"])
                version_match = re.search(r"Docker version (\S+)", result.stdout)
                if version_match:
                    docker_version = version_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to get Docker Version: {str(e)}", extra={"to_stdout": True})

        logger.info(f"Now Docker Version: {docker_version}", extra={'to_stdout': True})

        if not assume_yes:
            confirm = input(f"confirm to uninstall Docker {docker_version}? [Y/n] ").strip().lower()
            if confirm not in ('', 'y', 'yes'):
                logger.warning("Cancel uninstalling Docker", extra={'to_stdout': True})
                return

        logger.warning("Begin to uninstall Docker...", extra={'to_stdout': True})

        try:
            run_command(["systemctl", "stop", "docker"])
            run_command(["systemctl", "disable", "docker"])
            run_command(["systemctl", "daemon-reload"])
            logger.info("Docker service has been stopped and disabled", extra={'to_stdout': True})
        except CommandExecutionError as e:
            logger.warning(f"Failed to stop Docker service: {str(e)}", extra={"to_stdout": True})

        try:
            # check and delete all symlink to docker_bin_dir
            bin_dir = Path("/usr/local/bin")
            if bin_dir.exists():
                for item in bin_dir.iterdir():
                    if item.is_symlink():
                        try:
                            target = item.resolve()
                            if str(target).startswith(str(self.docker_bin_dir)):
                                item.unlink()
                                logger.debug(f"Already deleted: {item} -> {target}")
                        except (OSError, RuntimeError) as e:
                            logger.warning(f"Failed to resolve symlink {item}: {str(e)}", extra={'to_stdout': True})

            # delete docker_bin_dir
            if self.docker_bin_dir.exists():
                run_command(["rm", "-rf", str(self.docker_bin_dir)])
                logger.debug(f"Docker binary dir has been deleted: {self.docker_bin_dir}")
        except Exception as e:
            logger.warning(f"Failed to delete Docker binary dir: {str(e)}", extra={'to_stdout': True})

        try:
            service_file = Path("/etc/systemd/system/docker.service")
            if service_file.exists():
                service_file.unlink()
                logger.debug(f"Docker service file has been deleted: {service_file}")

            daemon_json = Path("/etc/docker/daemon.json")
            if daemon_json.exists():
                daemon_json.unlink()
                logger.debug(f"Docker config file has been deleted: {daemon_json}")

            # delete docker data dir
            if self.base_data_path.exists():
                run_command(["rm", "-rf", str(self.base_data_path / "docker")])
                logger.debug(f"Docker data dir has been deleted: {self.base_data_path / 'docker'}")

            # delete /var/run/docker.sock
            docker_sock = Path("/var/run/docker.sock")
            if docker_sock.exists():
                docker_sock.unlink()
                logger.debug(f"docker.sock has been deleted: {docker_sock}")
        except Exception as e:
            logger.warning(f"Failed to delete docker related files: {str(e)}")

        try:
            run_command(["groupdel", "docker"])
            logger.debug("docker group has been deleted")
        except CommandExecutionError:
            pass

        # 8. delete docker residual process if existing
        try:
            run_command(["pkill", "-9", "dockerd"])
            run_command(["pkill", "-9", "containerd"])
            logger.debug("Docker residual process has been killed")
        except CommandExecutionError:
            pass

        logger.info(f"Docker {docker_version} has been uninstalled successfully!", extra={'to_stdout': True})

    def _download_docker(self, version: str) -> None:
        """
        Download Docker binary
        """
        # ensure image_dir exists
        self.image_dir.mkdir(parents=True, exist_ok=True)

        docker_tgz = self.image_dir / f"docker-{version}.tgz"
        if docker_tgz.exists():
            logger.warning("Docker binary exists already", extra={'to_stdout': True})
            return

        logger.info(f"Downloading Docker binary: {version}", extra={'to_stdout': True})
        docker_bin_url = self.kube_constant.docker_bin_url(version)

        try:
            run_command(["wget", "-c", "--no-check-certificate", docker_bin_url, "-O", str(docker_tgz)])
        except CommandExecutionError:
            run_command(["curl", "-k", "-C-", "-o", str(docker_tgz), docker_bin_url])

        logger.info("Docker binary has been downloaded successfully!", extra={'to_stdout': True})

    def _install_docker_binaries(self, version) -> None:
        """
        Install Docker binary
        """
        logger.info(f"Installing Docker binary: {version}", extra={'to_stdout': True})

        self.temp_path.mkdir(parents=True, exist_ok=True)
        self.docker_bin_dir.mkdir(parents=True, exist_ok=True)

        run_command(["tar", "xf", str(self.image_dir / f"docker-{version}.tgz"), "-C", str(self.temp_path)])

        # [bug fixed] 在subprocess.run中直接使用*通配符时，shell不会自动扩展它
        run_command(["bash", "-c", f"cp -f {self.temp_path}/docker/* {self.docker_bin_dir}/"])
        for binary in self.docker_bin_dir.iterdir():
            if binary.is_file():
                run_command(["ln", "-svf", str(binary), str(self.sys_bin_dir)])

        run_command(["rm", "-rf", str(self.temp_path / "docker")])

        logger.info("Docker binary has been installed successfully!", extra={'to_stdout': True})

    def _configure_docker(self, version: str) -> None:
        """
        Configure Docker binary
        """
        logger.info(f"Configuring Docker Daemon: {version}", extra={'to_stdout': True})

        # Create docker user group
        try:
            # 9 indicates docker group exists
            run_command(["groupadd", "-r", "docker"], allowed_exit_codes=[0, 9])
        except Exception as e:
            logger.error(f"Failed to create docker user group: {e}", extra={'to_stdout': True})

        # Create docker bash completion (https://docs.docker.com/engine/cli/completion/)
        try:
            completions_dir = Path("~/.local/share/bash-completion/completions").expanduser()
            completions_dir.mkdir(parents=True, exist_ok=True)

            docker_completion = run_command(["docker", "completion", "bash"])
            output_file = completions_dir / "docker"

            with open(output_file, "w") as f:
                f.write(docker_completion.stdout)

        except CommandExecutionError as e:
            logger.error(f"Failed to generate docker completions: {e}", extra={'to_stdout': True})
        except IOError as e:
            logger.error(f"Failed to write completions file: {e}", extra={'to_stdout': True})

        # create systemd service file
        try:
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
        except Exception as e:
            logger.error("Failed to configure docker systemd file.")
            raise e

        # create daemon.json config
        try:
            v_docker_main = int(version.split('.')[0])
            cgroup_driver = "systemd" if v_docker_main >= 20 else "cgroupfs"

            data_docker = self.base_data_path / "docker"
            data_docker.mkdir(parents=True, exist_ok=True)

            config = {
                "exec-opts": [f"native.cgroupdriver={cgroup_driver}"],
                "insecure-registries": ["registry.talkschool.cn:5000"],
                "max-concurrent-downloads": 10,
                "log-driver": "json-file",
                "log-level": "warn",
                "log-opts": {
                    "max-size": "10m",
                    "max-file": "3"
                },
                "registry-mirrors": [
                    "https://docker.410006.xyz"
                ],
                "data-root": f"{data_docker}"
            }

            daemon_json = Path("/etc/docker/daemon.json")
            daemon_json.parent.mkdir(parents=True, exist_ok=True)
            daemon_json.write_text(json.dumps(config, indent=2))
        except Exception as e:
            logger.error("Failed to configure docker daemon.json file.", extra={'to_stdout': True})
            raise e

        # Disable SELinux if enabled
        try:
            selinux_config = Path("/etc/selinux/config")
            if selinux_config.exists():
                logger.debug("disabling SELinux")
                run_command(["setenforce", "0"], allowed_exit_codes=[0, 1])
                content = selinux_config.read_text()
                content = re.sub(r'^SELINUX=.*$', 'SELINUX=disabled', content, flags=re.MULTILINE)
                selinux_config.write_text(content)
        except Exception as e:
            logger.error("Failed to configure SELinux config.", extra={'to_stdout': True})
            raise e

        logger.info("Docker daemon has been configured successfully!", extra={'to_stdout': True})

    def _start_docker_service(self, version) -> None:
        """
        Start Docker service
        """
        logger.info(f"Starting Docker Service: {version}")
        try:
            run_command(["systemctl", "enable", "docker"])
            run_command(["systemctl", "daemon-reload"])
            run_command(["systemctl", "restart", "docker"])
        except Exception as e:
            logger.error("Failed to start docker service.", extra={'to_stdout': True})
            raise e

        logger.info("Docker service has been started successfully!", extra={'to_stdout': True})

    def container_exists(self, name: str) -> bool:
        """
        check if container exists
        """
        if self.client is not None:
            try:
                self.client.containers.get(name)
                return True
            except docker.errors.NotFound:
                return False
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        try:
            output = run_command(["docker", "ps", "-a", "--format={{.Names}}", "--filter", f"name={name}"])
            return name in output.stdout
        except CommandExecutionError:
            return False

    def remove_container(self, name: str) -> None:
        """
        remove container
        """
        if not self.container_exists(name):
            return

        logger.debug(f"Deleting container: {name}")

        if self.client is not None:
            try:
                container = self.client.containers.get(name)
                container.remove(force=True)
                return
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "rm", "-f", name])

    def clean_exited_containers(self) -> int:
        """
        clean all exited containers
        return deleted containers count
        """
        removed_count = 0

        if self.client is not None:
            try:
                exited_containers = self.client.containers.list(
                    all=True,
                    filters={'status': 'exited'}
                )

                for container in exited_containers:
                    try:
                        container.remove()
                        removed_count += 1
                        logger.debug(f"Exited container has been deleted: {container.name} ({container.id})")
                    except APIError as e:
                        logger.warning(f"Failed to delete container {container.name}: {str(e)}")
                return removed_count
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})
        try:
            cmd = ["docker", "ps", "-a", "--filter", "status=exited", "--format={{.ID}}"]
            result = run_command(cmd)

            if not result.stdout.strip():
                return 0

            container_ids = result.stdout.splitlines()

            for container_id in container_ids:
                try:
                    run_command(["docker", "rm", container_id])
                    removed_count += 1
                    logger.debug(f"Exited container has been deleted: {container_id}")
                except CommandExecutionError as e:
                    logger.warning(f"Failed to delete container {container_id}: {str(e)}", extra={'to_stdout': True})

            return removed_count
        except CommandExecutionError as e:
            logger.error(f"Failed to clean exited containers: {str(e)}")
            return 0

    def clean_all_containers(self, force: bool = False) -> int:
        """
        clean all containers including alive containers
        :param force: force clean all containers
        return count of clean containers
        """
        removed_count = 0

        if self.client is not None:
            try:
                all_containers = self.client.containers.list(all=True)

                for container in all_containers:
                    try:
                        if container.status == 'running' and not force:
                            logger.debug(f"skip running container: {container.name} (force=True force clean container)")
                            continue

                        container.remove(force=force)
                        removed_count += 1
                        logger.debug(f"already deleted container: {container.name} ({container.id})")
                    except APIError as e:
                        logger.warning(f"Failed to delete container {container.name}: {str(e)}")
                return removed_count
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})
        try:
            cmd = ["docker", "ps", "-a", "--format={{.ID}}"]
            result = run_command(cmd)

            if not result.stdout.strip():
                return 0

            container_ids = result.stdout.splitlines()

            for container_id in container_ids:
                try:
                    inspect_cmd = ["docker", "inspect", "--format={{.State.Running}}", container_id]
                    inspect_result = run_command(inspect_cmd)

                    if inspect_result.stdout.strip() == 'true' and not force:
                        logger.debug(f"skip running container: {container_id} (force=True force clean container)")
                        continue

                    run_command(["docker", "rm", "-f" if force else "", container_id])
                    removed_count += 1
                    logger.debug(f"already deleted container: {container_id}")
                except CommandExecutionError as e:
                    logger.warning(f"Failed to delete container {container_id}: {str(e)}")

            return removed_count
        except CommandExecutionError as e:
            logger.error(f"Failed to clean all containers: {str(e)}")
            return 0

    def run_container(self, image: str, name: str, **kwargs) -> str:
        """
        run temporary container and return container id
        """
        self.remove_container(name)

        if self.client is not None:
            try:
                ports = {}
                volumes = {}
                environment = {}

                for k, v in kwargs.items():
                    key = k.replace('_', '-')
                    if key == 'publish':
                        # handle container port map
                        if isinstance(v, list):
                            for port in v:
                                parts = port.split(':')
                                if len(parts) == 2:
                                    ports[f"{parts[1]}/tcp"] = int(parts[0])
                    elif key == 'volume':
                        # handle container volume map
                        if isinstance(v, list):
                            for vol in v:
                                parts = vol.split(':')
                                if len(parts) >= 2:
                                    volumes[parts[0]] = {'bind': parts[1], 'mode': 'rw'}
                    elif key == 'env':
                        # handle container environment
                        if isinstance(v, list):
                            for env in v:
                                if '=' in env:
                                    parts = env.split('=', 1)
                                    environment[parts[0]] = parts[1]

                container = self.client.containers.run(
                    image=image,
                    name=name,
                    detach=True,
                    ports=ports,
                    volumes=volumes,
                    environment=environment,
                    remove=False
                )
                return container.id
            except APIError as e:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        cmd = ["docker", "run", "-d", "--name", name]
        for k, v in kwargs.items():
            if v is not None:
                cmd.extend([f"--{k.replace('_', '-')}", str(v)])
        cmd.append(image)

        result = run_command(cmd)
        return result.stdout.strip()

    def check_container_exists(self, container_name: str) -> bool:
        """
        check whether container exists
        :param container_name: container name or id
        :return: return true if container exists, else return false
        """
        if self.client is not None:
            try:
                self.client.containers.get(container_name)
                logger.debug(f"Container '{container_name}' exists")
                return True
            except docker.errors.NotFound:
                logger.debug(f"Container '{container_name}' not found")
                return False
            except APIError as e:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        try:
            result = run_command([
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^{container_name}$",
                "--format",
                "{{.Names}}"
            ])
            exists = container_name in result.stdout
            logger.debug(f"Container '{container_name}' {'exists' if exists else 'not found'}")
            return exists
        except CommandExecutionError as e:
            logger.error(f"Failed to check container status: {str(e)}", extra={'to_stdout': True})
            return False

    def copy_from_container(self, container: str, src: str, dest: str) -> None:
        """
        copy file from container src to host dest
        Args:
            container: container name or id
            src: source path in container
            dest: destination path in host

        Raises:
            RuntimeError:
        """
        dest_path = Path(dest)
        if not dest_path.parent.exists():
            dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            run_command(["docker", "cp", f"{container}:{src}", dest])
        except CommandExecutionError as e:
            raise RuntimeError(f"Failed to copy file from container src to host dest: {str(e)}")

    def pull_image(self, image: str) -> None:
        """
        pull image from registry
        """
        if self.client is not None:
            try:
                logger.info(f"Pulling image: {image}", extra={'to_stdout': True})
                self.client.images.pull(image)
                logger.info(f"{image} has been pulled successfully", extra={'to_stdout': True})
                return
            except APIError as e:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "pull", image])

    def save_image(self, image: str, output: str) -> None:
        """
        save image to tar
        """
        if self.client is not None:
            try:
                image_obj = self.client.images.get(image)
                with open(output, 'wb') as f:
                    for chunk in image_obj.save():
                        f.write(chunk)
                return
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "save", "-o", output, image])

    def load_image(self, input_file: str) -> None:
        """
        load image from tar file
        """
        if self.client is not None:
            try:
                with open(input_file, 'rb') as f:
                    self.client.images.load(f.read())
                return
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "load", "-i", input_file])

    def tag_image(self, src: str, dest: str) -> None:
        """
        tag image from src to dest
        """
        if self.client is not None:
            try:
                image = self.client.images.get(src)
                image.tag(dest)
                return
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "tag", src, dest])

    def push_image(self, image: str) -> None:
        """
        push image to registry
        """
        if self.client is not None:
            try:
                logger.info(f"Pushing image: {image}", extra={'to_stdout': True})
                for line in self.client.images.push(image, stream=True, decode=True):
                    if 'status' in line:
                        logger.debug(line['status'])
                logger.info(f"{image} has been pushed successfully", extra={'to_stdout': True})
                return
            except APIError as e:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "push", image])

    def list_containers(self, all: bool = False) -> List[Dict[str, str]]:
        """
        fetch all containers
        """
        if self.client is not None:
            try:
                containers = self.client.containers.list(all=all)
                return [{
                    'id': c.id,
                    'name': c.name,
                    'status': c.status,
                    'image': c.image.tags[0] if c.image.tags else c.image.id
                } for c in containers]
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        try:
            output = run_command(["docker", "ps", "-a", "--format={{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"])
            containers = []
            for line in output.stdout.splitlines():
                if line.strip():
                    parts = line.split('|')
                    if len(parts) >= 4:
                        containers.append({
                            'id': parts[0],
                            'name': parts[1],
                            'status': parts[2],
                            'image': parts[3]
                        })
            return containers
        except CommandExecutionError:
            return []

    def get_container_logs(self, container: str, tail: int = 100) -> str:
        """
        fetch logs from container
        """
        if self.client is not None:
            try:
                container_obj = self.client.containers.get(container)
                return container_obj.logs(tail=tail).decode('utf-8')
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        try:
            output = run_command(["docker", "logs", "--tail", str(tail), container])
            return output.stdout
        except CommandExecutionError:
            return ""

    def image_exists(self, image: str) -> bool:
        """
        check if image exists
        """
        if self.client is not None:
            try:
                self.client.images.get(image)
                return True
            except ImageNotFound:
                return False
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        try:
            run_command(["docker", "image", "inspect", image])
            return True
        except CommandExecutionError:
            return False

    def remove_image(self, image: str) -> None:
        """
        remove image from registry
        """
        if not self.image_exists(image):
            return

        logger.debug(f"Deleting image: {image}")

        if self.client is not None:
            try:
                self.client.images.remove(image, force=True)
                return
            except APIError:
                logger.warning("Docker SDK got wrong, roll back to docker command", extra={'to_stdout': True})

        run_command(["docker", "rmi", "-f", image])

    def set_docker_proxy(self, host, port, no_proxy: Optional[List[str]] = None) -> None:
        """
        Set Docker proxy
        """
        if not self.is_docker_installed:
            raise RuntimeError("Docker is not installed or not running")

        conf_file = self.docker_proxy_dir / "http_proxy.conf"

        # Prepare NO_PROXY list
        no_proxy_defaults = [
            "localhost",
            "127.0.0.1",
            ".local",
            ".internal",
            "registry.talkschool.cn:5000"
        ]
        no_proxy_all = no_proxy_defaults + (no_proxy or [])
        no_proxy_str = ",".join(sorted(set(no_proxy_all)))

        # Write config
        conf_file.parent.mkdir(parents=True, exist_ok=True)
        conf_file.write_text(f"""
[Service]
Environment="HTTP_PROXY=http://{host}:{port}/"
Environment="HTTPS_PROXY=http://{host}:{port}/"
Environment="NO_PROXY={no_proxy_str}"
""")

        # Reload and restart Docker
        run_command(["systemctl", "daemon-reload"])
        run_command(["systemctl", "restart", "docker"])

    def unset_docker_proxy(self) -> None:
        """
        Unset Docker proxy
        """
        if not self.is_docker_installed:
            raise RuntimeError("Docker is not installed or not running")

        conf_file = self.docker_proxy_dir / "http_proxy.conf"
        if conf_file.exists():
            conf_file.unlink()
            run_command(["systemctl", "daemon-reload"])
            run_command(["systemctl", "restart", "docker"])
