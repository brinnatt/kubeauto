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
        self.temp_path = Path(self.kube_constant.TEMP_PATH)

        # 初始化 Docker SDK 客户端
        self._client = None
        self._initialize_docker_client()

    def _initialize_docker_client(self):
        """初始化 Docker SDK 客户端"""
        try:
            self._client = docker.from_env()
            # 验证连接
            self._client.ping()
        except DockerException as e:
            logger.warning(f"无法初始化 Docker SDK 客户端: {str(e)}")
            self._client = None

    @property
    def client(self):
        """获取 Docker 客户端，如果 SDK 不可用则返回 None"""
        return self._client

    @property
    def is_docker_installed(self) -> bool:
        """检查 Docker 是否安装并运行"""
        if self.client is not None:
            return True

        try:
            run_command(["docker", "info"])
            return True
        except CommandExecutionError:
            return False

    def install_docker(self, version: Optional[str] = None) -> None:
        """安装 Docker"""
        # 这部分仍然需要使用命令行，因为 SDK 不能用于安装 Docker
        version = version or self.kube_constant.v_docker
        self._download_docker(version)
        self._install_docker_binaries(version)
        self._configure_docker(version)
        self._start_docker_service(version)
        # 安装后重新初始化客户端
        self._initialize_docker_client()

    def _download_docker(self, version: str) -> None:
        """下载 Docker 二进制文件"""
        docker_tgz = self.image_dir / f"docker-{version}.tgz"
        if docker_tgz.exists():
            logger.warning("Docker 二进制文件已存在")
            return

        logger.info(f"正在下载 Docker 二进制文件，版本: {version}")
        docker_bin_url = self.kube_constant.docker_bin_url(version)

        try:
            run_command(["wget", "-c", "--no-check-certificate", docker_bin_url, "-O", str(docker_tgz)])
        except CommandExecutionError:
            run_command(["curl", "-k", "-C-", "-o", str(docker_tgz), docker_bin_url])

        logger.info("Docker 二进制文件下载完成!")

    def _install_docker_binaries(self, version) -> None:
        """安装 Docker 二进制文件"""
        logger.info(f"正在安装 Docker 二进制文件，版本: {version}")

        self.temp_path.mkdir(parents=True, exist_ok=True)
        self.docker_bin_dir.mkdir(parents=True, exist_ok=True)

        run_command(["tar", "xf", str(self.image_dir / f"docker-{version}.tgz"), "-C", str(self.temp_path)])
        run_command(["cp", "-f", str(self.temp_path / "docker" / "*"), str(self.docker_bin_dir)])
        run_command(["ln", "-svf", str(self.docker_bin_dir / "*"), "/usr/local/bin/"])

        run_command(["rm", "-rf", str(self.temp_path / "docker")])
        logger.info("Docker 二进制文件安装完成!")

    def _configure_docker(self, version: str) -> None:
        """配置 Docker 守护进程"""
        logger.info(f"正在配置 Docker 守护进程，版本: {version}")

        # 创建 systemd 服务文件
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

        # 创建 daemon.json 配置
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

        # 如果存在 SELinux 则禁用它
        selinux_config = Path("/etc/selinux/config")
        if selinux_config.exists():
            logger.debug("正在禁用 SELinux")
            run_command(["setenforce", "0"])
            content = selinux_config.read_text()
            content = re.sub(r'^SELINUX=.*$', 'SELINUX=disabled', content, flags=re.MULTILINE)
            selinux_config.write_text(content)

        logger.info("Docker 守护进程配置完成!")

    def _start_docker_service(self, version) -> None:
        """启动并启用 Docker 服务"""
        logger.info(f"正在启动 Docker 服务，版本: {version}")

        run_command(["systemctl", "enable", "docker"])
        run_command(["systemctl", "daemon-reload"])
        run_command(["systemctl", "restart", "docker"])

        logger.info("Docker 服务启动完成!")

    def container_exists(self, name: str) -> bool:
        """检查容器是否存在"""
        if self.client is not None:
            try:
                self.client.containers.get(name)
                return True
            except docker.errors.NotFound:
                return False
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        try:
            output = run_command(["docker", "ps", "-a", "--format={{.Names}}", "--filter", f"name={name}"])
            return name in output.stdout
        except CommandExecutionError:
            return False

    def remove_container(self, name: str) -> None:
        """删除容器"""
        if not self.container_exists(name):
            return

        logger.debug(f"正在删除容器: {name}")

        if self.client is not None:
            try:
                container = self.client.containers.get(name)
                container.remove(force=True)
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "rm", "-f", name])

    def clean_exited_containers(self) -> int:
        """
        清除所有 exited 状态的容器
        返回被删除的容器数量
        """
        removed_count = 0

        if self.client is not None:
            try:
                # 使用 SDK 获取所有 exited 状态的容器
                exited_containers = self.client.containers.list(
                    all=True,
                    filters={'status': 'exited'}
                )

                for container in exited_containers:
                    try:
                        container.remove()
                        removed_count += 1
                        logger.debug(f"已删除 exited 容器: {container.name} ({container.id})")
                    except APIError as e:
                        logger.warning(f"删除容器 {container.name} 失败: {str(e)}")
                return removed_count
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行方式")

        # 低版本兼容处理，回退到命令行实现
        try:
            # 获取所有 exited 容器的 ID
            cmd = ["docker", "ps", "-a", "--filter", "status=exited", "--format={{.ID}}"]
            result = run_command(cmd)

            if not result.stdout.strip():
                return 0

            container_ids = result.stdout.splitlines()

            # 批量删除容器
            for container_id in container_ids:
                try:
                    run_command(["docker", "rm", container_id])
                    removed_count += 1
                    logger.debug(f"已删除 exited 容器: {container_id}")
                except CommandExecutionError as e:
                    logger.warning(f"删除容器 {container_id} 失败: {str(e)}")

            return removed_count
        except CommandExecutionError as e:
            logger.error(f"清理 exited 容器失败: {str(e)}")
            return 0

    def clean_all_containers(self, force: bool = False) -> int:
        """
        清除所有容器（包括运行中和已退出的）
        :param force: 是否强制删除运行中的容器
        返回被删除的容器数量
        """
        removed_count = 0

        if self.client is not None:
            try:
                # 使用 SDK 获取所有容器
                all_containers = self.client.containers.list(all=True)

                for container in all_containers:
                    try:
                        if container.status == 'running' and not force:
                            logger.debug(f"跳过运行中的容器: {container.name} (使用 force=True 可强制删除)")
                            continue

                        container.remove(force=force)
                        removed_count += 1
                        logger.debug(f"已删除容器: {container.name} ({container.id})")
                    except APIError as e:
                        logger.warning(f"删除容器 {container.name} 失败: {str(e)}")
                return removed_count
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行方式")

        # 低版本兼容处理，回退到命令行实现
        try:
            # 获取所有容器的 ID
            cmd = ["docker", "ps", "-a", "--format={{.ID}}"]
            result = run_command(cmd)

            if not result.stdout.strip():
                return 0

            container_ids = result.stdout.splitlines()

            # 批量删除容器
            for container_id in container_ids:
                try:
                    # 检查容器是否在运行
                    inspect_cmd = ["docker", "inspect", "--format={{.State.Running}}", container_id]
                    inspect_result = run_command(inspect_cmd)

                    if inspect_result.stdout.strip() == 'true' and not force:
                        logger.debug(f"跳过运行中的容器: {container_id} (使用 force=True 可强制删除)")
                        continue

                    run_command(["docker", "rm", "-f" if force else "", container_id])
                    removed_count += 1
                    logger.debug(f"已删除容器: {container_id}")
                except CommandExecutionError as e:
                    logger.warning(f"删除容器 {container_id} 失败: {str(e)}")

            return removed_count
        except CommandExecutionError as e:
            logger.error(f"清理所有容器失败: {str(e)}")
            return 0

    def run_temp_container(self, image: str, name: str, **kwargs) -> str:
        """运行临时容器并返回其 ID"""
        self.remove_container(name)

        # 尝试使用 SDK
        if self.client is not None:
            try:
                # 转换参数格式
                ports = {}
                volumes = {}
                environment = {}

                for k, v in kwargs.items():
                    key = k.replace('_', '-')
                    if key == 'publish':
                        # 处理端口映射
                        if isinstance(v, list):
                            for port in v:
                                parts = port.split(':')
                                if len(parts) == 2:
                                    ports[f"{parts[1]}/tcp"] = int(parts[0])
                    elif key == 'volume':
                        # 处理卷映射
                        if isinstance(v, list):
                            for vol in v:
                                parts = vol.split(':')
                                if len(parts) >= 2:
                                    volumes[parts[0]] = {'bind': parts[1], 'mode': 'rw'}
                    elif key == 'env':
                        # 处理环境变量
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
                logger.warning(f"使用 SDK 运行容器失败: {str(e)}，回退到命令行")

        # 低版本兼容处理，回退到命令行实现
        cmd = ["docker", "run", "-d", "--name", name]
        for k, v in kwargs.items():
            if v is not None:
                cmd.extend([f"--{k.replace('_', '-')}", str(v)])
        cmd.append(image)

        result = run_command(cmd)
        return result.stdout.strip()

    def copy_from_container(self, container: str, src: str, dest: str) -> None:
        """从容器复制文件到主机"""
        if self.client is not None:
            try:
                container_obj = self.client.containers.get(container)
                data, stat = container_obj.get_archive(src)

                with open(dest, 'wb') as f:
                    for chunk in data:
                        f.write(chunk)
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "cp", f"{container}:{src}", dest])

    def pull_image(self, image: str) -> None:
        """拉取 Docker 镜像"""
        if self.client is not None:
            try:
                logger.info(f"正在拉取镜像: {image}")
                self.client.images.pull(image)
                logger.info(f"镜像 {image} 拉取完成")
                return
            except APIError as e:
                logger.warning(f"使用 SDK 拉取镜像失败: {str(e)}，回退到命令行")

        run_command(["docker", "pull", image])

    def save_image(self, image: str, output: str) -> None:
        """将 Docker 镜像保存为 tar 文件"""
        if self.client is not None:
            try:
                image_obj = self.client.images.get(image)
                with open(output, 'wb') as f:
                    for chunk in image_obj.save():
                        f.write(chunk)
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "save", "-o", output, image])

    def load_image(self, input_file: str) -> None:
        """从 tar 文件加载 Docker 镜像"""
        if self.client is not None:
            try:
                with open(input_file, 'rb') as f:
                    self.client.images.load(f.read())
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "load", "-i", input_file])

    def tag_image(self, src: str, dest: str) -> None:
        """为 Docker 镜像打标签"""
        if self.client is not None:
            try:
                image = self.client.images.get(src)
                image.tag(dest)
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "tag", src, dest])

    def push_image(self, image: str) -> None:
        """推送 Docker 镜像到仓库"""
        if self.client is not None:
            try:
                logger.info(f"正在推送镜像: {image}")
                for line in self.client.images.push(image, stream=True, decode=True):
                    if 'status' in line:
                        logger.debug(line['status'])
                logger.info(f"镜像 {image} 推送完成")
                return
            except APIError as e:
                logger.warning(f"使用 SDK 推送镜像失败: {str(e)}，回退到命令行")

        run_command(["docker", "push", image])

    # 新增功能：获取容器列表
    def list_containers(self, all: bool = False) -> List[Dict[str, str]]:
        """获取容器列表"""
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
                logger.warning("Docker SDK 出错，回退到命令行")

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

    # 新增功能：获取容器日志
    def get_container_logs(self, container: str, tail: int = 100) -> str:
        """获取容器日志"""
        if self.client is not None:
            try:
                container_obj = self.client.containers.get(container)
                return container_obj.logs(tail=tail).decode('utf-8')
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        try:
            output = run_command(["docker", "logs", "--tail", str(tail), container])
            return output.stdout
        except CommandExecutionError:
            return ""

    # 新增功能：检查镜像是否存在
    def image_exists(self, image: str) -> bool:
        """检查镜像是否存在"""
        if self.client is not None:
            try:
                self.client.images.get(image)
                return True
            except ImageNotFound:
                return False
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        try:
            run_command(["docker", "image", "inspect", image])
            return True
        except CommandExecutionError:
            return False

    # 新增功能：删除镜像
    def remove_image(self, image: str) -> None:
        """删除镜像"""
        if not self.image_exists(image):
            return

        logger.debug(f"正在删除镜像: {image}")

        if self.client is not None:
            try:
                self.client.images.remove(image, force=True)
                return
            except APIError:
                logger.warning("Docker SDK 出错，回退到命令行")

        run_command(["docker", "rmi", "-f", image])