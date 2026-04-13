#!/usr/bin/env python3
"""
Apache Kafka KRaft 部署与运维：生成配置与 systemd、调用发行版 bin/ 下工具。

文档一致性（须与代码同步维护，避免用户可见说明与行为不一致）：
  用户可见说明分散在：本段、下方 KAFKACLI_PARSER_DESCRIPTION、show_examples() 分步示例、argparse 各选项 help、
  kafka.json.example 注释与 deployment_note。行为以 main() 分支及 _resolve_kafka_client_config、
  KafkaDeployer、_run_remote_deploy 等实现为准；修改其一须核对其它各处。JSON 配置键名与命令行长选项对应（下划线）。

常用环境变量：
  KAFKA_CLI_TIMEOUT、KAFKA_LOG_DIR、KAFKA_ADVERTISED_HOST（生产请设为对客户端可达的本机地址或 DNS）、KAFKA_CLI_ASSUME_VERSION
  KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD（或 KAFKA_USER / KAFKA_PASSWORD）、KAFKA_SASL_MECHANISM
  KAFKA_CLI_COMMAND_CONFIG 或 KAFKA_COMMAND_CONFIG：显式客户端 properties（优先级见下「客户端认证」）
  自建 PKI：KAFKA_SSL_KEYSTORE_PATH、KAFKA_SSL_KEYSTORE_PASSWORD、KAFKA_SSL_TRUSTSTORE_PATH、KAFKA_SSL_TRUSTSTORE_PASSWORD（与 --ssl-* 等价）

客户端认证（与 KAFKACLI_PARSER_DESCRIPTION、§6 分步示例一致；实现见 _resolve_kafka_client_config）：
  --command-config 或 KAFKA_CLI_COMMAND_CONFIG 或 KAFKA_COMMAND_CONFIG
  优于 --kafka-user + --kafka-password 或 KAFKA_SASL_* / KAFKA_USER / KAFKA_PASSWORD
  优于 ${kafka_home}/config/kafkacli.client.properties（由 --deploy-sasl-plain / --deploy-sasl-ssl 等写入时）。

KRaft 节点编号（与 KAFKACLI_PARSER_DESCRIPTION【KRaft 节点编号】、--node-id 帮助一致）：
  多节点时 node.id（--node-id / JSON 的 node_id）须在全集群全部 controller 与 broker 间全局唯一；脚本不代为校验其它主机上的已占用编号。

验收：部署后可用 --verify；standalone/broker 侧重业务监听与 show_cluster_status；controller 侧重 kafka-metadata-quorum describe --status。日常可用 kafkacli --status --kafka-home …。

部署注意：systemd 以 --user/--group（默认 kafka）运行时，须能对 ${KAFKA_HOME}/logs、log.dirs 写入；server.properties 中 ssl.*、keystore 等路径须对该用户可读。脚本在 storage format 后 chown 数据目录；在执行 systemctl restart 之前处理 ${KAFKA_HOME}/logs 与 config/kafkacli.client.properties（0600 会 chown 给服务用户）。

调用 bin 工具时：含 --command-config 的命令统一为「脚本 → 可选 --command-config → --bootstrap-server → 其余参数」（见 _kafka_cli_cmd），避免子命令解析错误。

部署前置与失败回滚（与 KAFKACLI_PARSER_DESCRIPTION【部署前置与失败回滚】、deploy_* 实现一致）：
  · 写入生成配置前：对本次涉及的 metadata.log.dir 与 log.dirs 各根路径检查 meta.properties 中 cluster.id 是否一致；不一致则拒绝部署。
  · standalone/controller：cluster.id 须三选一（--cluster-id / --generate-cluster-id / --use-disk-cluster-id）；数据路径与 node.id 须显式给出，无隐式默认目录。
  · kafka-storage format 失败：若本次运行前不存在该生成配置文件，则删除刚写入的该文件，避免残留配置与后续集群意图冲突；若文件本就存在（幂等覆盖），则保留并打警告。
  · format 已成功但 systemd 或监听探测失败：磁盘上可能已有元数据，须用 --clean / --clean-data 等按文档处理，脚本不自动删除数据目录。

幂等约定（与 KAFKACLI_PARSER_DESCRIPTION「清理与幂等」、各 argparse 分组说明一致）：
  · --deploy：重复执行覆盖生成配置、kafka-storage format（--ignore-formatted）、chown、systemctl restart。
  · --topic-create / --topic-delete：Topic 已存在/已不存在时按 CLI 输出匹配后视为成功（非「改分区/副本」的广义收敛）。
  · --status / --metrics 系列：只读，天然可重复。
  · --clean：卸载（停服务、删 unit 与生成配置），非部署；抹数据用 --clean-data 或与 --clean/--clean-first 联用 --force。

退出码：0 成功，1 失败。
"""

import argparse
import atexit
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

# 日志配置常量（环境变量 KAFKA_LOG_DIR 未设置时为当前工作目录下的 logs）
LOG_DIR = os.getenv("KAFKA_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "kafka_deploy.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Kafka 默认端口（发行版惯例）
DEFAULT_BROKER_PORT = 9092
DEFAULT_CONTROLLER_PORT = 9093

# 部署 SASL/PLAIN（等）成功后写入 ${kafka_home}/config/，供后续子命令默认使用（0600）
KAFKACLI_CLIENT_PROPERTIES_NAME = "kafkacli.client.properties"

# 进程退出码：0 成功，1 失败
EXIT_OK = 0
EXIT_ERROR = 1


def _kafka_cli_timeout_sec(default: int = 120) -> int:
    """子进程超时秒数；环境变量 KAFKA_CLI_TIMEOUT（10～86400）覆盖 default。"""
    try:
        v = int(os.getenv("KAFKA_CLI_TIMEOUT", str(default)))
        return max(10, min(86400, v))
    except ValueError:
        return default


def _reassign_cmd_timeout_sec() -> int:
    """--execute / --verify 子进程超时：max(KAFKA_CLI_TIMEOUT, 3600)。"""
    return max(_kafka_cli_timeout_sec(120), 3600)


def _validate_bootstrap_server(bootstrap_server: str) -> Tuple[bool, str]:
    """校验 --bootstrap-server 非空且主机/端口可解析（减少 silent 失败）。"""
    s = (bootstrap_server or "").strip()
    if not s:
        return False, "bootstrap server 不能为空"
    if ":" in s:
        host, _, port_part = s.rpartition(":")
        host = host.strip()
        port_part = port_part.strip()
        if port_part:
            try:
                p = int(port_part)
                if not InputValidator.validate_port(p):
                    return False, f"bootstrap 端口无效: {port_part}"
            except ValueError:
                return False, f"bootstrap 端口无效: {port_part}"
        if host and not InputValidator.validate_hostname(host):
            return False, f"bootstrap 主机无效: {host}"
    else:
        if not InputValidator.validate_hostname(s):
            return False, f"bootstrap 主机无效: {s}"
    return True, ""


def _validate_command_config_path(command_config: Optional[str]) -> Optional[str]:
    """若提供 SASL/SSL 客户端配置，文件须存在。"""
    if not (command_config or "").strip():
        return None
    p = Path(command_config).expanduser()
    if not p.is_file():
        return f"--command-config 文件不存在或不可读: {command_config}"
    return None


def _parse_simple_kafka_properties_file(config_path: Path) -> Dict[str, str]:
    """
    读取 Kafka 风格 key=value 行（忽略空行与 # 注释；不处理续行）。
    用于清理前从待删配置中解析 log.dirs / metadata.log.dir。
    """
    out: Dict[str, str] = {}
    if not config_path.is_file():
        return out
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _merge_config_or_cli_path(from_file: Optional[str], from_cli: Optional[str]) -> str:
    """命令行非空优先，否则用配置文件中的值。"""
    c = (from_cli or "").strip()
    if c:
        return c
    return (from_file or "").strip()


def _csv_dirs_to_abs_paths(csv: str) -> List[str]:
    """log.dirs 逗号分隔为多目录，每项展开为绝对路径字符串（去重保序）。"""
    seen: set = set()
    out: List[str] = []
    for part in (csv or "").split(","):
        p = part.strip()
        if not p:
            continue
        ap = str(Path(p).expanduser().resolve())
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def _rm_rf_kraft_data_dir(path_str: str, label: str) -> None:
    """
    删除整块 KRaft 数据目录（与 log.dirs / metadata.log.dir 根目录一致）。
    拒绝删除根文件系统或明显不安全的极短路径。
    """
    root = Path(path_str).expanduser()
    try:
        resolved = root.resolve()
    except OSError as ex:
        logger.warning("跳过删除 %s（%s）：路径无效 — %s", label, path_str, ex, extra={"to_stdout": True})
        return
    s = str(resolved)
    if resolved == Path("/"):
        logger.error(
            "拒绝删除数据目录 %s：不能指定文件系统根路径。",
            label,
            extra={"to_stdout": True},
        )
        raise ValueError("unsafe data path: /")
    if not resolved.exists():
        logger.info("数据目录不存在，跳过删除: %s (%s)", label, s, extra={"to_stdout": True})
        return
    if not resolved.is_dir():
        logger.warning("数据路径不是目录，跳过删除: %s (%s)", label, s, extra={"to_stdout": True})
        return
    shutil.rmtree(resolved)
    logger.info("✓ 已删除 KRaft 数据目录 [%s]: %s", label, s, extra={"to_stdout": True})


def _read_cluster_id_from_meta_at_root(root: str) -> Optional[str]:
    """读取单个数据根目录下 meta.properties 的 cluster.id（若不存在或无键则 None）。"""
    if not (root or "").strip():
        return None
    try:
        base = Path(root).expanduser().resolve()
    except OSError:
        return None
    meta = base / "meta.properties"
    if not meta.is_file():
        return None
    try:
        text = meta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("cluster.id="):
            cid = s.split("=", 1)[1].strip()
            return cid if cid else None
    return None


def _read_cluster_id_from_meta_properties(data_dir_csv: str) -> Optional[str]:
    """
    读取 KRaft 数据目录下 meta.properties 的 cluster.id（若存在）。
    log.dirs 仅取逗号分隔的第一段路径（与历史行为一致）；多路径一致性请用 _audit_cluster_ids_across_kraft_roots。
    """
    first = (data_dir_csv or "").split(",")[0].strip()
    if not first:
        return None
    return _read_cluster_id_from_meta_at_root(first)


def _dedupe_paths_stable(paths: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _audit_cluster_ids_across_kraft_roots(roots: List[str]) -> Tuple[bool, str]:
    """
    前置检测：多个数据根路径下若均存在 meta.properties，则 cluster.id 须一致，否则拒绝部署。
    用于避免 standalone 残留、默认 log.dirs 等与本次 metadata.log.dir 混用导致 format 失败。
    """
    roots = _dedupe_paths_stable(roots)
    mp: Dict[str, Optional[str]] = {}
    for r in roots:
        mp[r] = _read_cluster_id_from_meta_at_root(r)
    vals = {v for v in mp.values() if v}
    if len(vals) <= 1:
        return True, ""
    lines = [f"  {p} -> cluster.id={mp[p]!r}" for p in sorted(mp.keys()) if mp[p]]
    return False, (
        "多个 KRaft 数据目录上的 cluster.id 不一致，拒绝部署。"
        "请先清空无关目录、或删除冲突的 meta.properties 后再执行（与 --clean / --clean-data 说明一致）。\n"
        + "\n".join(lines)
    )


def _consolidate_existing_cluster_id_from_roots(roots: List[str]) -> Optional[str]:
    """在已通过 _audit_cluster_ids_across_kraft_roots 后，返回唯一的已有 cluster.id（或 None）。"""
    roots = _dedupe_paths_stable(roots)
    cids = [_read_cluster_id_from_meta_at_root(r) for r in roots]
    nonempty = [c for c in cids if c]
    if not nonempty:
        return None
    return nonempty[0]


def _rollback_new_deploy_config(config_path: Path, had_existed_before_deploy: bool) -> None:
    """
    首次部署失败时删除本次生成的 properties，避免残留配置与下次集群意图不一致。
    若配置文件在部署前已存在（幂等覆盖），则保留并仅打日志，由运维自行恢复或再次部署。
    """
    if had_existed_before_deploy:
        logger.warning(
            "本次部署未成功：保留已存在的配置文件 %s（若本次已覆盖内容，请自行从备份恢复或再次修正后部署）。",
            config_path,
            extra={"to_stdout": True},
        )
        return
    try:
        config_path.unlink(missing_ok=True)
        logger.info("已删除本次生成的配置文件（首次部署 kafka-storage 等步骤失败时的回滚）: %s", config_path, extra={"to_stdout": True})
    except OSError as ex:
        logger.warning("回滚删除配置文件失败 %s: %s", config_path, ex, extra={"to_stdout": True})


_TMP_CLIENT_CONFIG_FILES: List[str] = []


def _cleanup_temp_client_configs() -> None:
    for path in _TMP_CLIENT_CONFIG_FILES:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup_temp_client_configs)


def _jaas_escape(val: str) -> str:
    return (val or "").replace("\\", "\\\\").replace('"', '\\"')


def _build_kafka_client_properties_content(username: str, password: str, mechanism: str) -> str:
    """生成 Kafka 客户端 properties 文本（SASL_PLAINTEXT；与 bin 工具 --command-config 格式一致）。"""
    mech = (mechanism or "PLAIN").strip().upper()
    u, p = _jaas_escape(username), _jaas_escape(password)
    if mech == "PLAIN":
        jaas = (
            "org.apache.kafka.common.security.plain.PlainLoginModule required "
            f'username="{u}" password="{p}";'
        )
        return (
            "security.protocol=SASL_PLAINTEXT\n"
            "sasl.mechanism=PLAIN\n"
            f"sasl.jaas.config={jaas}\n"
        )
    if mech in ("SCRAM-SHA-256", "SCRAM-SHA-512"):
        jaas = (
            "org.apache.kafka.common.security.scram.ScramLoginModule required "
            f'username="{u}" password="{p}";'
        )
        return (
            "security.protocol=SASL_PLAINTEXT\n"
            f"sasl.mechanism={mech}\n"
            f"sasl.jaas.config={jaas}\n"
        )
    raise ValueError(f"不支持的 SASL 机制: {mech}（支持 PLAIN、SCRAM-SHA-256、SCRAM-SHA-512）")


def _make_temp_client_properties_file(content: str) -> str:
    fd, path = tempfile.mkstemp(prefix="kafkacli-", suffix=".client.properties", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    _TMP_CLIENT_CONFIG_FILES.append(path)
    return path


def _persist_kafkacli_client_properties(kafka_home: Path, content: str) -> str:
    """写入 ${kafka_home}/config/kafkacli.client.properties，权限 0600。"""
    p = kafka_home / "config" / KAFKACLI_CLIENT_PROPERTIES_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    logger.info("已写入默认客户端配置（后续命令可省略密码）: %s", p, extra={"to_stdout": True})
    return str(p)


def _broker_plain_jaas(username: str, password: str) -> str:
    """Broker 侧 PlainLoginModule（单节点：broker 身份与业务用户同一账号）。user_<name> 与登录名一致。"""
    uq, pq = _jaas_escape(username), _jaas_escape(password)
    return (
        "org.apache.kafka.common.security.plain.PlainLoginModule required "
        f'username="{uq}" password="{pq}" user_{username}="{pq}";'
    )


def _validate_sasl_plain_username(username: str) -> Optional[str]:
    if not (username or "").strip():
        return "SASL 用户名为空"
    if not re.match(r"^[a-zA-Z0-9._-]+$", username):
        return "SASL/PLAIN 用户名仅允许字母、数字、._-（避免 JAAS 解析歧义）"
    return None


def _infer_ssl_store_type(path: str) -> str:
    """根据扩展名推断 JKS 或 PKCS12（自建 PKI 常见产物）。"""
    p = (path or "").lower()
    if p.endswith((".p12", ".pfx")):
        return "PKCS12"
    return "JKS"


def _ssl_stack_properties(
        keystore_path: str,
        keystore_password: str,
        key_password: str,
        truststore_path: str,
        truststore_password: str,
) -> Dict[str, str]:
    """Broker 侧 ssl.*（与 Kafka 文档中 SSL 配置项一致；自建 CA 签发 keystore/truststore）。"""
    return {
        "ssl.keystore.type": _infer_ssl_store_type(keystore_path),
        "ssl.keystore.location": keystore_path,
        "ssl.keystore.password": keystore_password,
        "ssl.key.password": key_password,
        "ssl.truststore.type": _infer_ssl_store_type(truststore_path),
        "ssl.truststore.location": truststore_path,
        "ssl.truststore.password": truststore_password,
        "ssl.client.auth": "none",
        "ssl.endpoint.identification.algorithm": "https",
    }


def _build_kafka_client_sasl_ssl_content(
        username: str,
        password: str,
        mechanism: str,
        truststore_path: str,
        truststore_password: str,
) -> str:
    """客户端 SASL_SSL + 信任自建 CA 的 truststore（与 broker ssl.truststore 同源或同 CA）。"""
    mech = (mechanism or "PLAIN").strip().upper()
    u, p = _jaas_escape(username), _jaas_escape(password)
    if mech == "PLAIN":
        jaas = (
            "org.apache.kafka.common.security.plain.PlainLoginModule required "
            f'username="{u}" password="{p}";'
        )
    elif mech in ("SCRAM-SHA-256", "SCRAM-SHA-512"):
        jaas = (
            "org.apache.kafka.common.security.scram.ScramLoginModule required "
            f'username="{u}" password="{p}";'
        )
    else:
        raise ValueError(f"不支持的 SASL 机制: {mech}")
    ts_type = _infer_ssl_store_type(truststore_path)
    return (
        "security.protocol=SASL_SSL\n"
        f"sasl.mechanism={mech}\n"
        f"sasl.jaas.config={jaas}\n"
        f"ssl.truststore.type={ts_type}\n"
        f"ssl.truststore.location={truststore_path}\n"
        f"ssl.truststore.password={truststore_password}\n"
        "ssl.endpoint.identification.algorithm=https\n"
    )


def _kraft_combined_sasl_ssl_listener_properties(
        username: str, password: str, ssl_stack: Dict[str, str]
) -> Dict[str, str]:
    """KRaft combined：对外 SASL_SSL + PLAIN + ssl.*；Quorum 仍为 PLAINTEXT。"""
    broker_port = DEFAULT_BROKER_PORT
    ctrl_port = DEFAULT_CONTROLLER_PORT
    adv_host = _advertised_host()
    ls = f"SASL_SSL://0.0.0.0:{broker_port},CONTROLLER://0.0.0.0:{ctrl_port}"
    jaas = _broker_plain_jaas(username, password)
    out: Dict[str, str] = {
        "listeners": ls,
        "advertised.listeners": (
            f"SASL_SSL://{adv_host}:{broker_port},CONTROLLER://{adv_host}:{ctrl_port}"
        ),
        "controller.quorum.bootstrap.servers": f"{adv_host}:{ctrl_port}",
        "controller.listener.names": "CONTROLLER",
        "inter.broker.listener.name": "SASL_SSL",
        "listener.security.protocol.map": "CONTROLLER:PLAINTEXT,SASL_SSL:SASL_SSL",
        "sasl.enabled.mechanisms": "PLAIN",
        "sasl.mechanism.inter.broker.protocol": "PLAIN",
        "listener.name.sasl_ssl.plain.sasl.jaas.config": jaas,
    }
    out.update(ssl_stack)
    return out


def _kraft_broker_sasl_ssl_listener_properties(username: str, password: str, ssl_stack: Dict[str, str]) -> Dict[str, str]:
    """KRaft broker-only：SASL_SSL + PLAIN + ssl.*。"""
    adv_host = _advertised_host()
    broker_port = DEFAULT_BROKER_PORT
    ls = f"SASL_SSL://0.0.0.0:{broker_port}"
    jaas = _broker_plain_jaas(username, password)
    out: Dict[str, str] = {
        "listeners": ls,
        "advertised.listeners": f"SASL_SSL://{adv_host}:{broker_port}",
        "inter.broker.listener.name": "SASL_SSL",
        "listener.security.protocol.map": "SASL_SSL:SASL_SSL",
        "sasl.enabled.mechanisms": "PLAIN",
        "sasl.mechanism.inter.broker.protocol": "PLAIN",
        "listener.name.sasl_ssl.plain.sasl.jaas.config": jaas,
    }
    out.update(ssl_stack)
    return out


def _kraft_combined_sasl_plain_listener_properties(username: str, password: str) -> Dict[str, str]:
    """KRaft combined：SASL_PLAINTEXT + PLAIN（与 _kraft_combined_listener_properties 同结构）。"""
    broker_port = DEFAULT_BROKER_PORT
    ctrl_port = DEFAULT_CONTROLLER_PORT
    adv_host = _advertised_host()
    ls = f"SASL_PLAINTEXT://0.0.0.0:{broker_port},CONTROLLER://0.0.0.0:{ctrl_port}"
    jaas = _broker_plain_jaas(username, password)
    return {
        "listeners": ls,
        "advertised.listeners": (
            f"SASL_PLAINTEXT://{adv_host}:{broker_port},CONTROLLER://{adv_host}:{ctrl_port}"
        ),
        "controller.quorum.bootstrap.servers": f"{adv_host}:{ctrl_port}",
        "controller.listener.names": "CONTROLLER",
        "inter.broker.listener.name": "SASL_PLAINTEXT",
        "listener.security.protocol.map": "CONTROLLER:PLAINTEXT,SASL_PLAINTEXT:SASL_PLAINTEXT",
        "sasl.enabled.mechanisms": "PLAIN",
        "sasl.mechanism.inter.broker.protocol": "PLAIN",
        "listener.name.sasl_plaintext.plain.sasl.jaas.config": jaas,
    }


def _normalize_topic_name(topic: str) -> str:
    return (topic or "").strip()


def _validate_topic_name(topic: str) -> Tuple[bool, str]:
    """Topic 名非空且长度 ≤249（与 kafka 日志目录 topic-partition 命名长度约束一致）。"""
    t = _normalize_topic_name(topic)
    if not t:
        return False, "Topic 名称不能为空"
    if len(t) > 249:
        return False, "Topic 名称长度须 ≤ 249"
    return True, ""


def _cli_already_exists(msg: str) -> bool:
    """对 stderr 字符串做子串匹配（本脚本实现）；判定以 kafka-topics.sh 实际退出码与输出为准。"""
    m = (msg or "").lower()
    return "already exists" in m or "already exist" in m


def _cli_topic_missing(msg: str) -> bool:
    """对 stderr 字符串做子串匹配（本脚本实现）；判定以 kafka-topics.sh 实际退出码与输出为准。"""
    m = (msg or "").lower()
    return (
        "unknown topic" in m
        or "does not exist" in m
        or "not found" in m
    )


def _advertised_host() -> str:
    """读取环境变量 KAFKA_ADVERTISED_HOST；未设置时为 127.0.0.1。"""
    return (os.getenv("KAFKA_ADVERTISED_HOST") or "127.0.0.1").strip() or "127.0.0.1"


class CommandExecutionError(Exception):
    """命令执行异常；message 可通过 str(e) 或 e.message 读取。"""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def setup_logger(
        name: str = "kafka_deploy",
        log_file: Optional[str] = None,
        level: int = DEFAULT_LOG_LEVEL,
        fmt: str = DEFAULT_FORMAT,
        datefmt: str = DEFAULT_DATEFMT,
        handlers: Optional[List[logging.Handler]] = None
) -> logging.Logger:
    """
    设置日志记录器（与 StarCli 一致：先确保日志目录存在，再挂 RotatingFileHandler + 按需 stdout）。
    首次调用且未传 handlers 时使用默认：文件（UTF-8、轮转）+ stdout；
    若 logger 已有 handler 则直接返回。日志目录由环境变量 KAFKA_LOG_DIR 或默认 LOG_DIR 决定。
    """
    if handlers is None:
        try:
            if not os.path.exists(LOG_DIR):
                os.makedirs(LOG_DIR, exist_ok=True)
        except OSError as e:
            print(f"Warning: 无法创建日志目录 {LOG_DIR}: {e}", file=sys.stderr)

    log = logging.getLogger(name)
    log.setLevel(level)

    if log.hasHandlers():
        return log

    formatter = logging.Formatter(fmt, datefmt)

    if handlers is None:
        try:
            log_file = log_file or DEFAULT_LOG_FILE
            file_handler = RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            file_handler.addFilter(lambda record: not getattr(record, "skip_file", False))
            log.addHandler(file_handler)
        except OSError as e:
            print(f"Warning: 日志目录不可用，仅输出到 stdout: {e}", file=sys.stderr)

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(level)
        stdout_handler.addFilter(lambda record: getattr(record, "to_stdout", False))
        log.addHandler(stdout_handler)
    else:
        for handler in handlers:
            handler.setLevel(level)
            if not handler.formatter:
                handler.setFormatter(formatter)
            log.addHandler(handler)

    return log


logger = setup_logger(__name__)

# 未指定 --skip-kraft-version-check 时，KRaft 自动部署最低版本（与脚本内校验一致）
KRAFT_DEPLOY_MIN_VERSION: Tuple[int, int, int] = (3, 3, 0)


def infer_kafka_release(kafka_home: Path, assume_version: Optional[str] = None) -> Optional[Tuple[int, int, int]]:
    """
    从 --assume-kafka-version / 环境变量 KAFKA_CLI_ASSUME_VERSION、安装路径名、libs/kafka-server-common-*.jar 推断
    Kafka 发行版语义版本 (major, minor, patch)；均失败时返回 None（脚本将按最严策略：Java 17+）。
    """
    raw = (assume_version or os.getenv("KAFKA_CLI_ASSUME_VERSION") or "").strip()
    if raw:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        logger.warning(
            "KAFKA_CLI_ASSUME_VERSION / --assume-kafka-version 须为 x.y.z，已忽略: %s",
            raw,
            extra={"to_stdout": True},
        )

    try:
        resolved = str(kafka_home.resolve())
    except OSError:
        resolved = str(kafka_home)

    m = re.search(r"kafka_2\.\d+-(\d+)\.(\d+)\.(\d+)", resolved, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    base = kafka_home.name
    m = re.match(r"kafka_2\.\d+-(\d+)\.(\d+)\.(\d+)$", base, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    libs = kafka_home / "libs"
    best_jar: Optional[Tuple[int, int, int]] = None
    if libs.is_dir():
        ver_re = re.compile(r"kafka-server-common-(\d+)\.(\d+)\.(\d+)\.jar$", re.I)
        for j in libs.glob("kafka-server-common-*.jar"):
            mj = ver_re.search(j.name)
            if mj:
                t = (int(mj.group(1)), int(mj.group(2)), int(mj.group(3)))
                if best_jar is None or t > best_jar:
                    best_jar = t
    return best_jar


def min_java_major_for_kafka(kafka_release: Optional[Tuple[int, int, int]]) -> int:
    """部署前 Java 最低主版本：major>=4 → 17；否则 11；无法推断 Kafka 版本 → 17。"""
    if kafka_release is None:
        return 17
    major, _, _ = kafka_release
    return 17 if major >= 4 else 11


def _listener_scheme_port(listeners: str, scheme: str, default_port: int) -> int:
    """从 listeners 行解析 SCHEME://host:port 中的端口（如 PLAINTEXT、CONTROLLER）。"""
    m = re.search(rf"{re.escape(scheme)}://[^:]*:(\d+)", listeners or "", re.I)
    return int(m.group(1)) if m else default_port


def _kraft_combined_listener_properties(listeners_override: Optional[str]) -> Dict[str, str]:
    """
    KRaft combined：返回 listeners、advertised.listeners、controller.quorum.bootstrap.servers、
    controller.listener.names、inter.broker.listener.name、listener.security.protocol.map。
    主机名来自 KAFKA_ADVERTISED_HOST（未设置则 127.0.0.1）。
    """
    broker_port = DEFAULT_BROKER_PORT
    ctrl_port = DEFAULT_CONTROLLER_PORT
    adv_host = _advertised_host()
    if listeners_override and listeners_override.strip():
        ls = listeners_override.strip().rstrip(",")
        if "CONTROLLER" not in ls.upper():
            ls = f"{ls},CONTROLLER://0.0.0.0:{ctrl_port}"
            logger.info(
                "listeners 未包含 CONTROLLER，已自动追加（KRaft combined 必选）",
                extra={"to_stdout": True},
            )
    else:
        ls = f"PLAINTEXT://0.0.0.0:{broker_port},CONTROLLER://0.0.0.0:{ctrl_port}"
    pm = re.search(r"PLAINTEXT://[^:]*:(\d+)", ls, re.I)
    if pm:
        broker_port = int(pm.group(1))
    cm = re.search(r"CONTROLLER://[^:]*:(\d+)", ls, re.I)
    if cm:
        ctrl_port = int(cm.group(1))
    return {
        "listeners": ls,
        "advertised.listeners": (
            f"PLAINTEXT://{adv_host}:{broker_port},CONTROLLER://{adv_host}:{ctrl_port}"
        ),
        "controller.quorum.bootstrap.servers": f"{adv_host}:{ctrl_port}",
        "controller.listener.names": "CONTROLLER",
        "inter.broker.listener.name": "PLAINTEXT",
        "listener.security.protocol.map": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
    }


def _kraft_broker_sasl_plain_listener_properties(username: str, password: str) -> Dict[str, str]:
    """KRaft broker-only：对外 SASL_PLAINTEXT + PLAIN（与 combined SASL 中 broker 侧一致）。"""
    adv_host = _advertised_host()
    broker_port = DEFAULT_BROKER_PORT
    ls = f"SASL_PLAINTEXT://0.0.0.0:{broker_port}"
    jaas = _broker_plain_jaas(username, password)
    return {
        "listeners": ls,
        "advertised.listeners": f"SASL_PLAINTEXT://{adv_host}:{broker_port}",
        "inter.broker.listener.name": "SASL_PLAINTEXT",
        "listener.security.protocol.map": "SASL_PLAINTEXT:SASL_PLAINTEXT",
        "sasl.enabled.mechanisms": "PLAIN",
        "sasl.mechanism.inter.broker.protocol": "PLAIN",
        "listener.name.sasl_plaintext.plain.sasl.jaas.config": jaas,
    }


def _kraft_broker_listener_properties(listeners_override: Optional[str]) -> Dict[str, str]:
    """
    KRaft broker-only：listeners 仅为 PLAINTEXT 且无 SSL/SASL 时补全 inter.broker.listener.name、
    listener.security.protocol.map、advertised.listeners；否则仅返回 listeners，由 extra_properties 提供完整映射。
    """
    adv_host = _advertised_host()
    ls = (listeners_override or "").strip() or f"PLAINTEXT://0.0.0.0:{DEFAULT_BROKER_PORT}"
    broker_port = DEFAULT_BROKER_PORT
    pm = re.search(r"PLAINTEXT://[^:]*:(\d+)", ls, re.I)
    if pm:
        broker_port = int(pm.group(1))
    out: Dict[str, str] = {"listeners": ls}
    if (
        re.search(r"\bPLAINTEXT://", ls, re.I)
        and "SSL" not in ls.upper()
        and "SASL" not in ls.upper()
    ):
        out["inter.broker.listener.name"] = "PLAINTEXT"
        out["listener.security.protocol.map"] = "PLAINTEXT:PLAINTEXT"
        out["advertised.listeners"] = f"PLAINTEXT://{adv_host}:{broker_port}"
    return out


MIN_PORT = 1
MAX_PORT = 65535
MAX_HOSTNAME_LENGTH = 253


class InputValidator:
    """部署与 CLI 预检：端口、主机名、路径、topic 长度等。"""

    @staticmethod
    def validate_port(port: int) -> bool:
        """端口须为 1..65535 的整数（拒绝 bool 等）"""
        if not isinstance(port, int) or isinstance(port, bool):
            return False
        return MIN_PORT <= port <= MAX_PORT

    @staticmethod
    def validate_hostname(hostname: str) -> bool:
        """IPv4 或匹配 ^[a-zA-Z0-9.-]+$ 的主机名字符串，长度≤253；不含 IPv6 字面量。"""
        if not hostname or len(hostname) > MAX_HOSTNAME_LENGTH:
            return False
        if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', hostname):
            try:
                return all(0 <= int(p) <= 255 for p in hostname.split('.'))
            except ValueError:
                return False
        return bool(re.match(r'^[a-zA-Z0-9.-]+$', hostname))

    @staticmethod
    def validate_path(path: str) -> bool:
        """路径格式基本校验（本脚本目标环境多为类 Unix；非 Kafka 文档条款）。"""
        if not path or ".." in path:
            return False
        try:
            Path(path).resolve()
            return True
        except (OSError, ValueError):
            return False


class SecurityChecker:
    """SSH identity 文件权限校验（与 OpenSSH 拒绝 overly-permissive key 的行为一致）。"""

    @staticmethod
    def ensure_ssh_identity_permissions(key_path: str) -> None:
        """POSIX：若私钥对 group/other 可访问则抛出 ValueError（OpenSSH 将拒绝该密钥文件）。"""
        if os.name != "posix":
            return
        try:
            mode = stat.S_IMODE(os.stat(key_path).st_mode)
        except OSError as e:
            raise OSError(f"无法访问 SSH 私钥: {key_path}") from e
        if mode & 0o077:
            raise ValueError(
                f"SSH 私钥权限对组或其他用户开放，OpenSSH 将拒绝: {key_path} mode={oct(mode)}"
            )


class SSHManager:
    """
    SSH 管理器（与 StarCli 同构）：BatchMode、ConnectTimeout、保活、StrictHostKeyChecking；
    失败时合并 stdout 与 stderr 写入返回字符串。
    """

    def __init__(
            self,
            host: str,
            port: int,
            username: str = "root",
            key_path: str = "~/.ssh/id_rsa",
            strict_host_key_checking: bool = True
    ):
        if not InputValidator.validate_hostname(host):
            raise ValueError(f"无效的主机名: {host}")
        if not InputValidator.validate_port(port):
            raise ValueError(f"无效的端口号: {port}")

        self.host = host
        self.port = port
        self.username = username
        self.key_path = os.path.expanduser(key_path)
        self.strict_host_key_checking = strict_host_key_checking

        if not os.path.exists(self.key_path):
            raise FileNotFoundError(f"SSH 密钥文件不存在: {self.key_path}")
        SecurityChecker.ensure_ssh_identity_permissions(self.key_path)

    def _build_ssh_options(self) -> List[str]:
        """与 StarCli 一致：超时、保活、非交互、端口、密钥、主机密钥策略。"""
        ssh_options = [
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes",
            "-p", str(self.port),
            "-i", self.key_path,
        ]
        if self.strict_host_key_checking:
            known_hosts_file = os.path.expanduser("~/.ssh/known_hosts")
            ssh_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts_file}",
            ])
        else:
            logger.info("StrictHostKeyChecking=no", extra={"to_stdout": True})
            ssh_options.extend(["-o", "StrictHostKeyChecking=no"])
        return ssh_options

    def run_command(self, cmd: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
        """在远程执行命令，返回 (成功, 输出)；失败时合并 stderr/stdout（与 StarCli 一致）。"""
        if not cmd:
            return False, "命令为空"
        ssh_cmd = ["ssh"] + self._build_ssh_options() + [f"{self.username}@{self.host}", cmd]
        try:
            result = run_command(ssh_cmd, check=False, capture_output=True, timeout=timeout or 3600)
            if result.returncode == 0:
                return True, result.stdout if result.stdout else ""
            output_parts = []
            if result.stderr:
                output_parts.append(f"[stderr] {result.stderr}")
            if result.stdout:
                output_parts.append(f"[stdout] {result.stdout}")
            if output_parts:
                return False, "\n".join(output_parts)
            return False, f"命令执行失败，返回码: {result.returncode}"
        except Exception as e:
            logger.error(f"SSH 命令执行失败: {e}", extra={"to_stdout": True})
            return False, str(e)

    def copy_file(self, src: str, dst: str) -> bool:
        """scp 到远程；选项与 StarCli 一致单独构造（-P 端口，不用 ssh 的 -p 映射）。"""
        src_path = Path(src).resolve()
        if not src_path.exists():
            logger.error(f"源文件不存在: {src}", extra={"to_stdout": True})
            return False
        if not dst or ".." in dst:
            logger.error("目标路径无效", extra={"to_stdout": True})
            return False
        scp_options = [
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-P", str(self.port),
            "-i", self.key_path,
        ]
        if self.strict_host_key_checking:
            known_hosts_file = os.path.expanduser("~/.ssh/known_hosts")
            scp_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts_file}",
            ])
        else:
            scp_options.extend(["-o", "StrictHostKeyChecking=no"])
        scp_cmd = ["scp"] + scp_options + [str(src_path), f"{self.username}@{self.host}:{dst}"]
        try:
            result = run_command(scp_cmd, check=False, capture_output=False, timeout=1800)
            return result.returncode == 0
        except Exception as e:
            logger.error("复制文件到远程失败 {} -> {}: {}".format(src, dst, e), extra={"to_stdout": True})
            return False


def run_command(
        cmd: Union[List[str], Tuple[str, ...], str],
        check: bool = True,
        capture_output: bool = True,
        allowed_exit_codes: Optional[List[int]] = None,
        **kwargs
):
    """
    执行 shell 命令并统一处理错误。成功时返回 CompletedProcess；
    当 allowed_exit_codes 非空且进程退出码在列表中时返回 CalledProcessError 实例（含 returncode/stdout/stderr）。
    cmd 为 str 时按空白分割；无法由空白安全拆分时须传入 list/tuple。
    """
    logger.debug(f"Executing: {' '.join(cmd) if isinstance(cmd, (list, tuple)) else cmd}")

    if isinstance(cmd, (list, tuple)):
        if kwargs.get("shell"):
            cmd = " ".join(cmd)
    elif isinstance(cmd, str):
        if not kwargs.get("shell"):
            cmd = cmd.split()
    else:
        raise CommandExecutionError(f"Command must be list, tuple or string: {cmd}")

    if capture_output and ("stdout" in kwargs or "stderr" in kwargs):
        capture_output = False

    # 未传 input= 时 stdin=DEVNULL，防止子进程阻塞读标准输入
    run_kw = dict(kwargs)
    if run_kw.get("stdin") is None and "input" not in run_kw:
        run_kw["stdin"] = subprocess.DEVNULL
    if run_kw.get("encoding") is None:
        run_kw["encoding"] = "utf-8"
        run_kw.setdefault("errors", "replace")

    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True, **run_kw)
        return result
    except subprocess.TimeoutExpired as e:
        logger.error("命令执行超时: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command timeout: {cmd}")
    except subprocess.CalledProcessError as e:
        if allowed_exit_codes and e.returncode in allowed_exit_codes:
            # 允许的退出码时返回异常对象，调用方可通过 e.returncode / e.stdout / e.stderr 使用
            return e
        error_msg = (
            f"Command failed with exit code {e.returncode}: {cmd}\n"
            f"Stderr: {e.stderr.strip() if e.stderr else '(empty)'}\n"
            f"Stdout: {e.stdout.strip() if e.stdout else '(empty)'}"
        )
        raise CommandExecutionError(error_msg)
    except Exception as e:
        logger.error("命令执行异常: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command failed: {e}")


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """同目录临时文件写入后 os.replace 到目标路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class EnvironmentChecker:
    """环境检查器（对齐 StarCli：端口、Java、目录、用户/组、发行版探测等）。"""

    @staticmethod
    def check_port_available(port: int, host: str = '0.0.0.0') -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result != 0
        except Exception as e:
            logger.error("检查端口可用性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def _find_java_home_from_path(java_path: Path) -> Optional[str]:
        try:
            resolved = java_path.resolve()
        except Exception as ex:
            logger.debug("resolve java_path 失败，使用原路径: %s", ex)
            resolved = java_path
        for parent in resolved.parents:
            if (parent / 'lib').exists() or (parent / 'jre' / 'lib').exists():
                if (parent / 'bin' / 'java').exists() or (parent / 'java').exists():
                    return str(parent)
        return None

    @staticmethod
    def _get_os_info() -> Tuple[str, Optional[str]]:
        """获取发行版信息（与 StarCli 一致；无 distro 时返回空串）。"""
        try:
            import distro
            return distro.id(), distro.like()
        except ImportError:
            return "", None

    @staticmethod
    def _find_java_home_by_distro() -> Optional[str]:
        """
        在无 JAVA_HOME 时尝试通过 update-alternatives/alternatives 及固定路径解析 java 可执行文件所在 JAVA_HOME。
        未安装 distro 库时仍遍历通用路径列表。
        """
        os_id, os_like = EnvironmentChecker._get_os_info()

        # Debian/Ubuntu
        if os_id in ("debian", "ubuntu") or (os_like and "debian" in os_like):
            try:
                result = run_command(
                    ["update-alternatives", "--list", "java"],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout:
                    java_path = Path(result.stdout.strip().split("\n")[0])
                    jh = EnvironmentChecker._find_java_home_from_path(java_path)
                    if jh:
                        return jh
            except Exception as ex:
                logger.debug("update-alternatives 查找 Java 失败: %s", ex)
            for path in (
                "/usr/lib/jvm/default-java",
                "/usr/lib/jvm/java-17-openjdk-amd64",
                "/usr/lib/jvm/java-17-openjdk",
                "/usr/lib/jvm/java-11-openjdk-amd64",
                "/usr/lib/jvm/java-11-openjdk",
            ):
                p = Path(path)
                if p.exists() and (p / "bin" / "java").exists():
                    return str(p)

        # RHEL/Rocky/Fedora 等
        if os_id in ("rhel", "centos", "rocky", "fedora", "almalinux") or (
            os_like and "rhel" in os_like
        ):
            try:
                result = run_command(
                    ["alternatives", "--display", "java"],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.split("\n"):
                        if "currently points to" in line or "link currently points to" in line:
                            java_path_str = line.split()[-1]
                            java_path = Path(java_path_str)
                            jh = EnvironmentChecker._find_java_home_from_path(java_path)
                            if jh:
                                return jh
            except Exception as ex:
                logger.debug("alternatives 查找 Java 失败: %s", ex)
            for path in (
                "/usr/lib/jvm/java-17-openjdk",
                "/usr/lib/jvm/java-17",
                "/usr/lib/jvm/java-11-openjdk",
                "/usr/lib/jvm/java-1.17.0-openjdk",
                "/usr/lib/jvm/java-1.11.0-openjdk",
            ):
                p = Path(path)
                if p.exists() and (p / "bin" / "java").exists():
                    return str(p)

        for path in (
            "/usr/lib/jvm/default-java",
            "/usr/lib/jvm/default",
            "/usr/lib/jvm/java-17-openjdk",
            "/usr/lib/jvm/java-17",
            "/usr/lib/jvm/java-11-openjdk",
        ):
            p = Path(path)
            if p.exists() and (p / "bin" / "java").exists():
                return str(p)
        return None

    @staticmethod
    def check_user_group_exists(user: str, group: str) -> Tuple[bool, bool]:
        """
        使用 id(1)、getent(1) 判断用户与组是否存在（与 StarCli 一致）。
        非 POSIX 下返回 (True, True)。
        """
        if os.name != "posix":
            return True, True
        user_exists = False
        group_exists = False
        try:
            result = run_command(
                ["id", user],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5,
            )
            user_exists = result.returncode == 0
        except Exception as ex:
            logger.error("检查用户是否存在失败: {}".format(ex), extra={"to_stdout": True})
        try:
            result = run_command(
                ["getent", "group", group],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5,
            )
            group_exists = result.returncode == 0
        except Exception as ex:
            logger.error("检查组是否存在失败: {}".format(ex), extra={"to_stdout": True})
        return user_exists, group_exists

    @staticmethod
    def check_java(minimum_java_major: int = 17) -> Tuple[bool, Optional[str], Optional[str]]:
        """检查 Java 环境；minimum_java_major 由 min_java_major_for_kafka() 与 Kafka 版本推断结果决定。"""
        try:
            result = run_command(
                ['java', '-version'],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=10
            )
            if result.returncode != 0 and not result.stderr:
                return False, None, None

            version_output = (result.stderr or result.stdout or "").strip()
            java_version = None
            version_match = re.search(r'version\s+"?(\d+)\.?(\d+)?', version_output)
            if version_match:
                major = int(version_match.group(1))
                minor = int(version_match.group(2)) if version_match.group(2) else 0
                if major == 1 and version_match.group(2):
                    major = int(version_match.group(2))
                java_version = f"{major}.{minor}" if minor > 0 else str(major)
                if major < minimum_java_major:
                    logger.error(
                        "java -version 主版本为 {}；本脚本要求 ≥ {}。".format(
                            java_version, minimum_java_major
                        ),
                        extra={"to_stdout": True}
                    )
                    return False, None, java_version
            elif version_output:
                logger.error(
                    "无法解析 java -version 主版本；本脚本要求 Java {}+。".format(minimum_java_major),
                    extra={"to_stdout": True}
                )
                return False, None, None

            java_home = os.environ.get('JAVA_HOME')
            if not java_home:
                java_bin = shutil.which('java')
                if java_bin:
                    java_home = EnvironmentChecker._find_java_home_from_path(Path(java_bin))
            if not java_home:
                java_home = EnvironmentChecker._find_java_home_by_distro()
                if java_home:
                    logger.debug("通过发行版探测得到 JAVA_HOME: %s", java_home)

            if java_home:
                java_home_path = Path(java_home)
                if not (java_home_path / 'bin' / 'java').exists():
                    if (java_home_path.parent / 'bin' / 'java').exists():
                        java_home = str(java_home_path.parent)

            return True, java_home, java_version
        except Exception as e:
            logger.error("检查 Java 环境失败: {}".format(e), extra={"to_stdout": True})
            return False, None, None

    @staticmethod
    def check_directory_writable(path: str) -> bool:
        try:
            path_obj = Path(path)
            if not path_obj.exists():
                path_obj.mkdir(parents=True, exist_ok=True)
            test_file = path_obj / '.kafka_write_test'
            test_file.write_text('test')
            test_file.unlink()
            return True
        except Exception as e:
            logger.error("检查目录可写性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def warn_if_path_under_kafka_home(kafka_home: Path, path_list_csv: str, label: str) -> None:
        """若解析后的路径为 kafka_home 子路径，仅 DEBUG 记录（Kafka 文档不规定 log.dirs 与安装目录相对位置）。"""
        first = (path_list_csv or "").split(",")[0].strip()
        if not first:
            return
        try:
            base = kafka_home.resolve()
            target = Path(first).resolve()
            target.relative_to(base)
            logger.debug("%s 解析路径位于 kafka_home 之下: %s", label, target)
        except ValueError:
            pass
        except Exception as ex:
            logger.debug("路径关系检查跳过: %s", ex)


class ConfigGenerator:
    """
    生成 server.properties 键值对（KRaft combined / controller / broker）。

    extra_properties 合并并覆盖同名字段；SSL/SASL 时须在 extra_properties 中给出完整 listener.security.protocol.map。
    """

    @staticmethod
    def generate_combined_standalone_properties(
            node_id: int,
            log_dirs: str,
            listeners_override: Optional[str] = None,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """KRaft 单节点 combined（broker,controller）server.properties 键值对（3.3+ / 4.x）。"""
        props: Dict[str, str] = {
            "process.roles": "broker,controller",
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            **_kraft_combined_listener_properties(listeners_override),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_combined_standalone_sasl_plain(
            node_id: int,
            log_dirs: str,
            sasl_username: str,
            sasl_password: str,
            extra_properties: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """KRaft 单节点 combined，Broker 对外 SASL_PLAINTEXT + PLAIN（与 generate_combined_standalone_properties 并列）。"""
        props: Dict[str, str] = {
            "process.roles": "broker,controller",
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            **_kraft_combined_sasl_plain_listener_properties(sasl_username, sasl_password),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_combined_standalone_sasl_ssl(
            node_id: int,
            log_dirs: str,
            sasl_username: str,
            sasl_password: str,
            keystore_path: str,
            keystore_password: str,
            key_password: str,
            truststore_path: str,
            truststore_password: str,
            extra_properties: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """KRaft 单节点 combined：SASL_SSL + PLAIN + 自建 PKI ssl.*。"""
        ssl_stack = _ssl_stack_properties(
            keystore_path, keystore_password, key_password, truststore_path, truststore_password
        )
        props: Dict[str, str] = {
            "process.roles": "broker,controller",
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            **_kraft_combined_sasl_ssl_listener_properties(sasl_username, sasl_password, ssl_stack),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_broker_sasl_plain_properties(
            node_id: int,
            log_dirs: str,
            controller_quorum_bootstrap_servers: str,
            sasl_username: str,
            sasl_password: str,
            extra_properties: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """KRaft broker-only，对外 SASL_PLAINTEXT + PLAIN。"""
        props: Dict[str, str] = {
            "process.roles": "broker",
            "node.id": str(node_id),
            "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
            "log.dirs": log_dirs,
            **_kraft_broker_sasl_plain_listener_properties(sasl_username, sasl_password),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_broker_sasl_ssl_properties(
            node_id: int,
            log_dirs: str,
            controller_quorum_bootstrap_servers: str,
            sasl_username: str,
            sasl_password: str,
            keystore_path: str,
            keystore_password: str,
            key_password: str,
            truststore_path: str,
            truststore_password: str,
            extra_properties: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """KRaft broker-only：SASL_SSL + PLAIN + ssl.*。"""
        ssl_stack = _ssl_stack_properties(
            keystore_path, keystore_password, key_password, truststore_path, truststore_password
        )
        props: Dict[str, str] = {
            "process.roles": "broker",
            "node.id": str(node_id),
            "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
            "log.dirs": log_dirs,
            **_kraft_broker_sasl_ssl_listener_properties(sasl_username, sasl_password, ssl_stack),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_server_properties(
            process_roles: str,
            node_id: int,
            log_dirs: str,
            listeners: str,
            controller_quorum_bootstrap_servers: Optional[str] = None,
            metadata_log_dir: Optional[str] = None,
            num_partitions: int = 1,
            default_replication_factor: Optional[int] = None,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """生成 Broker/Standalone server.properties 键值对。"""
        props = {
            "process.roles": process_roles,
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            "listeners": listeners,
            "num.partitions": str(num_partitions),
        }
        if default_replication_factor is not None:
            props["default.replication.factor"] = str(default_replication_factor)
        if controller_quorum_bootstrap_servers:
            props["controller.quorum.bootstrap.servers"] = controller_quorum_bootstrap_servers
        if metadata_log_dir:
            props["metadata.log.dir"] = metadata_log_dir
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_controller_properties(
            node_id: int,
            controller_listener_port: int,
            controller_quorum_bootstrap_servers: str,
            metadata_log_dir: str,
            log_dirs: str,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """生成 Controller 专用 properties。log.dirs 与 metadata.log.dir 均须由调用方显式传入（见 main 校验）。"""
        props = {
            "process.roles": "controller",
            "node.id": str(node_id),
            "listeners": f"CONTROLLER://0.0.0.0:{controller_listener_port}",
            "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
            "controller.listener.names": "CONTROLLER",
            "listener.security.protocol.map": "CONTROLLER:PLAINTEXT",
            "metadata.log.dir": metadata_log_dir,
            "log.dirs": str(log_dirs).strip(),
        }
        if extra_properties:
            props.update(extra_properties)
        return props


class SystemdServiceGenerator:
    """生成 systemd unit（ExecStart=kafka-server-start.sh）。"""

    @staticmethod
    def generate_kafka_service(
            bin_dir: Path,
            config_path: Path,
            deploy_type: str,
            kafka_home: Path,
            user: str = "kafka",
            group: str = "kafka",
            java_home: Optional[str] = None,
    ) -> str:
        """bin_dir、config_path、kafka_home 须为绝对路径；ExecStart=kafka-server-start.sh。"""
        kh = str(kafka_home.resolve())
        env_lines: List[str] = [f'Environment="KAFKA_HOME={kh}"']
        if java_home:
            env_lines.append(f'Environment="JAVA_HOME={java_home}"')
        env_block = "\n".join(env_lines)
        safe_ident = re.sub(r"[^a-z0-9-]+", "-", deploy_type.lower()).strip("-") or "kafka"
        return f"""[Unit]
Description=Apache Kafka - {deploy_type}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={kh}
UMask=0022
{env_block}
ExecStart={bin_dir / "kafka-server-start.sh"} {config_path}
Restart=on-failure
RestartSec=10
TimeoutStartSec=600
TimeoutStopSec=120
SuccessExitStatus=0 143
KillMode=mixed
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kafka-{safe_ident}

LimitNOFILE=1048576
LimitNPROC=65536
LimitCORE=infinity
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
"""


class KafkaDeployer:
    """
    KRaft 部署：写配置、kafka-storage.sh format、kafka-server-start.sh / systemd（enable + restart）。

    幂等：deploy_* 不因「生成配置已存在」失败；版本下限由 check_environment 与 KRAFT_DEPLOY_MIN_VERSION 执行，
    可用 --skip-kraft-version-check 跳过。

    多节点须保证各节点的 node.id 在整集群唯一（含全部 controller 与 broker）；脚本无法代你校验其它机器上的已占用编号，
    配置错误将导致 Kafka 拒绝启动或元数据异常，部署前请自行核对。

    部署前置与失败回滚：见文件头「部署前置与失败回滚」及 KAFKACLI_PARSER_DESCRIPTION【部署前置与失败回滚】；
    写入配置前做跨路径 cluster.id 一致性检查；kafka-storage format 失败时对首次生成的配置文件回滚删除。
    """

    SERVICE_NAME_STANDALONE = "kafka-standalone"
    SERVICE_NAME_CONTROLLER = "kafka-controller"
    SERVICE_NAME_BROKER = "kafka-broker"

    def __init__(
            self,
            kafka_home: str,
            user: str = "kafka",
            group: str = "kafka",
            assume_kafka_version: Optional[str] = None,
            skip_kraft_version_check: bool = False,
    ):
        self.kafka_home = Path(kafka_home).resolve()
        self.user = user
        self.group = group
        self.skip_kraft_version_check = skip_kraft_version_check
        if not self.kafka_home.exists():
            raise ValueError(f"Kafka 安装目录不存在: {kafka_home}")
        self.bin_dir = self.kafka_home / "bin"
        self.config_dir = self.kafka_home / "config"
        if not (self.bin_dir.exists() and (self.bin_dir / "kafka-storage.sh").exists()):
            raise ValueError(f"无效的 Kafka 安装目录（缺少 bin/kafka-storage.sh）: {kafka_home}")
        self.kafka_release = infer_kafka_release(self.kafka_home, assume_kafka_version)
        if self.kafka_release:
            logger.info(
                "检测到 Apache Kafka 版本: %s",
                ".".join(map(str, self.kafka_release)),
                extra={"to_stdout": True},
            )
        else:
            logger.warning(
                "未能解析 Kafka 版本（路径或 libs/kafka-server-common-*.jar）；将按 Kafka 4.x 策略要求 Java 17+。"
                "可设置 KAFKA_CLI_ASSUME_VERSION 或 --assume-kafka-version x.y.z。",
                extra={"to_stdout": True},
            )

    def check_environment(self) -> Tuple[bool, List[str]]:
        """检查部署环境：Java、系统用户/组（与 StarCli 一致）、KRaft 版本下限、安装目录可写等。"""
        errors = []
        min_java = min_java_major_for_kafka(self.kafka_release)
        java_ok, java_home, java_version = EnvironmentChecker.check_java(minimum_java_major=min_java)
        if not java_ok:
            if java_version:
                errors.append(f"Java 版本过低: {java_version}，需要 Java {min_java}+")
            else:
                errors.append(f"未找到 Java 环境；本脚本要求 PATH 或 JAVA_HOME 下存在 JDK {min_java}+")
        elif java_version:
            logger.info(f"检测到 Java 版本: {java_version}", extra={"to_stdout": True})

        if os.name == "posix":
            user_ok, group_ok = EnvironmentChecker.check_user_group_exists(self.user, self.group)
            if not user_ok:
                errors.append(
                    f"系统用户不存在: {self.user}（unit 中 User={self.user}；systemd 将报 217/USER）"
                )
            if not group_ok:
                errors.append(
                    f"系统组不存在: {self.group}（unit 中 Group={self.group}）。"
                )

        if self.kafka_release is not None and self.kafka_release < KRAFT_DEPLOY_MIN_VERSION:
            ver_s = ".".join(map(str, self.kafka_release))
            if self.skip_kraft_version_check:
                logger.warning(
                    "已启用 --skip-kraft-version-check：检测到 Kafka %s（本脚本默认下限 %s+）",
                    ver_s,
                    ".".join(map(str, KRAFT_DEPLOY_MIN_VERSION)),
                    extra={"to_stdout": True},
                )
            else:
                errors.append(
                    f"Kafka 版本 {ver_s} 低于本脚本 KRaft 自动部署下限 {'.'.join(map(str, KRAFT_DEPLOY_MIN_VERSION))}；"
                    f"使用 --skip-kraft-version-check 或配置 skip_kraft_version_check 可跳过"
                )

        if not EnvironmentChecker.check_directory_writable(str(self.kafka_home)):
            errors.append(f"Kafka 目录不可写: {self.kafka_home}")

        if not (self.bin_dir / "kafka-server-start.sh").exists():
            errors.append(f"缺少 bin/kafka-server-start.sh，安装可能不完整: {self.bin_dir}")

        return len(errors) == 0, errors

    def _run_storage_cmd(self, args: List[str], env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        """执行 bin/kafka-storage.sh"""
        cmd = [str(self.bin_dir / "kafka-storage.sh")] + args
        return run_command(cmd, capture_output=True, timeout=60, env=env or os.environ.copy())

    def _run_server_start(self, config_path: Path, java_home: Optional[str] = None) -> subprocess.CompletedProcess:
        """执行 bin/kafka-server-start.sh（前台；启用 systemd 时由 ExecStart 调用，未启用时可手动调用此方法做一次性启动）"""
        cmd = [str(self.bin_dir / "kafka-server-start.sh"), str(config_path)]
        env = os.environ.copy()
        if java_home:
            env["JAVA_HOME"] = java_home
        return run_command(cmd, capture_output=True, timeout=5, env=env)

    def _write_properties(self, path: Path, props: Dict[str, str]) -> bool:
        """写入 Java properties（原子写入，见 _atomic_write_text）。"""
        try:
            lines = []
            for k, v in props.items():
                if v is None or v == "":
                    continue
                v_str = str(v).replace("\\", "\\\\").replace("\n", "\\n")
                lines.append(f"{k}={v_str}")
            _atomic_write_text(path, "\n".join(lines) + "\n")
            logger.info(f"✓ 已写入配置: {path}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"写入配置失败: {e}", extra={"to_stdout": True})
            return False

    def _systemd_unit_content(
            self,
            deploy_type: str,
            config_path: Path,
            java_home: Optional[str] = None,
            user: Optional[str] = None,
            group: Optional[str] = None
    ) -> str:
        """生成 systemd unit 文件内容（委托 SystemdServiceGenerator）。"""
        return SystemdServiceGenerator.generate_kafka_service(
            self.bin_dir,
            config_path,
            deploy_type,
            self.kafka_home,
            user=user or self.user,
            group=group or self.group,
            java_home=java_home,
        )

    def _generate_cluster_id(self) -> Optional[str]:
        """生成 KRaft cluster ID（random-uuid），失败返回 None。"""
        try:
            result = self._run_storage_cmd(["random-uuid"])
            cluster_id = (result.stdout or "").strip()
            if cluster_id:
                logger.info(f"生成 Cluster ID: {cluster_id}", extra={"to_stdout": True})
            return cluster_id or None
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh random-uuid 失败: {e}", extra={"to_stdout": True})
            return None

    def _journal_tail_for_unit(self, service_name: str, lines: int = 40) -> str:
        """journalctl -u 读取最近若干行。"""
        try:
            r = run_command(
                ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"],
                capture_output=True,
                check=False,
                timeout=20,
            )
            return (r.stdout or r.stderr or "").strip()
        except Exception as ex:
            return f"(journalctl 失败: {ex})"

    def _log_friendly_systemd_failure(self, journal_tail: str) -> None:
        """根据 journal 摘录输出与 systemd/Kafka 相关的客观描述（无操作指引）。"""
        t = journal_tail or ""
        if re.search(r"217\s*/\s*USER|status\s*=\s*217", t, re.I) or (
            "217" in t and "USER" in t.upper()
        ):
            u, g = self.user, self.group
            logger.error(
                "journal 与 217/USER 一致：User=%s Group=%s 在系统中不可解析。",
                u, g,
                extra={"to_stdout": True},
            )
            return
        if "permission denied" in t.lower():
            logger.error(
                "journal 含 Permission denied（字符串匹配）。",
                extra={"to_stdout": True},
            )

    @staticmethod
    def _tcp_connect_ok(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((host, port))
            return True
        except OSError:
            return False

    def _chown_data_paths_for_service_user(self, *path_csvs: str) -> None:
        """
        将 log.dirs / metadata.log.dir 根路径递归 chown 为 systemd 运行用户。
        root 执行 kafka-storage format 时常产生 root 属主文件，若服务以 kafka 用户启动会无法读写元数据而秒退。
        非 POSIX 或非 root 时 chown 可能失败，仅打 WARNING。
        """
        roots: List[str] = []
        for csv in path_csvs:
            for part in (csv or "").split(","):
                p = part.strip()
                if p:
                    roots.append(p)
        if not roots:
            return
        ug = f"{self.user}:{self.group}"
        for root in roots:
            try:
                r = run_command(
                    ["chown", "-R", ug, root],
                    check=False,
                    timeout=300,
                    capture_output=True,
                )
                if r.returncode == 0:
                    logger.info(
                        "✓ 数据目录已归属 %s（供 systemd 进程读写）: %s",
                        ug,
                        root,
                        extra={"to_stdout": True},
                    )
                else:
                    err = (r.stderr or r.stdout or "").strip()
                    logger.warning(
                        "chown -R %s %s 未成功（若 Kafka 无法启动请检查目录权限）: %s",
                        ug,
                        root,
                        err,
                        extra={"to_stdout": True},
                    )
            except Exception as ex:
                logger.warning("chown 异常 %s: %s", root, ex, extra={"to_stdout": True})

    def _ensure_kafka_home_paths_for_service_user(self) -> None:
        """
        systemd 启动前：保证安装目录下运行期常用路径对服务用户可用。
        - ${KAFKA_HOME}/logs：JVM GC / log4j（mkdir + chown -R）
        - ${KAFKA_HOME}/config/kafkacli.client.properties：若存在则 chown（脚本写为 0600 root 时仅 root 可读，
          以 kafka 跑 kafkacli 或需读默认客户端配置时会 Permission denied）
        """
        logs_dir = self.kafka_home / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            logger.error(
                "无法创建 Kafka 安装目录下 logs（JVM 须能写入 GC 日志）: %s — %s",
                logs_dir,
                ex,
                extra={"to_stdout": True},
            )
            return
        if os.name != "posix":
            return
        ug = f"{self.user}:{self.group}"
        try:
            r = run_command(
                ["chown", "-R", ug, str(logs_dir)],
                check=False,
                timeout=120,
                capture_output=True,
            )
            if r.returncode == 0:
                logger.info(
                    "✓ 安装目录 logs 已归属 %s（JVM GC / 应用日志可写）: %s",
                    ug,
                    logs_dir,
                    extra={"to_stdout": True},
                )
            else:
                err = (r.stderr or r.stdout or "").strip()
                logger.warning(
                    "chown -R %s %s 未成功（若 JVM 报 GC 日志 Permission denied 请检查）: %s",
                    ug,
                    logs_dir,
                    err,
                    extra={"to_stdout": True},
                )
        except Exception as ex:
            logger.warning("logs 目录 chown 异常: %s", ex, extra={"to_stdout": True})

        kcp = self.kafka_home / "config" / KAFKACLI_CLIENT_PROPERTIES_NAME
        if not kcp.is_file():
            return
        try:
            r2 = run_command(
                ["chown", ug, str(kcp)],
                check=False,
                timeout=30,
                capture_output=True,
            )
            if r2.returncode == 0:
                logger.info(
                    "✓ 默认客户端配置已归属 %s（0600 仅属主可读）: %s",
                    ug,
                    kcp,
                    extra={"to_stdout": True},
                )
            else:
                err = (r2.stderr or r2.stdout or "").strip()
                logger.warning(
                    "chown %s %s 未成功（以 %s 运行 kafkacli 读默认客户端文件可能失败）: %s",
                    ug,
                    kcp,
                    self.user,
                    err,
                    extra={"to_stdout": True},
                )
        except Exception as ex:
            logger.warning("kafkacli.client.properties chown 异常: %s", ex, extra={"to_stdout": True})

    def _wait_for_tcp_listening(
            self,
            host: str,
            port: int,
            label: str,
            total_sec: float = 50.0,
            systemd_unit: Optional[str] = None,
    ) -> bool:
        """部署后轮询 TCP 直至可连（JVM 启动可能较慢）；失败则 ERROR 并可选拉 journal。"""
        deadline = time.time() + total_sec
        while time.time() < deadline:
            if self._tcp_connect_ok(host, port):
                logger.info("✓ %s 已在 %s:%s 监听（TCP 探测成功）", label, host, port, extra={"to_stdout": True})
                return True
            time.sleep(1.2)
        logger.error(
            "在约 %.0f 秒内 %s 未在 %s:%s 接受 TCP 连接（本脚本判定部署未成功）。",
            total_sec,
            label,
            host,
            port,
            extra={"to_stdout": True},
        )
        if systemd_unit:
            tail = self._journal_tail_for_unit(systemd_unit)
            self._log_friendly_systemd_failure(tail)
            if tail:
                logger.error(
                    "journal 摘录（排查进程未监听端口）:\n%s",
                    tail,
                    extra={"to_stdout": True},
                )
        return False

    def _verify_systemd_service_active(self, service_name: str, wait_sec: int = 45) -> Tuple[bool, str]:
        """
        Type=simple 下 systemctl start 可能在主进程立刻退出时仍返回 0，必须轮询 is-active；
        若 is-failed 则提前结束并拉 journal。
        """
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if self._is_service_active(service_name):
                return True, ""
            try:
                rf = run_command(
                    ["systemctl", "is-failed", service_name],
                    check=False,
                    capture_output=True,
                    allowed_exit_codes=[0, 1],
                    timeout=8,
                )
                if rf.returncode == 0 and (rf.stdout or "").strip() == "failed":
                    return False, self._journal_tail_for_unit(service_name)
            except Exception as ex:
                logger.debug("systemctl is-failed: %s", ex)
            time.sleep(1)
        return False, self._journal_tail_for_unit(service_name)

    def _enable_systemd_service(
            self,
            service_name: str,
            config_path: Path,
            deploy_type: str,
            java_home: Optional[str] = None
    ) -> bool:
        """安装并启动 systemd 服务（写 unit、daemon-reload、enable、restart），并确认进入 active。"""
        unit_path = Path(f"/etc/systemd/system/{service_name}.service")
        unit_content = self._systemd_unit_content(deploy_type, config_path, java_home)
        try:
            _atomic_write_text(unit_path, unit_content)
            run_command(["systemctl", "daemon-reload"], timeout=30)
            run_command(["systemctl", "enable", service_name], timeout=30)
            run_command(["systemctl", "reset-failed", service_name], check=False, timeout=10)
            self._ensure_kafka_home_paths_for_service_user()
            # restart：幂等部署时须重载已写入的 server.properties（start 对已 active 的单元不会重启进程）
            run_command(["systemctl", "restart", service_name], timeout=180)
        except Exception as e:
            logger.error(f"systemd 启动阶段异常: {e}", extra={"to_stdout": True})
            tail = self._journal_tail_for_unit(service_name)
            self._log_friendly_systemd_failure(tail)
            if tail:
                logger.error("journal 摘录:\n%s", tail, extra={"to_stdout": True})
            return False

        ok, jtail = self._verify_systemd_service_active(service_name)
        if not ok:
            self._log_friendly_systemd_failure(jtail)
            logger.error(
                "【详情】systemd 未处于 active 或已进入 failed，journal 摘录：\n%s",
                jtail or "(无)",
                extra={"to_stdout": True},
            )
            return False
        logger.info(f"✓ systemd 服务已处于 active: {service_name}", extra={"to_stdout": True})
        return True

    def _verify_port_reachable(self, host: str, port: int, label: str, timeout: int = 5) -> bool:
        """检查 host:port 是否 TCP 可达。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((host, port))
            logger.info(f"✓ {label} 可连接: {host}:{port}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.warning(f"{label} 连接检查失败: {e}", extra={"to_stdout": True})
            return False

    def _check_service_exists(self, service_name: str) -> bool:
        """检查 unit 是否已加载（与 StarCli 一致：优先 LoadState，再回退 status / 文件）。"""
        try:
            show_result = run_command(
                ["systemctl", "show", service_name, "-p", "LoadState", "--value"],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3, 4],
                timeout=10,
            )
            load_state = (show_result.stdout or "").strip().lower()
            if load_state == "not-found":
                return False
            if load_state in ("loaded", "masked", "stub", "generated", "bad"):
                return True
            result = run_command(
                ["systemctl", "status", service_name, "--no-pager"],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 2, 3, 4],
                timeout=10,
            )
            combined = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            if result.returncode in (2, 4) or "not-found" in combined or "could not be found" in combined:
                return False
            return True
        except Exception as ex:
            logger.debug("检查 systemd 服务是否存在失败: %s", ex)
            return Path(f"/etc/systemd/system/{service_name}.service").exists()

    def _is_service_active(self, service_name: str) -> bool:
        try:
            result = run_command(
                ['systemctl', 'is-active', service_name],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3],
                timeout=10
            )
            return result.returncode == 0 and (result.stdout or "").strip() == 'active'
        except Exception as ex:
            logger.debug("systemctl is-active 失败: %s", ex)
            return False

    def _stop_and_disable_service(self, service_name: str, force: bool = True) -> bool:
        """停止并禁用 systemd 服务"""
        try:
            if not self._check_service_exists(service_name):
                return True
            if self._is_service_active(service_name):
                logger.info(f"正在停止服务: {service_name}", extra={"to_stdout": True})
                run_command(
                    ['systemctl', 'stop', service_name],
                    check=False,
                    allowed_exit_codes=[0, 1, 5],
                    timeout=60
                )
                for _ in range(10):
                    if not self._is_service_active(service_name):
                        break
                    time.sleep(1)
                if force and self._is_service_active(service_name):
                    self._kill_service_processes(service_name)
                    time.sleep(2)
            run_command(['systemctl', 'disable', service_name], check=False, timeout=30)
            run_command(['systemctl', 'reset-failed', service_name], check=False, timeout=10)
            logger.info(f"✓ 已停止并禁用: {service_name}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"停止服务失败: {e}", extra={"to_stdout": True})
            return False

    def _kill_service_processes(self, service_name: str) -> bool:
        """强制结束与服务相关的进程"""
        try:
            result = run_command(
                ['systemctl', 'show', service_name, '--property=MainPID', '--value'],
                check=False,
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0 and result.stdout and result.stdout.strip().isdigit():
                pid = int(result.stdout.strip())
                if pid > 1:
                    run_command(['kill', '-TERM', str(pid)], check=False, timeout=5)
                    time.sleep(2)
                    run_command(['kill', '-0', str(pid)], check=False, timeout=5)
                    if run_command(['kill', '-0', str(pid)], check=False, timeout=5).returncode == 0:
                        run_command(['kill', '-KILL', str(pid)], check=False, timeout=5)
            # 禁止使用过于宽泛的匹配（如单独 "kafka"）：会命中 kafkacli 命令行里的 --kafka-home /path/kafka，误杀本进程。
            for pattern in ("kafka-server-start.sh", "kafka.Kafka"):
                try:
                    r = run_command(["pgrep", "-f", pattern], check=False, capture_output=True, timeout=10)
                    if r.returncode == 0 and r.stdout:
                        for pid_str in r.stdout.strip().split("\n"):
                            if pid_str.strip().isdigit() and int(pid_str.strip()) > 1:
                                run_command(["kill", "-KILL", pid_str.strip()], check=False, timeout=5)
                except Exception as ex:
                    logger.debug("pgrep/kill 处理失败: %s", ex)
            return True
        except Exception as e:
            logger.error(f"强制结束进程失败: {e}", extra={"to_stdout": True})
            return False

    def deploy_standalone(
            self,
            log_dirs: str,
            node_id: int,
            cluster_id: Optional[str] = None,
            generate_cluster_id: bool = False,
            use_disk_cluster_id: bool = False,
            listeners: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            extra_properties: Optional[Dict[str, str]] = None,
            enable_sasl_plain: bool = False,
            sasl_username: Optional[str] = None,
            sasl_password: Optional[str] = None,
            sasl_ssl_material: Optional[Tuple[str, str, str, str, str]] = None,
    ) -> bool:
        """
        单节点 combined：写 server.properties → kafka-storage.sh format --standalone → systemd 或打印启动命令。
        cluster.id 须通过 --cluster-id、--generate-cluster-id、--use-disk-cluster-id 之一显式声明（见 main 校验），不再隐式生成或静默复用磁盘。
        可重复执行（幂等）：相同参数下覆盖配置并 systemctl restart。
        enable_sasl_plain：SASL_PLAINTEXT + PLAIN。
        sasl_ssl_material 非空：SASL_SSL + PLAIN + ssl.*（自建 PKI），并写入 kafkacli.client.properties。
        """
        logger.info("=== 部署 Kafka Standalone（KRaft 单节点）===", extra={"to_stdout": True})

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        log_dirs_path = Path(log_dirs.split(",")[0].strip())
        if not EnvironmentChecker.check_directory_writable(str(log_dirs_path)):
            logger.error(f"日志目录不可写: {log_dirs}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, log_dirs, "log.dirs")

        roots_sa = _csv_dirs_to_abs_paths(log_dirs)
        ok_aud, err_aud = _audit_cluster_ids_across_kraft_roots(roots_sa)
        if not ok_aud:
            logger.error(err_aud, extra={"to_stdout": True})
            return False

        config_path = self.config_dir / "server-standalone.properties"
        if config_path.exists():
            logger.info(
                "已有生成配置，将覆盖并收敛为本次参数（幂等部署）: %s",
                config_path,
                extra={"to_stdout": True},
            )

        # 1) cluster.id：须显式 --cluster-id / --generate-cluster-id / --use-disk-cluster-id（与 main 一致）
        existing_cid = _consolidate_existing_cluster_id_from_roots(roots_sa)
        cid_arg = (cluster_id or "").strip() or None
        if cid_arg and generate_cluster_id:
            logger.error("--cluster-id 与 --generate-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False
        if cid_arg and use_disk_cluster_id:
            logger.error("--cluster-id 与 --use-disk-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False
        if generate_cluster_id and use_disk_cluster_id:
            logger.error("--generate-cluster-id 与 --use-disk-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False

        if cid_arg:
            if existing_cid and existing_cid != cid_arg:
                ld0 = log_dirs.split(",")[0].strip()
                logger.error(
                    "log.dirs（%s）中已有 KRaft 元数据，cluster.id=%s，与 --cluster-id=%s 不一致。\n"
                    "可选：① 清空该目录或更换 --log-dirs；② 使用 --cluster-id %s 与磁盘一致；"
                    "③ 确认废弃旧数据后删除 %s/meta.properties 再部署。",
                    ld0,
                    existing_cid,
                    cid_arg,
                    existing_cid,
                    ld0,
                    extra={"to_stdout": True},
                )
                return False
            cluster_id = cid_arg
        elif use_disk_cluster_id:
            if not existing_cid:
                logger.error(
                    "--use-disk-cluster-id 要求 log.dirs 下已存在 meta.properties 且含 cluster.id",
                    extra={"to_stdout": True},
                )
                return False
            cluster_id = existing_cid
            logger.info("按 --use-disk-cluster-id 采用磁盘 cluster.id=%s", cluster_id, extra={"to_stdout": True})
        elif generate_cluster_id:
            if existing_cid:
                logger.error(
                    "数据目录已有 cluster.id=%s，不能使用 --generate-cluster-id；请指定 --cluster-id 或 --use-disk-cluster-id，或清空数据目录。",
                    existing_cid,
                    extra={"to_stdout": True},
                )
                return False
            cluster_id = self._generate_cluster_id()
            if not cluster_id:
                logger.error("无法生成 KAFKA_CLUSTER_ID", extra={"to_stdout": True})
                return False
        else:
            if existing_cid:
                logger.error(
                    "数据目录已有 cluster.id=%s。请指定 --cluster-id 与之相同，或 --use-disk-cluster-id，或清空数据后再部署。",
                    existing_cid,
                    extra={"to_stdout": True},
                )
                return False
            logger.error(
                "新建部署须指定 --cluster-id，或使用 --generate-cluster-id（由脚本调用发行版工具生成 UUID），二者互斥。",
                extra={"to_stdout": True},
            )
            return False

        # 2) 构建 server.properties
        if sasl_ssl_material:
            if listeners and listeners.strip():
                logger.error(
                    "--deploy-sasl-ssl 时不要同时指定 --listeners（当前版本固定 SASL_SSL 与默认端口）",
                    extra={"to_stdout": True},
                )
                return False
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-ssl 需同时提供 SASL 用户名与密码（--kafka-user / --kafka-password）",
                    extra={"to_stdout": True},
                )
                return False
            ks, kspw, keypw, ts, tspw = sasl_ssl_material
            props = ConfigGenerator.generate_combined_standalone_sasl_ssl(
                node_id,
                log_dirs,
                sasl_username.strip(),
                sasl_password,
                ks,
                kspw,
                keypw,
                ts,
                tspw,
                extra_properties=extra_properties,
            )
        elif enable_sasl_plain:
            if listeners and listeners.strip():
                logger.error(
                    "--deploy-sasl-plain 时不要同时指定 --listeners（当前版本固定 SASL_PLAINTEXT 与默认端口）",
                    extra={"to_stdout": True},
                )
                return False
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-plain 需同时提供用户名与密码（--kafka-user / --kafka-password 或 JSON 配置）",
                    extra={"to_stdout": True},
                )
                return False
            props = ConfigGenerator.generate_combined_standalone_sasl_plain(
                node_id,
                log_dirs,
                sasl_username.strip(),
                sasl_password,
                extra_properties=extra_properties,
            )
        else:
            props = ConfigGenerator.generate_combined_standalone_properties(
                node_id, log_dirs, listeners, extra_properties=extra_properties
            )
        config_preexisted = config_path.is_file()
        if not self._write_properties(config_path, props):
            return False

        # 3) kafka-storage.sh format --standalone（已格式化目录须加 --ignore-formatted，否则 4.x 直接失败）
        try:
            self._run_storage_cmd([
                "format",
                "--standalone",
                "-t", cluster_id,
                "--ignore-formatted",
                "-c", str(config_path)
            ])
            logger.info("✓ Kafka 存储已格式化（standalone）", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            _rollback_new_deploy_config(config_path, config_preexisted)
            return False

        self._chown_data_paths_for_service_user(log_dirs)

        # 4) systemd 或前台启动（启用 systemd 时必须 active + 数据端口可连，否则返回失败、不报成功）
        if enable_systemd:
            if not self._enable_systemd_service(
                    self.SERVICE_NAME_STANDALONE, config_path, "standalone", java_home
            ):
                return False
            ls = props.get("listeners", "")
            if sasl_ssl_material:
                bport = _listener_scheme_port(ls, "SASL_SSL", DEFAULT_BROKER_PORT)
                label = "Broker（SASL_SSL）"
            elif enable_sasl_plain:
                bport = _listener_scheme_port(ls, "SASL_PLAINTEXT", DEFAULT_BROKER_PORT)
                label = "Broker（SASL_PLAINTEXT）"
            else:
                bport = _listener_scheme_port(ls, "PLAINTEXT", DEFAULT_BROKER_PORT)
                label = "Broker（PLAINTEXT）"
            if not self._wait_for_tcp_listening(
                    "127.0.0.1", bport, label, systemd_unit=self.SERVICE_NAME_STANDALONE
            ):
                return False
        else:
            logger.info("未启用 systemd；前台启动命令:", extra={"to_stdout": True})
            logger.info(f"  {self.bin_dir / 'kafka-server-start.sh'} {config_path}", extra={"to_stdout": True})

        if sasl_ssl_material and sasl_username and sasl_password:
            try:
                _ks, _kspw, _kp, ts_path, ts_pw = sasl_ssl_material
                ctext = _build_kafka_client_sasl_ssl_content(
                    sasl_username.strip(), sasl_password, "PLAIN", ts_path, ts_pw
                )
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except (ValueError, OSError) as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )
        elif enable_sasl_plain and sasl_username and sasl_password:
            try:
                ctext = _build_kafka_client_properties_content(sasl_username.strip(), sasl_password, "PLAIN")
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except OSError as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )

        return True

    def deploy_controller(
            self,
            node_id: int,
            controller_quorum_bootstrap_servers: str,
            metadata_log_dir: str,
            log_dirs: str,
            controller_listener_port: int = DEFAULT_CONTROLLER_PORT,
            cluster_id: Optional[str] = None,
            generate_cluster_id: bool = False,
            use_disk_cluster_id: bool = False,
            initial_controllers: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            extra_properties: Optional[Dict[str, str]] = None,
            enable_sasl_plain: bool = False,
            sasl_username: Optional[str] = None,
            sasl_password: Optional[str] = None,
            sasl_ssl_material: Optional[Tuple[str, str, str, str, str]] = None,
    ) -> bool:
        """
        部署 KRaft Controller 节点（Kafka 4.x）。配置与 ConfigGenerator.generate_controller_properties 一致，
        含 controller.listener.names、listener.security.protocol.map。
        可重复执行（幂等）：已存在 controller-<node_id>.properties 时覆盖并 restart。
        - 首个 controller：format --standalone 或 --initial-controllers
        - 后续 controller：format --no-initial-controllers
        enable_sasl_plain / sasl_ssl_material：Quorum 仍为 CONTROLLER:PLAINTEXT；仅写入 kafkacli.client.properties，
        供本机连接集群内 Broker（PLAIN 或 SASL_SSL，与集群一致）。

        node.id 须与本集群内其它 controller 及全部 broker 的编号互不重复（全局唯一）。
        """
        logger.info("=== 部署 Kafka Controller（KRaft）===", extra={"to_stdout": True})
        logger.info(
            "KRaft：本节点 node.id=%s 须与集群内其余 controller、全部 broker 的 node.id 互不重复；重复将导致异常。",
            node_id,
            extra={"to_stdout": True},
        )

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        if not controller_quorum_bootstrap_servers:
            logger.error("必须指定 controller.quorum.bootstrap.servers", extra={"to_stdout": True})
            return False

        metadata_dir = (metadata_log_dir or "").strip()
        if not metadata_dir:
            logger.error("须显式指定 --metadata-log-dir（脚本不再使用默认路径）", extra={"to_stdout": True})
            return False
        ld_ctrl = (log_dirs or "").strip()
        if not ld_ctrl:
            logger.error("须显式指定 --log-dirs（controller 部署不再自动生成子目录）", extra={"to_stdout": True})
            return False
        if not EnvironmentChecker.check_directory_writable(metadata_dir):
            logger.error(f"元数据日志目录不可写: {metadata_dir}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, metadata_dir, "metadata.log.dir")

        config_path = self.config_dir / f"controller-{node_id}.properties"
        if config_path.exists():
            logger.info(
                "已有生成配置，将覆盖并收敛为本次参数（幂等部署）: %s",
                config_path,
                extra={"to_stdout": True},
            )

        if sasl_ssl_material:
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-ssl 需同时提供 SASL 用户名与密码（--kafka-user / --kafka-password）",
                    extra={"to_stdout": True},
                )
                return False
            logger.info(
                "已启用 --deploy-sasl-ssl（仅客户端文件）：Controller 监听仍为 CONTROLLER:PLAINTEXT；"
                "将写入 kafkacli.client.properties（SASL_SSL）供连接集群内 Broker。",
                extra={"to_stdout": True},
            )
        elif enable_sasl_plain:
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-plain 需同时提供用户名与密码（--kafka-user / --kafka-password 或 JSON）",
                    extra={"to_stdout": True},
                )
                return False
            logger.info(
                "已启用 --deploy-sasl-plain：Controller 监听仍为 CONTROLLER:PLAINTEXT；"
                "将写入 kafkacli.client.properties 供连接集群内 SASL Broker。",
                extra={"to_stdout": True},
            )

        # Controller 配置（与 ConfigGenerator.generate_controller_properties 一致，含 Kafka 4.x protocol map）
        props = ConfigGenerator.generate_controller_properties(
            node_id=node_id,
            controller_listener_port=controller_listener_port,
            controller_quorum_bootstrap_servers=controller_quorum_bootstrap_servers,
            metadata_log_dir=metadata_dir,
            log_dirs=ld_ctrl,
            extra_properties=extra_properties,
        )
        roots_ct: List[str] = [str(Path(metadata_dir).resolve())]
        roots_ct.extend(_csv_dirs_to_abs_paths(props.get("log.dirs") or ""))
        ok_aud, err_aud = _audit_cluster_ids_across_kraft_roots(roots_ct)
        if not ok_aud:
            logger.error(err_aud, extra={"to_stdout": True})
            return False

        cid_arg = (cluster_id or "").strip() or None
        existing_cid_meta = _consolidate_existing_cluster_id_from_roots(roots_ct)
        if cid_arg and generate_cluster_id:
            logger.error("--cluster-id 与 --generate-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False
        if cid_arg and use_disk_cluster_id:
            logger.error("--cluster-id 与 --use-disk-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False
        if generate_cluster_id and use_disk_cluster_id:
            logger.error("--generate-cluster-id 与 --use-disk-cluster-id 不能同时指定", extra={"to_stdout": True})
            return False

        if cid_arg:
            cid_resolved = cid_arg
            if existing_cid_meta and existing_cid_meta != cid_resolved:
                logger.error(
                    "数据目录已有 KRaft 元数据，cluster.id=%s，与 --cluster-id=%s 不一致。\n"
                    "可选：清空目录或更换 --metadata-log-dir/--log-dirs，或使 --cluster-id 与磁盘一致。",
                    existing_cid_meta,
                    cid_resolved,
                    extra={"to_stdout": True},
                )
                return False
        elif use_disk_cluster_id:
            if not existing_cid_meta:
                logger.error(
                    "--use-disk-cluster-id 要求数据目录已存在 meta.properties 且含 cluster.id",
                    extra={"to_stdout": True},
                )
                return False
            cid_resolved = existing_cid_meta
            logger.info("按 --use-disk-cluster-id 采用磁盘 cluster.id=%s", cid_resolved, extra={"to_stdout": True})
        elif generate_cluster_id:
            if existing_cid_meta:
                logger.error(
                    "数据目录已有 cluster.id=%s，不能使用 --generate-cluster-id；请指定 --cluster-id 或 --use-disk-cluster-id，或清空数据。",
                    existing_cid_meta,
                    extra={"to_stdout": True},
                )
                return False
            cid_resolved = self._generate_cluster_id()
            if not cid_resolved:
                logger.error("无法生成 KAFKA_CLUSTER_ID", extra={"to_stdout": True})
                return False
            logger.info("多节点时其他 controller/broker 须使用同一 cluster id", extra={"to_stdout": True})
        else:
            if existing_cid_meta:
                logger.error(
                    "数据目录已有 cluster.id=%s。请指定 --cluster-id 与之相同，或 --use-disk-cluster-id，或清空数据后再部署。",
                    existing_cid_meta,
                    extra={"to_stdout": True},
                )
                return False
            logger.error(
                "新建 controller 存储须指定 --cluster-id，或使用 --generate-cluster-id，二者与 --use-disk-cluster-id 互斥见帮助。",
                extra={"to_stdout": True},
            )
            return False

        user_supplied_cid = bool(cid_arg)

        # 空磁盘首次格式化用 --standalone；盘上已有元数据则用 --no-initial-controllers；多节点首批见 --initial-controllers
        first_bootstrap = not (initial_controllers or "").strip() and existing_cid_meta is None

        config_preexisted = config_path.is_file()
        if not self._write_properties(config_path, props):
            return False

        format_args = ["format", "--cluster-id", cid_resolved, "-c", str(config_path)]
        if initial_controllers:
            format_args.extend(["--initial-controllers", initial_controllers])
        elif first_bootstrap:
            format_args.append("--standalone")
        else:
            format_args.append("--no-initial-controllers")
        format_args.append("--ignore-formatted")

        try:
            self._run_storage_cmd(format_args)
            logger.info("✓ Controller 存储已格式化", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            _rollback_new_deploy_config(config_path, config_preexisted)
            return False

        self._chown_data_paths_for_service_user(metadata_dir, props.get("log.dirs") or "")

        if enable_systemd:
            if not self._enable_systemd_service(
                    f"{self.SERVICE_NAME_CONTROLLER}-{node_id}", config_path, "controller", java_home
            ):
                return False
            ls = props.get("listeners", "")
            cport = _listener_scheme_port(ls, "CONTROLLER", controller_listener_port)
            if not self._wait_for_tcp_listening(
                    "127.0.0.1",
                    cport,
                    "Controller（CONTROLLER）",
                    systemd_unit=f"{self.SERVICE_NAME_CONTROLLER}-{node_id}",
            ):
                return False

        if sasl_ssl_material and sasl_username and sasl_password:
            try:
                _ks, _kspw, _kp, ts_path, ts_pw = sasl_ssl_material
                ctext = _build_kafka_client_sasl_ssl_content(
                    sasl_username.strip(), sasl_password, "PLAIN", ts_path, ts_pw
                )
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except (ValueError, OSError) as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )
        elif enable_sasl_plain and sasl_username and sasl_password:
            try:
                ctext = _build_kafka_client_properties_content(sasl_username.strip(), sasl_password, "PLAIN")
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except OSError as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )

        return True

    def deploy_broker(
            self,
            node_id: int,
            controller_quorum_bootstrap_servers: str,
            log_dirs: str,
            listeners: Optional[str] = None,
            cluster_id: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            extra_properties: Optional[Dict[str, str]] = None,
            enable_sasl_plain: bool = False,
            sasl_username: Optional[str] = None,
            sasl_password: Optional[str] = None,
            sasl_ssl_material: Optional[Tuple[str, str, str, str, str]] = None,
    ) -> bool:
        """
        部署 KRaft Broker 节点（Kafka 4.x）。format --no-initial-controllers，process.roles=broker。
        可重复执行（幂等）：已存在 server-broker-<id>.properties 时覆盖并 restart。
        默认 PLAINTEXT 监听器时自动补全 inter.broker.listener.name、listener.security.protocol.map、
        advertised.listeners 主机来自 KAFKA_ADVERTISED_HOST；SSL/SASL 时在 extra_properties 中给出完整 listener 与协议映射。
        enable_sasl_plain / sasl_ssl_material：对外 SASL_PLAINTEXT 或 SASL_SSL+PLAIN，并写入 kafkacli.client.properties。

        node.id 须与本集群内全部 controller 及其它 broker 的编号互不重复（全局唯一）。
        """
        logger.info("=== 部署 Kafka Broker（KRaft）===", extra={"to_stdout": True})
        logger.info(
            "KRaft：本节点 node.id=%s 须与集群内全部 controller、其它 broker 的 node.id 互不重复；重复将导致异常。",
            node_id,
            extra={"to_stdout": True},
        )

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        if not controller_quorum_bootstrap_servers:
            logger.error("必须指定 controller.quorum.bootstrap.servers", extra={"to_stdout": True})
            return False

        if not cluster_id:
            logger.error("Broker 部署必须指定 --cluster-id（与 Controller 集群一致）", extra={"to_stdout": True})
            return False

        log_dirs_path = Path(log_dirs.split(",")[0].strip())
        if not EnvironmentChecker.check_directory_writable(str(log_dirs_path)):
            logger.error(f"日志目录不可写: {log_dirs}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, log_dirs, "log.dirs")

        roots_br = _csv_dirs_to_abs_paths(log_dirs)
        ok_aud, err_aud = _audit_cluster_ids_across_kraft_roots(roots_br)
        if not ok_aud:
            logger.error(err_aud, extra={"to_stdout": True})
            return False

        config_path = self.config_dir / f"server-broker-{node_id}.properties"
        if config_path.exists():
            logger.info(
                "已有生成配置，将覆盖并收敛为本次参数（幂等部署）: %s",
                config_path,
                extra={"to_stdout": True},
            )

        if sasl_ssl_material:
            if listeners and listeners.strip():
                logger.error(
                    "--deploy-sasl-ssl 时不要同时指定 --listeners（当前版本固定 SASL_SSL 与默认端口）",
                    extra={"to_stdout": True},
                )
                return False
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-ssl 需同时提供 SASL 用户名与密码（--kafka-user / --kafka-password）",
                    extra={"to_stdout": True},
                )
                return False
            ks, kspw, keypw, ts, tspw = sasl_ssl_material
            props = ConfigGenerator.generate_broker_sasl_ssl_properties(
                node_id,
                log_dirs,
                controller_quorum_bootstrap_servers,
                sasl_username.strip(),
                sasl_password,
                ks,
                kspw,
                keypw,
                ts,
                tspw,
                extra_properties=extra_properties,
            )
        elif enable_sasl_plain:
            if listeners and listeners.strip():
                logger.error(
                    "--deploy-sasl-plain 时不要同时指定 --listeners（当前版本固定 SASL_PLAINTEXT 与默认端口）",
                    extra={"to_stdout": True},
                )
                return False
            err_nm = _validate_sasl_plain_username((sasl_username or "").strip())
            if err_nm:
                logger.error(err_nm, extra={"to_stdout": True})
                return False
            if not sasl_username or not sasl_password:
                logger.error(
                    "--deploy-sasl-plain 需同时提供用户名与密码（--kafka-user / --kafka-password 或 JSON）",
                    extra={"to_stdout": True},
                )
                return False
            props = ConfigGenerator.generate_broker_sasl_plain_properties(
                node_id,
                log_dirs,
                controller_quorum_bootstrap_servers,
                sasl_username.strip(),
                sasl_password,
                extra_properties=extra_properties,
            )
        else:
            props = {
                "process.roles": "broker",
                "node.id": str(node_id),
                "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
                "log.dirs": log_dirs,
                **_kraft_broker_listener_properties(listeners),
            }
            if extra_properties:
                props.update(extra_properties)

        cid_req = (cluster_id or "").strip()
        existing_cid = _consolidate_existing_cluster_id_from_roots(roots_br)
        if existing_cid and existing_cid != cid_req:
            ld0 = log_dirs.split(",")[0].strip()
            logger.error(
                "log.dirs（%s）中已有 KRaft 元数据，cluster.id=%s，与 --cluster-id=%s 不一致。\n"
                "可选：① 清空该目录或更换 log.dirs；② 使用 --cluster-id %s；"
                "③ 确认废弃旧数据后删除 %s/meta.properties 再部署。",
                ld0,
                existing_cid,
                cid_req,
                existing_cid,
                ld0,
                extra={"to_stdout": True},
            )
            return False

        config_preexisted = config_path.is_file()
        if not self._write_properties(config_path, props):
            return False

        try:
            self._run_storage_cmd([
                "format",
                "--cluster-id", cid_req,
                "--no-initial-controllers",
                "--ignore-formatted",
                "-c", str(config_path)
            ])
            logger.info("✓ Broker 存储已格式化", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            _rollback_new_deploy_config(config_path, config_preexisted)
            return False

        self._chown_data_paths_for_service_user(log_dirs)

        if enable_systemd:
            if not self._enable_systemd_service(
                    f"{self.SERVICE_NAME_BROKER}-{node_id}", config_path, "broker", java_home
            ):
                return False
            ls = props.get("listeners", "")
            if sasl_ssl_material:
                bport = _listener_scheme_port(ls, "SASL_SSL", DEFAULT_BROKER_PORT)
                label = "Broker（SASL_SSL）"
            elif enable_sasl_plain:
                bport = _listener_scheme_port(ls, "SASL_PLAINTEXT", DEFAULT_BROKER_PORT)
                label = "Broker（SASL_PLAINTEXT）"
            else:
                bport = _listener_scheme_port(ls, "PLAINTEXT", DEFAULT_BROKER_PORT)
                label = "Broker（PLAINTEXT）"
            if not self._wait_for_tcp_listening(
                    "127.0.0.1",
                    bport,
                    label,
                    systemd_unit=f"{self.SERVICE_NAME_BROKER}-{node_id}",
            ):
                return False

        if sasl_ssl_material and sasl_username and sasl_password:
            try:
                _ks, _kspw, _kp, ts_path, ts_pw = sasl_ssl_material
                ctext = _build_kafka_client_sasl_ssl_content(
                    sasl_username.strip(), sasl_password, "PLAIN", ts_path, ts_pw
                )
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except (ValueError, OSError) as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )
        elif enable_sasl_plain and sasl_username and sasl_password:
            try:
                ctext = _build_kafka_client_properties_content(sasl_username.strip(), sasl_password, "PLAIN")
                _persist_kafkacli_client_properties(self.kafka_home, ctext)
            except OSError as ex:
                logger.warning(
                    "写入 %s 失败: %s",
                    KAFKACLI_CLIENT_PROPERTIES_NAME,
                    ex,
                    extra={"to_stdout": True},
                )

        return True

    def verify_broker_started(self, host: str = "localhost", port: int = DEFAULT_BROKER_PORT) -> bool:
        """健康检查：验证 Broker 是否可连接（TCP 端口）"""
        return self._verify_port_reachable(host, port, "Broker")

    def verify_controller_started(
            self, host: str = "localhost", port: int = DEFAULT_CONTROLLER_PORT
    ) -> bool:
        """健康检查：验证 Controller 端口是否监听"""
        return self._verify_port_reachable(host, port, "Controller")

    def verify_controller_quorum(
            self,
            bootstrap_controller: str,
            command_config: Optional[str] = None,
    ) -> bool:
        """
        部署后元数据面验收：kafka-metadata-quorum.sh --bootstrap-controller … describe --status。
        与 KRaft 官方 Quorum 运维方式一致（先确认 Leader/Voters 可读）。
        """
        cc_err = _validate_command_config_path(command_config)
        if cc_err:
            logger.error(cc_err, extra={"to_stdout": True})
            return False
        bc = (bootstrap_controller or "").strip()
        ok_bs, msg_bs = _validate_bootstrap_server(bc)
        if not ok_bs:
            logger.error("bootstrap-controller 无效: %s", msg_bs, extra={"to_stdout": True})
            return False
        script = self.bin_dir / "kafka-metadata-quorum.sh"
        if not script.is_file():
            logger.error("未找到: %s", script, extra={"to_stdout": True})
            return False
        cmd = [str(script)]
        if command_config:
            cmd.extend(["--command-config", command_config])
        cmd.extend(["--bootstrap-controller", bc, "describe", "--status"])
        print()
        print("=" * 72)
        print(" Controller 部署验收（元数据 Quorum）")
        print("=" * 72)
        print(f" bootstrap.controller : {bc}")
        print(f" command.config       : {command_config or '(未使用)'}")
        print("=" * 72)
        try:
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
        except Exception as ex:
            logger.error("kafka-metadata-quorum 执行失败: %s", ex, extra={"to_stdout": True})
            return False
        out = (r.stdout or "").strip()
        stderr_msg = (r.stderr or "").strip()
        if r.returncode != 0:
            logger.error(
                "Quorum describe --status 失败 (rc=%s): %s",
                r.returncode,
                (stderr_msg or out)[:800],
                extra={"to_stdout": True},
            )
            return False
        for line in out.splitlines():
            print(f"  {line}")
        if not out:
            logger.error("Quorum 无输出", extra={"to_stdout": True})
            return False
        logger.info("✓ Controller 元数据 Quorum 验收通过", extra={"to_stdout": True})
        return True

    def clean_deployment(
            self,
            deploy_type: str,
            node_id: Optional[int] = None,
            backup_config: bool = True,
            clean_data: bool = False,
            log_dirs: Optional[str] = None,
            metadata_log_dir: Optional[str] = None,
    ) -> bool:
        """
        清理本脚本安装的 systemd 服务与生成配置（停止→禁用→进程清理→可选备份→删 unit→daemon-reload）。

        默认不删除 KRaft 数据目录。当 clean_data=True（或命令行 --clean-data，或与 --clean 联用的 --force）
        时，在停止进程后、删除配置文件之前，按「待删配置文件 + 传入路径」解析出的 log.dirs /
        metadata.log.dir 整目录删除（破坏性，等同抹除元数据与分区数据）。
        """
        logger.info(f"=== 清理 Kafka {deploy_type} 部署 ===", extra={"to_stdout": True})

        if deploy_type == "standalone":
            service_name = self.SERVICE_NAME_STANDALONE
            config_path = self.config_dir / "server-standalone.properties"
        elif deploy_type == "controller":
            if node_id is None:
                logger.error("清理 controller 需指定 --node-id", extra={"to_stdout": True})
                return False
            service_name = f"{self.SERVICE_NAME_CONTROLLER}-{node_id}"
            config_path = self.config_dir / f"controller-{node_id}.properties"
        elif deploy_type == "broker":
            if node_id is None:
                logger.error("清理 broker 需指定 --node-id", extra={"to_stdout": True})
                return False
            service_name = f"{self.SERVICE_NAME_BROKER}-{node_id}"
            config_path = self.config_dir / f"server-broker-{node_id}.properties"
        else:
            logger.error(f"无效的 deploy 类型: {deploy_type}", extra={"to_stdout": True})
            return False

        props_from_file: Dict[str, str] = {}
        if config_path.is_file():
            props_from_file = _parse_simple_kafka_properties_file(config_path)

        ld_merged = _merge_config_or_cli_path(props_from_file.get("log.dirs"), log_dirs)
        meta_merged = _merge_config_or_cli_path(props_from_file.get("metadata.log.dir"), metadata_log_dir)

        dirs_to_wipe: List[Tuple[str, str]] = []
        if clean_data:
            if deploy_type == "standalone":
                if not (ld_merged or "").strip():
                    logger.error(
                        "standalone 删除数据（--clean-data 或与 --clean/--clean-first 联用 --force）时，"
                        "须指定 --log-dirs，且/或待删配置文件中能解析出 log.dirs",
                        extra={"to_stdout": True},
                    )
                    return False
                for p in _csv_dirs_to_abs_paths(ld_merged):
                    dirs_to_wipe.append(("log.dirs", p))
            elif deploy_type == "broker":
                if not (ld_merged or "").strip():
                    logger.error(
                        "broker 删除数据时须指定 --log-dirs，且/或待删配置中能解析 log.dirs",
                        extra={"to_stdout": True},
                    )
                    return False
                for p in _csv_dirs_to_abs_paths(ld_merged):
                    dirs_to_wipe.append(("log.dirs", p))
            else:  # controller
                if not (meta_merged or "").strip():
                    logger.error(
                        "controller 删除数据时须指定 --metadata-log-dir，且/或待删配置中有 metadata.log.dir",
                        extra={"to_stdout": True},
                    )
                    return False
                if not (ld_merged or "").strip():
                    logger.error(
                        "controller 删除数据时须指定 --log-dirs，且/或待删配置中有 log.dirs",
                        extra={"to_stdout": True},
                    )
                    return False
                for p in _csv_dirs_to_abs_paths((meta_merged or "").strip()):
                    dirs_to_wipe.append(("metadata.log.dir", p))
                for p in _csv_dirs_to_abs_paths((ld_merged or "").strip()):
                    dirs_to_wipe.append(("log.dirs", p))

        service_path = Path(f"/etc/systemd/system/{service_name}.service")

        logger.info("正在停止服务...", extra={"to_stdout": True})
        self._stop_and_disable_service(service_name, force=True)
        self._kill_service_processes(service_name)
        time.sleep(1)

        if clean_data:
            if not dirs_to_wipe:
                logger.warning(
                    "已请求删除 KRaft 数据，但未解析到任何 log.dirs/metadata.log.dir（请检查配置或传入 --log-dirs / --metadata-log-dir）",
                    extra={"to_stdout": True},
                )
            else:
                logger.info("正在删除 KRaft 数据目录（clean_data）...", extra={"to_stdout": True})
                try:
                    seen_rm: set = set()
                    for label, abs_p in dirs_to_wipe:
                        if abs_p in seen_rm:
                            continue
                        seen_rm.add(abs_p)
                        _rm_rf_kraft_data_dir(abs_p, label)
                except (ValueError, OSError) as ex:
                    logger.error("删除 KRaft 数据目录失败: %s", ex, extra={"to_stdout": True})
                    return False

        logger.info("正在删除服务文件...", extra={"to_stdout": True})
        if service_path.exists():
            service_path.unlink()
            logger.info(f"✓ 已删除: {service_path}", extra={"to_stdout": True})
        run_command(['systemctl', 'daemon-reload'], check=False, timeout=30)

        logger.info("正在处理配置文件...", extra={"to_stdout": True})
        if config_path.exists():
            if backup_config:
                backup_path = config_path.with_suffix(config_path.suffix + f".backup.{int(time.time())}")
                shutil.copy2(config_path, backup_path)
                logger.info(f"✓ 配置已备份: {backup_path}", extra={"to_stdout": True})
            config_path.unlink()
            logger.info(f"✓ 已删除配置: {config_path}", extra={"to_stdout": True})

        if deploy_type in ("standalone", "controller", "broker"):
            kcp = self.config_dir / KAFKACLI_CLIENT_PROPERTIES_NAME
            if kcp.exists():
                kcp.unlink()
                logger.info(f"✓ 已删除默认客户端配置: {kcp}", extra={"to_stdout": True})

        logger.info("=== 清理完成 ===", extra={"to_stdout": True})
        return True

    def show_cluster_status(
            self,
            bootstrap_server: str = "localhost:9092",
            command_config: Optional[str] = None,
            auth_summary: Optional[str] = None,
    ) -> bool:
        """
        集群验收报告：连通、KRaft Quorum、副本健康、Topic/Consumer Group 规模、Lag 汇总与结论。
        认证与 _resolve_kafka_client_config 一致（用户名密码或 kafkacli.client.properties）。
        返回 True 表示客户端连通且 Quorum describe --status 成功（可认为元数据面可用）。
        """
        cc_err = _validate_command_config_path(command_config)
        if cc_err:
            logger.error(cc_err, extra={"to_stdout": True})
            return False

        auth_line = auth_summary if auth_summary is not None else (
            command_config or "(未使用 — PLAINTEXT 或未配置认证)"
        )

        print()
        print("=" * 72)
        print(" Kafka 集群状态 / 验收报告")
        print("=" * 72)
        print(f" bootstrap.server : {bootstrap_server}")
        print(f" 客户端认证       : {auth_line}")
        if command_config:
            print(f" command.config   : {command_config}")
        print("=" * 72)

        coll = KafkaMetricsCollector(str(self.kafka_home), bootstrap_server, command_config)

        ok_connect = False
        ok_quorum_status = False

        # [1] 客户端 → Broker（与运维命令同源认证）
        print("\n[1] 客户端 → 集群（kafka-broker-api-versions）")
        bv = coll.collect_broker_api_versions()
        if bv.get("ok"):
            ok_connect = True
            print(f"  状态: OK")
            print(f"  摘要: {bv.get('snippet', '')}")
        else:
            print(f"  状态: FAIL")
            print(f"  原因: {bv.get('error', 'unknown')}")

        # [2] KRaft Quorum
        print("\n[2] KRaft 元数据 Quorum（kafka-metadata-quorum describe --status）")
        qs = coll.collect_quorum_status()
        if qs.get("ok"):
            ok_quorum_status = True
            print(f"  LeaderId     : {qs.get('leader_id')}")
            print(f"  LeaderEpoch  : {qs.get('leader_epoch')}")
            print(f"  CurrentVoters: {qs.get('voters')}")
        else:
            print("  状态: FAIL")
            qe = (qs.get("error") or "").strip()
            if qe:
                print(f"  原因: {qe}")
            else:
                print("  原因: kafka-metadata-quorum describe --status 非零退出或无输出（见上无「原因」时请查脚本日志）")

        quorum_script = self.bin_dir / "kafka-metadata-quorum.sh"
        if quorum_script.is_file():
            cmd_rep = _kafka_cli_cmd(
                quorum_script,
                bootstrap_server,
                ["describe", "--replication"],
                command_config,
            )
            try:
                r = run_command(
                    cmd_rep,
                    capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120),
                )
                print("\n[3] Quorum 副本同步（describe --replication）")
                if r.returncode == 0 and (r.stdout or "").strip():
                    for line in (r.stdout or "").strip().splitlines():
                        print(f"  {line}")
                else:
                    err3 = (r.stderr or r.stdout or "").strip()
                    print(f"  状态: FAIL")
                    print(f"  原因: {err3[:1200] if err3 else f'exit {r.returncode}，无输出'}")
            except Exception as ex:
                print("\n[3] Quorum 副本同步（describe --replication）")
                print(f"  查询失败: {ex}")

        # [4] 副本健康（under-replicated）
        print("\n[4] 分区副本健康（kafka-topics --under-replicated-partitions）")
        ph = coll.collect_topic_partition_health()
        ur = int(ph.get("under_replicated_partitions") or 0)
        if ph.get("ok"):
            print(f"  under-replicated 分区数: {ur}")
            if ur > 0:
                print("  说明: 存在副本未齐，需排查 Broker/网络/ISR（生产风险）。")
        else:
            print(f"  查询失败: {ph.get('error', '')}")

        # [5] Topic / Group 规模
        print("\n[5] 规模概览")
        ti = coll.collect_topic_count()
        gi = coll.collect_consumer_group_count()
        if ti.get("ok"):
            print(f"  Topic 数量           : {ti.get('topic_count', 0)}")
        else:
            print(f"  Topic 数量: 查询失败 — {ti.get('error', '')}")
        if gi.get("ok"):
            print(f"  Consumer Group 数量  : {gi.get('group_count', 0)}")
        else:
            print(f"  Consumer Group 数量: 查询失败 — {gi.get('error', '')}")

        # [6] Lag 汇总
        print("\n[6] Consumer Lag 汇总（--all-groups --describe）")
        lag = coll.collect_consumer_lag()
        if lag.get("ok"):
            print(f"  估算总 Lag（逐行 LAG 累加）: {lag.get('total_lag', 0)}")
            detail = lag.get("groups") or []
            if lag.get("empty_groups"):
                print("  （当前集群无 Consumer Group，跳过 --all-groups --describe；Lag 为 0 符合预期）")
            elif detail:
                print("  样例（前 8 条）:")
                for row in detail[:8]:
                    print(f"    topic={row.get('topic')} partition={row.get('partition')} lag={row.get('lag')}")
        else:
            print(f"  查询失败: {lag.get('error', '')}")

        # [7] 结论（分项失败原因与 [1]～[6] 对应，避免笼统「连通或 Quorum」）
        print("\n" + "-" * 72)
        print(" 验收结论")
        print("-" * 72)
        critical_ok = ok_connect and ok_quorum_status
        ur_ok = ph.get("ok") and ur == 0
        if critical_ok and ur_ok:
            print("  [通过] [1] Broker 连通、[2] Quorum 可读；[4] 无 under-replicated 分区。")
        elif critical_ok and ph.get("ok") and ur > 0:
            print("  [警告] [1][2] 通过，但 [4] 存在 under-replicated 分区（生产风险）。")
        elif not ok_connect:
            print("  [未通过] 仅 [1] Broker API 连通/认证失败（与 bootstrap、command-config、账号密码有关）；[2] 未单独判定。")
        elif not ok_quorum_status:
            print(
                "  [未通过] [1] 通过但 [2] KRaft Quorum（kafka-metadata-quorum describe --status）失败；"
                "请查看 [2] 原因（含 CLI 参数、元数据口是否可达）。"
            )
        elif not ph.get("ok"):
            print("  [部分] [1][2] 通过，[4] 副本健康查询失败，见上文。")
        else:
            print("  [部分] [1][2][4] 未全部满足或 [5][6] 查询异常，请按各段「状态/原因」逐项排查。")
        print("-" * 72)
        print()

        return critical_ok


def _kafka_cli_cmd(
        script_path: Union[str, Path],
        bootstrap_server: str,
        tail_args: List[str],
        command_config: Optional[str] = None,
) -> List[str]:
    """
    组装多数 bin/*.sh 的命令行：可选全局 --command-config → --bootstrap-server → 其余参数。
    与 Kafka 发行版 Admin 客户端惯例一致；尾随 --command-config 在 kafka-metadata-quorum 等带子命令的工具上会解析失败。
    """
    cmd: List[str] = [str(script_path)]
    if command_config:
        cmd.extend(["--command-config", command_config])
    cmd.extend(["--bootstrap-server", (bootstrap_server or "").strip()])
    cmd.extend(tail_args)
    return cmd


def _build_bootstrap_cmd(
        script_path: Path,
        bootstrap_server: str,
        args: List[str],
        command_config: Optional[str] = None,
        timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """校验脚本、bootstrap、command-config 后执行；超时默认 _kafka_cli_timeout_sec(120)。"""
    if not script_path.is_file():
        raise CommandExecutionError(
            f"未找到 Kafka CLI 脚本: {script_path}（--kafka-home 须指向含 bin/ 的解压根目录）"
        )
    ok, bmsg = _validate_bootstrap_server(bootstrap_server)
    if not ok:
        raise CommandExecutionError(bmsg)
    cc_err = _validate_command_config_path(command_config)
    if cc_err:
        raise CommandExecutionError(cc_err)
    to = timeout if timeout is not None else _kafka_cli_timeout_sec(120)
    cmd = _kafka_cli_cmd(script_path, bootstrap_server, args, command_config)
    return run_command(cmd, capture_output=True, timeout=to)


class KafkaTopicManager:
    """
    bin/kafka-topics.sh 封装。

    create/delete 对「已存在 / 不存在」做 stderr 子串匹配后视为成功（幂等）；若 Topic 已存在但分区数等
    与请求不一致，仍以 broker 报错为准（非「自动改配」）。
    """

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-topics.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def create(self, topic: str, partitions: int = 1, replication_factor: int = 1) -> bool:
        """--create；若 stderr 匹配 _cli_already_exists 则返回 True（本脚本实现）。"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return False
        t = _normalize_topic_name(topic)
        try:
            self._run(
                ["--create", "--topic", t, "--partitions", str(partitions),
                 "--replication-factor", str(replication_factor)],
            )
            logger.info(f"✓ Topic 已创建: {t}", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            if _cli_already_exists(str(e)):
                logger.info("Topic 已存在，跳过创建（幂等）: %s", t, extra={"to_stdout": True})
                return True
            logger.error(f"创建 Topic 失败: {e}", extra={"to_stdout": True})
            return False

    def delete(self, topic: str) -> bool:
        """--delete；broker 须允许 delete.topic.enable。stderr 匹配 _cli_topic_missing 时返回 True。"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return False
        t = _normalize_topic_name(topic)
        try:
            self._run(["--delete", "--topic", t])
            logger.info(f"✓ Topic 已删除: {t}", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            if _cli_topic_missing(str(e)):
                logger.info("Topic 已不存在，跳过删除（幂等）: %s", t, extra={"to_stdout": True})
                return True
            logger.error(f"删除 Topic 失败: {e}", extra={"to_stdout": True})
            return False

    def describe(self, topic: Optional[str] = None) -> Optional[str]:
        """描述 Topic（--describe），不传 topic 则列出所有"""
        try:
            args = ["--describe"]
            if topic:
                ok, msg = _validate_topic_name(topic)
                if not ok:
                    logger.error(msg, extra={"to_stdout": True})
                    return None
                args.extend(["--topic", _normalize_topic_name(topic)])
            r = self._run(args)
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe topic 失败: %s", e, extra={"to_stdout": True})
            return None

    def list(self) -> Optional[str]:
        """列出所有 Topic（--list）"""
        try:
            r = self._run(["--list"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("list topics 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaConsumerGroupManager:
    """kafka-consumer-groups.sh 封装。"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-consumer-groups.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def list_groups(self) -> Optional[str]:
        """列出所有 Consumer Group（--list）"""
        try:
            r = self._run(["--list"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("list consumer groups 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_group(self, group: str) -> Optional[str]:
        """描述 Group（含 lag、--describe --group）"""
        if not (group or "").strip():
            logger.error("consumer group 名称不能为空", extra={"to_stdout": True})
            return None
        try:
            r = self._run(["--describe", "--group", group.strip()])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe consumer group 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_all(self) -> Optional[str]:
        """描述所有 Group（--all-groups --describe）"""
        try:
            r = self._run(["--all-groups", "--describe"], timeout=_kafka_cli_timeout_sec(300))
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe all groups 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaQuorumManager:
    """
    KRaft 元数据 Quorum 运维（add-controller、remove-controller、describe）。
    需提供 bootstrap_server 或 bootstrap_controller 之一，否则 _quorum_cmd 将缺少连接参数。
    """

    def __init__(self, kafka_home: str, bootstrap_server: Optional[str] = None,
                 bootstrap_controller: Optional[str] = None, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.bootstrap_controller = bootstrap_controller
        self.command_config = command_config

    def _quorum_cmd(self, args: List[str]) -> subprocess.CompletedProcess:
        script = self.bin_dir / "kafka-metadata-quorum.sh"
        if not script.is_file():
            raise CommandExecutionError(f"未找到 Quorum CLI: {script}")
        cc_err = _validate_command_config_path(self.command_config)
        if cc_err:
            raise CommandExecutionError(cc_err)
        bc = (self.bootstrap_controller or "").strip()
        bs = (self.bootstrap_server or "").strip()
        if bc:
            ok, msg = _validate_bootstrap_server(bc)
            if not ok:
                raise CommandExecutionError(f"bootstrap-controller: {msg}")
            conn = ["--bootstrap-controller", bc]
        elif bs:
            ok, msg = _validate_bootstrap_server(bs)
            if not ok:
                raise CommandExecutionError(msg)
            conn = ["--bootstrap-server", bs]
        else:
            raise CommandExecutionError("须指定 bootstrap_server 或 bootstrap_controller（kafka-metadata-quorum.sh）")
        # 全局选项须先于子命令 describe/add-controller 等（否则 --command-config 会报 unrecognized arguments）
        cmd = [str(script)]
        if self.command_config:
            cmd.extend(["--command-config", self.command_config])
        cmd.extend(conn + args)
        to = _kafka_cli_timeout_sec(120)
        return run_command(cmd, capture_output=True, timeout=to)

    def add_controller(self) -> bool:
        """kafka-metadata-quorum.sh add-controller。"""
        try:
            self._quorum_cmd(["add-controller"])
            logger.info("✓ 已提交 add-controller", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"add-controller 失败: {e}", extra={"to_stdout": True})
            return False

    def remove_controller(self, controller_id: int, directory_id: str) -> bool:
        """动态移除 Controller（--controller-id --controller-directory-id）"""
        try:
            self._quorum_cmd(["remove-controller", "--controller-id", str(controller_id),
                             "--controller-directory-id", directory_id])
            logger.info("✓ 已提交 remove-controller", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"remove-controller 失败: {e}", extra={"to_stdout": True})
            return False

    def describe_replication(self) -> Optional[str]:
        """describe --replication"""
        try:
            r = self._quorum_cmd(["describe", "--replication"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe quorum replication 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaConfigManager:
    """Broker/Topic 配置运维（kafka-configs.sh）"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-configs.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def describe_broker(self, broker_id: Optional[int] = None) -> Optional[str]:
        """--entity-type brokers [--entity-name id] --describe"""
        args = ["--entity-type", "brokers", "--describe"]
        if broker_id is not None:
            args.extend(["--entity-name", str(broker_id)])
        try:
            r = self._run(args)
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe broker config 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_topic(self, topic: str) -> Optional[str]:
        """--entity-type topics --entity-name <topic> --describe"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return None
        t = _normalize_topic_name(topic)
        try:
            r = self._run(["--entity-type", "topics", "--entity-name", t, "--describe"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe topic config 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaMetricsCollector:
    """
    调用 bin 下 metadata-quorum / kafka-topics / kafka-consumer-groups 采集并汇总为字典（--metrics-json）。
    collect_consumer_lag：无消费组时跳过 --all-groups --describe，避免空集群误报失败（与 --status 一致）。
    """

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.kafka_home = Path(kafka_home).resolve()
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self.bin_dir = self.kafka_home / "bin"

    def _metrics_precheck(self, script_leaf: str) -> Optional[str]:
        """脚本存在、bootstrap、command-config；失败返回错误说明。"""
        p = self.bin_dir / script_leaf
        if not p.is_file():
            return f"未找到 CLI: {p}"
        ok, msg = _validate_bootstrap_server(self.bootstrap_server)
        if not ok:
            return msg
        return _validate_command_config_path(self.command_config)

    def collect_quorum_status(self) -> Dict[str, Any]:
        """元数据 Quorum 状态（LeaderId、HighWatermark、CurrentVoters）"""
        out: Dict[str, Any] = {"ok": False, "leader_id": None, "leader_epoch": None, "voters": []}
        try:
            pre = self._metrics_precheck("kafka-metadata-quorum.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-metadata-quorum.sh",
                self.bootstrap_server,
                ["describe", "--status"],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0 or not (r.stdout or "").strip():
                err = (r.stderr or r.stdout or "").strip()
                out["error"] = err[:4000] if err else f"exit {r.returncode}，无输出"
                return out
            for line in r.stdout.splitlines():
                if "LeaderId:" in line:
                    out["leader_id"] = line.split(":", 1)[1].strip()
                elif "LeaderEpoch:" in line:
                    out["leader_epoch"] = line.split(":", 1)[1].strip()
                elif "CurrentVoters:" in line:
                    out["voters"] = line.split(":", 1)[1].strip()
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_topic_partition_health(self) -> Dict[str, Any]:
        """kafka-topics.sh --describe --under-replicated-partitions。"""
        out: Dict[str, Any] = {"ok": False, "under_replicated_partitions": 0, "topics": {}}
        try:
            pre = self._metrics_precheck("kafka-topics.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-topics.sh",
                self.bootstrap_server,
                ["--describe", "--under-replicated-partitions"],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0:
                out["error"] = ((r.stderr or r.stdout or "").strip() or "describe failed")[:4000]
                return out
            lines = (r.stdout or "").strip().splitlines()
            for line in lines:
                if "Topic:" in line and "Partition:" in line:
                    out["under_replicated_partitions"] += 1
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_consumer_lag(self) -> Dict[str, Any]:
        """Consumer Group Lag（--all-groups --describe）解析 LAG 列"""
        out: Dict[str, Any] = {"ok": False, "groups": [], "total_lag": 0}
        try:
            pre = self._metrics_precheck("kafka-consumer-groups.sh")
            if pre:
                out["error"] = pre
                return out
            # 无任何 Group 时，Kafka 4.x 对 --all-groups --describe 常非 0 退出或 stdout 为空，属正常，勿判失败
            gc = self.collect_consumer_group_count()
            if not gc.get("ok"):
                out["error"] = (gc.get("error") or "").strip() or "consumer-groups --list 失败"
                return out
            if int(gc.get("group_count") or 0) == 0:
                out["ok"] = True
                out["empty_groups"] = True
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-consumer-groups.sh",
                self.bootstrap_server,
                ["--all-groups", "--describe"],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(300))
            if r.returncode != 0 or not (r.stdout or "").strip():
                out["error"] = (r.stderr or r.stdout or "").strip() or "describe failed"
                return out
            lines = r.stdout.strip().splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) >= 6 and parts[5].isdigit():
                    try:
                        lag = int(parts[5])
                        out["total_lag"] += lag
                        out["groups"].append({"topic": parts[1], "partition": parts[2], "lag": lag})
                    except (IndexError, ValueError):
                        pass
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_broker_api_versions(self) -> Dict[str, Any]:
        """kafka-broker-api-versions.sh：验证 bootstrap 可访问（含 SASL/SSL 时与 command-config 一致）。"""
        out: Dict[str, Any] = {"ok": False, "error": None, "snippet": ""}
        try:
            pre = self._metrics_precheck("kafka-broker-api-versions.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-broker-api-versions.sh",
                self.bootstrap_server,
                [],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0:
                out["error"] = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                return out
            text = (r.stdout or "").strip()
            lines = [ln for ln in text.splitlines() if ln.strip()]
            out["ok"] = True
            if lines:
                s0 = lines[0]
                out["snippet"] = (s0[:240] + "…") if len(s0) > 240 else s0
            else:
                out["snippet"] = "(无输出)"
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_topic_count(self) -> Dict[str, Any]:
        """kafka-topics.sh --list 统计 Topic 数量。"""
        out: Dict[str, Any] = {"ok": False, "topic_count": 0, "error": None}
        try:
            pre = self._metrics_precheck("kafka-topics.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-topics.sh",
                self.bootstrap_server,
                ["--list"],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0:
                out["error"] = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                return out
            names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            out["topic_count"] = len(names)
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_consumer_group_count(self) -> Dict[str, Any]:
        """kafka-consumer-groups.sh --list 统计 Group 数量。"""
        out: Dict[str, Any] = {"ok": False, "group_count": 0, "error": None}
        try:
            pre = self._metrics_precheck("kafka-consumer-groups.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = _kafka_cli_cmd(
                self.bin_dir / "kafka-consumer-groups.sh",
                self.bootstrap_server,
                ["--list"],
                self.command_config,
            )
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0:
                out["error"] = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                return out
            names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            out["group_count"] = len(names)
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_all(self) -> Dict[str, Any]:
        """汇总：连通、Quorum、副本、Topic/Group 规模、Lag。"""
        result = {
            "bootstrap_server": self.bootstrap_server,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "broker_connect": self.collect_broker_api_versions(),
            "quorum": self.collect_quorum_status(),
            "partition_health": self.collect_topic_partition_health(),
            "topic_inventory": self.collect_topic_count(),
            "consumer_group_inventory": self.collect_consumer_group_count(),
            "consumer_lag": self.collect_consumer_lag(),
        }
        return result


class KafkaBrokerDecommission:
    """kafka-reassign-partitions.sh --generate / --execute / --verify。"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._reassign_script = self.bin_dir / "kafka-reassign-partitions.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        if not self._reassign_script.is_file():
            raise CommandExecutionError(f"未找到分区重分配脚本: {self._reassign_script}")
        ok, msg = _validate_bootstrap_server(self.bootstrap_server)
        if not ok:
            raise CommandExecutionError(msg)
        cc_err = _validate_command_config_path(self.command_config)
        if cc_err:
            raise CommandExecutionError(cc_err)
        to = timeout if timeout is not None else _kafka_cli_timeout_sec(120)
        cmd = _kafka_cli_cmd(self._reassign_script, self.bootstrap_server, args, self.command_config)
        return run_command(cmd, capture_output=True, timeout=to)

    def generate(self, broker_ids: str, topics_to_move_json_path: Optional[str] = None) -> Optional[str]:
        """--generate --broker-list [--topics-to-move-json-file]。"""
        args = ["--generate", "--broker-list", broker_ids]
        json_path = topics_to_move_json_path if (topics_to_move_json_path and Path(topics_to_move_json_path).exists()) else None
        if not json_path:
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".json")
            try:
                os.write(fd, b'{"topics":[],"version":1}')
                os.close(fd)
                args.extend(["--topics-to-move-json-file", tmp])
                r = self._run(args)
                return r.stdout
            except Exception as ex:
                logger.warning("生成迁移计划（临时 JSON）失败: %s", ex, extra={"to_stdout": True})
                return None
            finally:
                try:
                    os.unlink(tmp)
                except Exception as ex_cleanup:
                    logger.debug("清理临时文件失败: %s", ex_cleanup)
        args.extend(["--topics-to-move-json-file", json_path])
        try:
            r = self._run(args)
            return r.stdout
        except CommandExecutionError:
            return None

    def execute(self, reassignment_json_path: str, throttle_bytes: Optional[int] = None) -> bool:
        """执行迁移（--execute --reassignment-json-file）"""
        args = ["--execute", "--reassignment-json-file", reassignment_json_path]
        if throttle_bytes:
            args.extend(["--throttle", str(throttle_bytes)])
        try:
            self._run(args, timeout=_reassign_cmd_timeout_sec())
            logger.info("✓ 副本迁移已提交", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"执行迁移失败: {e}", extra={"to_stdout": True})
            return False

    def verify(self, reassignment_json_path: str) -> bool:
        """校验迁移进度（--verify）"""
        try:
            r = self._run(
                ["--verify", "--reassignment-json-file", reassignment_json_path],
                timeout=_reassign_cmd_timeout_sec(),
            )
            if "Reassignment of partition" in (r.stdout or "") and "completed" in (r.stdout or "").lower():
                logger.info("✓ 迁移已完成", extra={"to_stdout": True})
                return True
            return False
        except CommandExecutionError:
            return False


def _parse_bootstrap_server(bs: str, default_port: int = DEFAULT_BROKER_PORT) -> Tuple[str, int]:
    """解析 bootstrap_server 字符串为 (host, port)。"""
    s = (bs or "").strip() or "localhost"
    if ":" in s:
        parts = s.rsplit(":", 1)
        host = (parts[0] or "localhost").strip()
        try:
            port = int(parts[1].strip()) if parts[1].strip() else default_port
        except ValueError:
            port = default_port
    else:
        host = s or "localhost"
        port = default_port
    return host, port


def _require(condition: bool, message: str) -> None:
    """条件不满足时打日志并退出。用于 main 中必选参数校验。"""
    if not condition:
        logger.error(message, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


def _get_opt(args: argparse.Namespace, config: Dict[str, Any], attr: str, config_key: Optional[str] = None) -> Any:
    """从 args 或 config 取可选值；config_key 未指定时用 attr。"""
    key = config_key or attr.replace("-", "_")
    val = getattr(args, key, None)
    if val is not None and (not isinstance(val, str) or val.strip() != ""):
        return val
    return config.get(key)


def _resolve_kafka_client_config(
        args: argparse.Namespace,
        config: Dict[str, Any],
        kafka_home: Optional[str],
) -> Tuple[Optional[str], str]:
    """
    解析 Kafka 客户端认证（用户可见描述须与 KAFKACLI_PARSER_DESCRIPTION【认证优先级】、文件头「客户端认证」一致）。

    顺序：1）--command-config / command_config / KAFKA_CLI_COMMAND_CONFIG / KAFKA_COMMAND_CONFIG；
    2）--kafka-user + --kafka-password，或环境变量 KAFKA_SASL_*、KAFKA_USER、KAFKA_PASSWORD（由脚本生成临时 client 配置）；
    3）若存在 ${kafka_home}/config/kafkacli.client.properties（如 --deploy-sasl-plain / --deploy-sasl-ssl 写入）则使用；
    否则无认证（PLAINTEXT 等）。
    """
    v = _get_opt(args, config, "command_config")
    if not (v or "").strip():
        v = os.getenv("KAFKA_CLI_COMMAND_CONFIG") or os.getenv("KAFKA_COMMAND_CONFIG")
    v = (v or "").strip() or None
    if v:
        path_err = _validate_command_config_path(v)
        if path_err:
            logger.error(path_err, extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        return v, f"客户端配置文件: {v}"

    user = _get_opt(args, config, "kafka_user")
    if not (user or "").strip():
        user = os.getenv("KAFKA_SASL_USERNAME") or os.getenv("KAFKA_USER")
    user = (user or "").strip() or None

    pw = _get_opt(args, config, "kafka_password")
    if not (pw or "").strip():
        pw = os.getenv("KAFKA_SASL_PASSWORD") or os.getenv("KAFKA_PASSWORD")
    pw = (pw or "").strip() or None

    mech = _get_opt(args, config, "kafka_sasl_mechanism")
    if not (mech or "").strip():
        mech = os.getenv("KAFKA_SASL_MECHANISM")
    mech = ((mech or "PLAIN").strip() or "PLAIN").upper()

    if user and pw:
        # 显式 str：收窄 Optional / os.getenv 推断，满足 _build_kafka_client_properties_content 签名
        u_s, p_s, m_s = str(user), str(pw), str(mech)
        try:
            content = _build_kafka_client_properties_content(u_s, p_s, m_s)
        except ValueError as e:
            logger.error(str(e), extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        path = _make_temp_client_properties_file(content)
        return (
            path,
            f"SASL/{m_s} 用户={u_s}（由 --kafka-user/--kafka-password 或 KAFKA_SASL_* 生成临时配置）",
        )

    if kafka_home:
        saved = Path(kafka_home).expanduser() / "config" / KAFKACLI_CLIENT_PROPERTIES_NAME
        if saved.is_file():
            p = str(saved.resolve())
            saved_err = _validate_command_config_path(p)
            if saved_err:
                logger.error(saved_err, extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
            return p, f"默认客户端配置: {p}（由 --deploy-sasl-plain / --deploy-sasl-ssl 写入，0600）"

    return None, "(未使用 — PLAINTEXT 或未配置认证)"


def _resolve_deploy_sasl_plain_credentials(
        args: argparse.Namespace, config: Dict[str, Any]
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    --deploy-sasl-plain 与 --kafka-user/--kafka-password（或 JSON / KAFKA_SASL_*）解析。
    用于 standalone / controller / broker 部署；当前部署侧仅生成 PLAIN 服务端与客户端配置。
    """
    deploy_sasl = bool(getattr(args, "deploy_sasl_plain", False) or config.get("deploy_sasl_plain"))
    ku = _get_opt(args, config, "kafka_user")
    if not (ku or "").strip():
        ku = os.getenv("KAFKA_SASL_USERNAME") or os.getenv("KAFKA_USER")
    ku = (ku or "").strip() or None
    kp = _get_opt(args, config, "kafka_password")
    if not (kp or "").strip():
        kp = os.getenv("KAFKA_SASL_PASSWORD") or os.getenv("KAFKA_PASSWORD")
    kp = (kp or "").strip() or None
    if deploy_sasl:
        if not ku or not kp:
            logger.error(
                "--deploy-sasl-plain 需同时提供用户名与密码（--kafka-user / --kafka-password 或 JSON / KAFKA_SASL_*）",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
        mech_chk = _get_opt(args, config, "kafka_sasl_mechanism") or os.getenv("KAFKA_SASL_MECHANISM") or "PLAIN"
        mech_chk = (mech_chk or "PLAIN").strip().upper()
        if mech_chk != "PLAIN":
            logger.error(
                "--deploy-sasl-plain 当前仅支持 PLAIN（请将 kafka_sasl_mechanism 设为 PLAIN）",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
    return deploy_sasl, ku if deploy_sasl else None, kp if deploy_sasl else None


def _resolve_kafka_sasl_account(
        args: argparse.Namespace, config: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """解析 --kafka-user / --kafka-password 与 KAFKA_SASL_* / KAFKA_* 环境变量。"""
    ku = _get_opt(args, config, "kafka_user")
    if not (ku or "").strip():
        ku = os.getenv("KAFKA_SASL_USERNAME") or os.getenv("KAFKA_USER")
    ku = (ku or "").strip() or None
    kp = _get_opt(args, config, "kafka_password")
    if not (kp or "").strip():
        kp = os.getenv("KAFKA_SASL_PASSWORD") or os.getenv("KAFKA_PASSWORD")
    kp = (kp or "").strip() or None
    return ku, kp


def _resolve_deploy_sasl_ssl_material(
        args: argparse.Namespace, config: Dict[str, Any]
) -> Tuple[str, str, str, str, str]:
    """
    --deploy-sasl-ssl：服务端 keystore + truststore（自建 PKI）；环境变量与 --ssl-* 等价。
    返回 (keystore_path, keystore_pw, key_pw, truststore_path, truststore_pw)。
    """
    ks = _get_opt(args, config, "ssl_keystore_path")
    if not (ks or "").strip():
        ks = os.getenv("KAFKA_SSL_KEYSTORE_PATH") or os.getenv("KAFKA_SSL_KEYSTORE_LOCATION")
    ks = (ks or "").strip()
    kspw = _get_opt(args, config, "ssl_keystore_password")
    if not (kspw or "").strip():
        kspw = os.getenv("KAFKA_SSL_KEYSTORE_PASSWORD")
    kspw = (kspw or "").strip()
    keypw = _get_opt(args, config, "ssl_key_password")
    if not (keypw or "").strip():
        keypw = os.getenv("KAFKA_SSL_KEY_PASSWORD")
    keypw = (keypw or "").strip()
    ts = _get_opt(args, config, "ssl_truststore_path")
    if not (ts or "").strip():
        ts = os.getenv("KAFKA_SSL_TRUSTSTORE_PATH") or os.getenv("KAFKA_SSL_TRUSTSTORE_LOCATION")
    ts = (ts or "").strip()
    tspw = _get_opt(args, config, "ssl_truststore_password")
    if not (tspw or "").strip():
        tspw = os.getenv("KAFKA_SSL_TRUSTSTORE_PASSWORD")
    tspw = (tspw or "").strip()
    if not ks or not kspw or not ts or not tspw:
        logger.error(
            "--deploy-sasl-ssl 需 keystore/truststore 路径与口令："
            "--ssl-keystore-path、--ssl-keystore-password、--ssl-truststore-path、--ssl-truststore-password "
            "（或 KAFKA_SSL_* 环境变量）",
            extra={"to_stdout": True},
        )
        sys.exit(EXIT_ERROR)
    ks_p = Path(ks).expanduser()
    ts_p = Path(ts).expanduser()
    if not ks_p.is_file():
        logger.error("--ssl-keystore-path 不是可读文件: %s", ks, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    if not ts_p.is_file():
        logger.error("--ssl-truststore-path 不是可读文件: %s", ts, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    eff_key_pw = keypw if keypw else kspw
    return str(ks_p.resolve()), kspw, eff_key_pw, str(ts_p.resolve()), tspw


def _log_post_deploy_production_practices(
        deploy_type: str, deploy_sasl_plain: bool, deploy_sasl_ssl: bool
) -> None:
    """
    与 Apache Kafka 文档中的生产安全建议对齐的简要提示（非替代安全评审）。
    参考：Security 章节中关于 SSL、SASL、listeners 与受信网络的说明。
    """
    logger.info("--- 生产实践提示（与官方文档方向一致）---", extra={"to_stdout": True})
    logger.info(
        "· 跨不可信网络应使用 TLS（listener 使用 SSL 或 SASL_SSL）；SASL_PLAINTEXT 不加密链路与凭据传输。",
        extra={"to_stdout": True},
    )
    logger.info(
        "· 凭据与账号长期管理优先考虑 SCRAM；PLAIN 多用于受控内网或迁移阶段。",
        extra={"to_stdout": True},
    )
    logger.info(
        "· 高可用依赖多副本、磁盘与监控；本脚本生成标准 properties 并调用发行版 bin/ 工具。",
        extra={"to_stdout": True},
    )
    if deploy_sasl_plain and not deploy_sasl_ssl:
        logger.info(
            "· 当前部署含 SASL_PLAINTEXT：若需与公网或跨机房对齐，请用 extra_properties / --command-config 配置 SASL_SSL。",
            extra={"to_stdout": True},
        )
    if deploy_sasl_ssl:
        logger.info(
            "· 当前为 SASL_SSL + 自建 PKI：请妥善保管 keystore/truststore 口令并定期轮换证书（与 Kafka SSL 运维一致）。",
            extra={"to_stdout": True},
        )
    if deploy_type == "controller":
        logger.info(
            "· 当前为 Controller 节点：Produce/Fetch 与 Topic/Lag 全量验收请在 Broker 上对业务 bootstrap 执行 --status。",
            extra={"to_stdout": True},
        )


def _kafka_deployer_kwargs(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    """传给 KafkaDeployer 的版本相关参数（JSON 中可使用同名字段）。"""
    return {
        "assume_kafka_version": _get_opt(args, config, "assume_kafka_version"),
        "skip_kraft_version_check": bool(
            getattr(args, "skip_kraft_version_check", False) or config.get("skip_kraft_version_check")
        ),
    }


def _parse_target_host(target_host: str, default_port: int) -> Tuple[str, int]:
    """解析 target_host（支持 host 或 host:port）；空或仅空白时抛出 ValueError"""
    th = (target_host or "").strip()
    if not th:
        raise ValueError("target_host 不能为空")
    host, port = th, default_port
    if ":" in th:
        parts = th.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            host, port = parts[0].strip(), int(parts[1])
    if host and not InputValidator.validate_hostname(host):
        raise ValueError(f"无效的 target_host 主机: {host}")
    if not InputValidator.validate_port(port):
        raise ValueError(f"无效的 target_host 端口: {port}")
    return host, port


def _is_local_host(host: str) -> bool:
    """判断是否为本机"""
    if not host:
        return True
    h = host.lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return h in {socket.gethostname().lower(), socket.getfqdn().lower()}
    except Exception as ex:
        logger.debug("判断本机主机名失败: %s", ex)
        return False


def _build_remote_command(
        argv: List[str],
        remote_config_path: Optional[str],
        remote_script: Optional[str] = None
) -> str:
    """
    构建远程执行命令：过滤仅本机有效的参数（如 --target-host、--batch、SSH 相关），
    将 --config 替换为远程路径；若已复制配置但 argv 中无 --config（如批量部署），则追加 --config。
    """
    filtered: List[str] = []
    skip_next = False
    flags_with_value = {"--target-host", "--ssh-user", "--ssh-port", "--ssh-key", "--remote-workdir", "--config"}
    flags_no_value = {"--disable-ssh-host-check", "--batch"}
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in flags_no_value:
            continue
        if arg in flags_with_value:
            if arg == "--config" and remote_config_path:
                filtered.extend(["--config", remote_config_path])
            skip_next = True
            continue
        filtered.append(arg)
    # 批量部署时 argv 不含 --config，但已复制配置到远程，需显式传入
    if remote_config_path and "--config" not in filtered:
        filtered.extend(["--config", remote_config_path])

    if remote_script:
        return f"python3 {shlex.quote(remote_script)} " + " ".join(shlex.quote(a) for a in filtered)
    return "kafkacli " + " ".join(shlex.quote(a) for a in filtered)


def _run_remote_deploy(
        target_host: str,
        args: argparse.Namespace,
        config: Dict[str, Any],
        argv: List[str],
        exit_on_finish: bool = True
) -> Optional[bool]:
    """由前置机经 SSH 在目标机执行 kafkacli，标准输出与错误回显到本地。
    exit_on_finish=False 时仅返回是否成功且不 sys.exit（供 --batch 串行调用下一台）。"""
    ssh_user = config.get("ssh_user") or args.ssh_user
    ssh_port = config.get("ssh_port") or args.ssh_port or 22
    ssh_key = config.get("ssh_key") or args.ssh_key
    strict = not (config.get("disable_ssh_host_check") or args.disable_ssh_host_check)
    remote_workdir = config.get("remote_workdir") or args.remote_workdir or "/tmp/kafka_deploy"

    host, port = _parse_target_host(target_host, ssh_port)
    try:
        ssh = SSHManager(host=host, port=port, username=ssh_user, key_path=ssh_key, strict_host_key_checking=strict)
    except Exception as e:
        logger.error(f"初始化 SSH 失败: {e}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

    # 检查远程是否有 kafkacli（与 StarCli 远程探测 starcli 一致）
    check_cmd = "command -v kafkacli 2>/dev/null || true"
    success, which_out = ssh.run_command(check_cmd, timeout=20)
    remote_script = None
    if not success or not (which_out or "").strip():
        logger.info(f"远程 {host}:{port} 未找到 kafkacli，将复制本地脚本到远程", extra={"to_stdout": True})
        local_script = Path(__file__).resolve() if __file__ and Path(__file__).exists() else None
        if not local_script or not local_script.exists():
            which_k = shutil.which("kafkacli")
            local_script = Path(which_k).resolve() if which_k and Path(which_k).exists() else None
        if not local_script or not local_script.exists():
            logger.error("无法解析本地 kafkacli 路径；PATH 或当前目录须包含可执行脚本", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        ok, out = ssh.run_command(f"mkdir -p {shlex.quote(remote_workdir)}", timeout=20)
        if not ok:
            logger.error(f"创建远程目录失败: {out}", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        remote_script = f"{remote_workdir}/kafka_deploy.py"
        if not ssh.copy_file(str(local_script), remote_script):
            logger.error("复制脚本到远程失败", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        ssh.run_command(f"chmod +x {remote_script}", timeout=10)
        logger.info(f"已复制脚本到远程: {remote_script}", extra={"to_stdout": True})
    else:
        test_cmd = "kafkacli --help 2>&1 | head -5 || true"
        ok_help, help_out = ssh.run_command(test_cmd, timeout=20)
        if not ok_help:
            logger.warning(f"远程 kafkacli --help 探测异常: {help_out}", extra={"to_stdout": True})
        else:
            logger.debug("远程 kafkacli 可用")

    remote_config_path = None
    if args.config:
        if not remote_script:
            ssh.run_command(f"mkdir -p {shlex.quote(remote_workdir)}", timeout=20)
        remote_config_path = f"{remote_workdir}/config.json"
        if not ssh.copy_file(str(Path(args.config).resolve()), remote_config_path):
            logger.error("复制配置文件到远程失败", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)

    remote_cmd = _build_remote_command(argv, remote_config_path, remote_script)
    logger.info(f"正在远程主机 {host}:{port} 执行...", extra={"to_stdout": True})
    logger.info(f"远程命令: {remote_cmd}", extra={"to_stdout": True})
    success, output = ssh.run_command(f"{remote_cmd} 2>&1", timeout=3600)
    if success:
        if output:
            for line in output.strip().split("\n"):
                logger.info(line, extra={"to_stdout": True})
        if not exit_on_finish:
            return True
        sys.exit(EXIT_OK)
    if output:
        logger.error("远程执行失败，完整输出:", extra={"to_stdout": True})
        for line in output.strip().split("\n"):
            logger.error(f"  {line}", extra={"to_stdout": True})
    else:
        logger.error("远程执行失败（无输出）", extra={"to_stdout": True})
    logger.error(f"命令: {remote_cmd}", extra={"to_stdout": True})
    logger.error(f"目标: {host}:{port} (用户: {ssh_user})", extra={"to_stdout": True})
    if not output:
        logger.error("远程失败排查（本脚本约定，非 Kafka 项目文档）：", extra={"to_stdout": True})
        logger.error("  1. 目标机是否已将 kafkacli 安装到 PATH，或本次是否已成功复制脚本到远程", extra={"to_stdout": True})
        logger.error("  2. 在目标机手动执行: kafkacli --help", extra={"to_stdout": True})
        logger.error("  3. 检查目标机 Python3、SSH 与环境变量", extra={"to_stdout": True})
    if not exit_on_finish:
        return False
    sys.exit(EXIT_ERROR)


class _KCliHelpFormatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """加宽帮助列、保留描述换行、并显示默认值（与 argparse 文档一致）。"""

    def __init__(self, prog: str):
        try:
            w = min(110, max(80, shutil.get_terminal_size(fallback=(100, 24)).columns))
        except OSError:
            w = 100
        super().__init__(prog, max_help_position=26, width=w)


KAFKACLI_PARSER_DESCRIPTION = """\
Apache Kafka（KRaft）运维脚本：在 --kafka-home 下调用发行版 bin/kafka-*.sh，生成 server.properties、
可选 systemd，并封装 topic / consumer group / metrics / quorum 等常用操作。

【如何阅读下方选项】每一行格式为「长选项 / 短选项」+ 含义；带默认值的会在行尾标出。
【新手怎么用】运行「kafkacli -h」后阅读文末「分步示例」：建议从 §0 读起，再按 §1、§2… 顺序往下看。
  多节点、远程 SSH、--batch 批量等场景在对应章节说明了执行顺序（例如批量为串行 SSH）与参数含义。
【典型用法】
    1. 指定 Kafka 安装根目录 --kafka-home
    2. 选择一种动作（--deploy / --status / --topic-* …）
    3. 若集群启用认证：使用 --command-config，或 --kafka-user + --kafka-password，或部署时生成的
       ${kafka_home}/config/kafkacli.client.properties（后续子命令默认可读该文件，与 --command-config 优先级见下）。
【配置文件】--config xxx.json 中的键名与长选项对应（下划线，如 kafka_home）；与命令行同时存在时命令行优先。
【认证优先级】与文件头「客户端认证」一致：
  --command-config 或 KAFKA_CLI_COMMAND_CONFIG 或 KAFKA_COMMAND_CONFIG
  优于 --kafka-user + --kafka-password 或 KAFKA_SASL_* / KAFKA_USER / KAFKA_PASSWORD
  优于 ${kafka_home}/config/kafkacli.client.properties
【KRaft 节点编号】多节点时，node.id（命令行即 --node-id，JSON 即 node_id）在同一集群内须全局唯一，
  适用于全部 controller 进程与全部 broker 进程；禁止两台主机使用相同编号（错误示例：controller 已用 1～3 时 broker 仍填 1）。
【部署前置与失败回滚】
  · 写入配置前：对本次 metadata.log.dir / log.dirs 所涉各数据根路径检查 meta.properties 中 cluster.id 是否一致；不一致则拒绝部署（须先清空冲突目录或使用 --clean-data 等）。
  · standalone/controller：cluster.id 须显式三选一（--cluster-id / --generate-cluster-id / --use-disk-cluster-id）；log.dirs、node.id、controller 的 metadata.log.dir 等均无隐式默认路径。
  · kafka-storage format 失败：若本次运行前该生成配置文件不存在，则删除刚生成的该文件；若文件已存在（幂等覆盖），则保留并提示自行核对。
  · format 已成功但后续步骤失败：可能已产生元数据，请按 --clean 分组说明处理，脚本不自动抹盘。
【清理与幂等】
  · --deploy：可重复执行；含上述前置检查；覆盖生成配置、kafka-storage format（--ignore-formatted）、chown、systemctl restart。
  · --topic-create / --topic-delete：对已存在/已缺失按工具输出做幂等处理（见 Topic 分组说明）。
  · --clean：卸载（删 unit 与生成配置），非部署；抹数据须 --clean-data 或与 --clean/--clean-first 联用 --force。
  · --force：单独部署一般不需要；主要用于与 --clean / --clean-first 联用时删除磁盘数据。
"""


def load_json_config(config_file: str) -> Dict[str, Any]:
    """加载 JSON 配置文件；路径须非空且指向已存在的文件"""
    if not (config_file or "").strip():
        logger.error("配置文件路径不能为空", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    config_path = Path(config_file).resolve()
    if not config_path.exists() or not config_path.is_file():
        logger.error(f"配置文件不存在或不是文件: {config_file}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            logger.error("配置文件根类型须为 JSON 对象 {...}，不能为数组或标量", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        logger.info(f"✓ 加载配置文件: {config_file}", extra={"to_stdout": True})
        return config
    except json.JSONDecodeError as ex:
        logger.error(f"配置文件格式错误: {ex}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


def show_examples():
    """接续 argparse -h 输出：分步可复制命令（与上方各 -- 选项对应）。"""
    examples = """
（以下为「分步示例」，须与本命令 -h 屏幕最前各节、源码 KafkaCli.py 文件头「文档一致性」及 tools/kafka/kafka.json.example 交叉一致维护；环境变量见文件头。
 JSON 键名与命令行长选项一致（下划线）。建议按 § 编号顺序阅读。）

------------------------------------------------------------------------
§0 术语：两种「地址」含义不同，请勿混淆
------------------------------------------------------------------------
  · kafkacli：在终端中运行的运维脚本；--kafka-home 为 Kafka 解压目录。
  · 部署角色（KRaft）：
      - standalone：单机同时承担 broker 与 controller（入门、测试常用）；须显式 --node-id。
      - controller：仅参与元数据仲裁（常见为 3 或 5 台，须为奇数）；每台指定一个 --node-id。
      - broker：承载分区数据，对外提供 produce/consume；每台指定一个 --node-id。
      同一集群内可以是「多台 controller + 多台 broker」，并非全集群只有一个 controller。
  · node.id（--node-id）：凡加入同一 KRaft 集群的进程（不论角色）共用一套编号，须全集群唯一。
      例如 controller 已使用 1、2、3，则第一台 broker 应使用 4（或任意尚未占用的正整数），不可再填 1。
  · --target-host：表示「通过 SSH 登录哪一台主机来执行本条 kafkacli」。一条命令只对应一台主机；
      若要多台主机，请多次执行并每次更换 --target-host（或在各主机本地登录后执行，此时可不写 --target-host）。
  · --controller-quorum-bootstrap-servers "host1:9093,host2:9093,..."：
      该字符串写入 Kafka 配置，表示 KRaft 元数据仲裁（CONTROLLER 协议）的访问入口，供本机 Broker/Controller
      进程加入集群使用。它不是 SSH 目标列表，也不会由脚本代为登录多台机器；其中主机名或 IP、端口须与
      各台 Controller 实际监听地址一致。

常用环境变量:
  KAFKA_CLI_TIMEOUT、KAFKA_LOG_DIR、KAFKA_ADVERTISED_HOST、KAFKA_CLI_ASSUME_VERSION、
  KAFKA_CLI_COMMAND_CONFIG（或 KAFKA_COMMAND_CONFIG）、KAFKA_SASL_*、KAFKA_SSL_*

版本: Kafka ≥3.3.0（可 --skip-kraft-version-check）；Java：Kafka 4.x→17，3.x→11。

------------------------------------------------------------------------
§1 最小闭环：本机单机 standalone → 验收
------------------------------------------------------------------------
  步骤 1 — 部署（可重复执行，会覆盖配置并 restart；须含 --node-id 与 cluster.id 三选一之一）:
   export KAFKA_ADVERTISED_HOST=127.0.0.1
   kafkacli --deploy standalone --kafka-home /opt/kafka --node-id 1 --log-dirs /tmp/kafka-logs --generate-cluster-id

  步骤 2 — 看集群是否正常（默认连 localhost:9092）:
   kafkacli --status --kafka-home /opt/kafka

------------------------------------------------------------------------
§2 单机 SASL：部署时写入账号，后续 topic/status 可自动读客户端配置文件
------------------------------------------------------------------------
  从 PLAINTEXT 切到 SASL 可直接再跑下面一行，无需先手工删配置（脚本幂等覆盖）:
   kafkacli --deploy standalone --deploy-sasl-plain --kafka-user admin --kafka-password '***' \\
     --kafka-home /opt/kafka --node-id 1 --log-dirs /tmp/kafka-logs --use-disk-cluster-id
  说明：脚本会写 ${kafka_home}/config/kafkacli.client.properties（0600），之后 --status、--topic-* 等
  若不传密码，会优先读该文件（与「认证优先级」一致）。

  自建 PKI，服务端 SASL_SSL（keystore/truststore 可为 JKS 或 PKCS12）:
   kafkacli --deploy standalone --deploy-sasl-ssl --kafka-user admin --kafka-password '***' \\
     --ssl-keystore-path /secure/kafka.server.p12 --ssl-keystore-password '***' \\
     --ssl-truststore-path /secure/kafka.truststore.jks --ssl-truststore-password '***' \\
     --kafka-home /opt/kafka --node-id 1 --log-dirs /tmp/kafka-logs --use-disk-cluster-id

------------------------------------------------------------------------
§3 远程单机：在前置机（或跳板机）上发起 SSH，只在目标机安装
------------------------------------------------------------------------
  下列命令在运维机上执行，Kafka 实际装在 192.168.1.10 上（与 --controller-quorum-bootstrap-servers 无对应关系）:
   kafkacli --target-host 192.168.1.10 --deploy standalone --kafka-home /opt/kafka --node-id 1 \\
     --log-dirs /var/kafka/logs --generate-cluster-id

  在主机 broker1 上部署 Broker（须先保证 Controller 仲裁已可用；--node-id 须与集群内已有 id 不重复，下例假定用 4）:
   kafkacli --target-host broker1 --deploy broker --kafka-home /opt/kafka --node-id 4 \\
     --controller-quorum-bootstrap-servers "ctrl1:9093,ctrl2:9093,ctrl3:9093" \\
     --log-dirs /var/kafka/logs --cluster-id <CLUSTER_ID>
  说明：--target-host 仅决定 SSH 登录哪台机器；--controller-quorum-bootstrap-servers 供 Kafka 进程定位元数据，二者职责不同。

------------------------------------------------------------------------
§4 多节点 KRaft：先理清拓扑与 node-id，再按步骤执行命令
------------------------------------------------------------------------
  规划示例（主机名可换成 IP）:
    ctrl1、ctrl2、ctrl3 → 各跑一台仅含 controller 角色的节点，--node-id 分别设为 1、2、3。
    broker1、broker2    → 各跑一台仅含 broker 角色的节点；其 --node-id 须在整集群内唯一，
      不能与上述 controller 重复。例如 controller 已占用 1～3，则两台 broker 可设为 4 与 5（仅作编号示例）。
  （说明：KRaft 下「controller 与 broker」是进程角色不同，但 node.id 是同一套全局编号，不能两套各从 1 开始。）
  公共配置串（所有 broker / 后续 controller 都要一致，指向三台 Controller 的 CONTROLLER 口）:
    QUORUM="ctrl1:9093,ctrl2:9093,ctrl3:9093"

  分步 A — 在 ctrl1 上初始化第一台 controller（得到 cluster-id，记下）:
   kafkacli --deploy controller --kafka-home /opt/kafka --node-id 1 \\
     --controller-quorum-bootstrap-servers "$QUORUM" \\
     --metadata-log-dir /var/kafka/metadata-log --log-dirs /var/kafka/controller-log --generate-cluster-id
   # 若用 JSON 合并公共项:  kafkacli --deploy controller --config cluster.json --node-id 1 （须含 metadata_log_dir、log_dirs、generate_cluster_id 等）

  分步 B — 在 ctrl2、ctrl3 上追加 controller（--cluster-id 与集群已有值一致）:
   kafkacli --deploy controller --kafka-home /opt/kafka --node-id 2 --cluster-id <CLUSTER_ID> \\
     --controller-quorum-bootstrap-servers "$QUORUM" --metadata-log-dir /var/kafka/metadata-log \\
     --log-dirs /var/kafka/controller-log
   kafkacli --deploy controller --kafka-home /opt/kafka --node-id 3 --cluster-id <CLUSTER_ID> \\
     --controller-quorum-bootstrap-servers "$QUORUM" --metadata-log-dir /var/kafka/metadata-log \\
     --log-dirs /var/kafka/controller-log

  分步 C — 在每台 broker 机器上部署 broker（cluster-id 同上；--node-id 取未占用的值，勿与 controller 重复）:
   kafkacli --deploy broker --kafka-home /opt/kafka --node-id 4 --cluster-id <CLUSTER_ID> \\
     --controller-quorum-bootstrap-servers "$QUORUM" --log-dirs /var/kafka/logs
   # 第二台 broker 机器上改用 --node-id 5（示例）

  若 Controller 与 Broker 对外要 SASL，可在各自命令上加 --deploy-sasl-plain 与账号（Quorum 口仍为 PLAINTEXT 的常见布局见脚本说明）。

------------------------------------------------------------------------
§5 批量多机（--batch）：从 JSON 读取 nodes，按顺序串行 SSH
------------------------------------------------------------------------
  执行方式（与实现一致）:
    · 仅当同时使用 --batch 与 --config <cluster.json> 时生效；读取根对象中的 nodes 数组。
    · 按数组下标从小到大依次处理：对 nodes[i] 先 SSH 到该元素的 target_host，再在远程执行合并后的 kafkacli；
      本节点命令结束并成功后，再处理 nodes[i+1]。此为串行执行，而非多台同时建立 SSH。
    · 若某一节点远程执行失败，则立即终止并不再处理后续节点；已成功完成的节点不会自动回滚。
  集群依赖与排列顺序:
    · 脚本不会根据角色自动调整 nodes 顺序，须由你在 JSON 中按部署依赖自行排列。
    · 常见做法：先列出全部 controller（首台格式化并产生 cluster_id 后，后续项须带相同 cluster_id），
      再列出各 broker（须带与仲裁一致的 cluster_id）。若顺序错误，后段步骤可能失败。
  配置示例（片段，可复制后补全）:
  # cluster.json 根级可含 kafka_home；nodes 每项对应一台主机，至少含 target_host、deploy、node_id 等
  # { "kafka_home": "/opt/kafka", "nodes": [
  #   { "target_host": "ctrl1", "deploy": "controller", "node_id": 1,
  #     "metadata_log_dir": "/var/kafka/meta", "log_dirs": "/var/kafka/controller-log",
  #     "generate_cluster_id": true,
  #     "controller_quorum_bootstrap_servers": "ctrl1:9093,ctrl2:9093,ctrl3:9093" },
  #   { "target_host": "broker1", "deploy": "broker", "node_id": 4,
  #     "log_dirs": "/var/kafka/logs", "cluster_id": "<CLUSTER_ID>",
  #     "controller_quorum_bootstrap_servers": "ctrl1:9093,ctrl2:9093,ctrl3:9093" }
  # ]}
   kafkacli --batch --config cluster.json

------------------------------------------------------------------------
§6 验收与指标（客户端认证优先级与本帮助最前「【认证优先级】」、文件头「客户端认证」相同）
------------------------------------------------------------------------
   kafkacli --status --kafka-home /opt/kafka --bootstrap-server broker1:9092
   export KAFKA_SASL_USERNAME=admin KAFKA_SASL_PASSWORD='***'
   kafkacli --status --kafka-home /opt/kafka --bootstrap-server broker1:9092
   kafkacli --metrics --kafka-home /opt/kafka --bootstrap-server broker1:9092
   kafkacli --metrics-json --kafka-home /opt/kafka --bootstrap-server broker1:9092

------------------------------------------------------------------------
§7 Topic / Consumer Group
------------------------------------------------------------------------
   kafkacli --topic-create --topic my-topic --partitions 6 --replication-factor 2 --kafka-home /opt/kafka
   kafkacli --topic-list --kafka-home /opt/kafka --bootstrap-server broker1:9092
   kafkacli --topic-describe --topic my-topic --kafka-home /opt/kafka
   kafkacli --group-describe --consumer-group my-consumer --kafka-home /opt/kafka --bootstrap-server broker1:9092

------------------------------------------------------------------------
§8 Broker 下线与分区迁移（先生成方案，再执行，最后校验；停止进程需另行操作）
------------------------------------------------------------------------
   kafkacli --broker-decommission-generate --broker-list 1,2 --kafka-home /opt/kafka --bootstrap-server broker1:9092
  # 将输出中的 Current partition reassignment configuration 保存为 plan.json
   kafkacli --broker-decommission-execute --reassignment-json-file plan.json --throttle 1048576 --kafka-home /opt/kafka
   kafkacli --broker-decommission-verify --reassignment-json-file plan.json --kafka-home /opt/kafka
  # 迁移完成后在该机卸载: kafkacli --clean --deploy broker --node-id <id> --kafka-home /opt/kafka --log-dirs …

------------------------------------------------------------------------
§9 清理与重装（--clean 默认不删数据目录；删数据用 --clean-data 或配合 --force）
------------------------------------------------------------------------
   kafkacli --clean --deploy standalone --kafka-home /opt/kafka --log-dirs /tmp/kafka-logs
   kafkacli --clean --deploy standalone --force --kafka-home /opt/kafka --log-dirs /tmp/kafka-logs
   kafkacli --deploy standalone --clean-first --force --kafka-home /opt/kafka --node-id 1 --log-dirs /tmp/kafka-logs --generate-cluster-id
   kafkacli --config-describe-broker --kafka-home /opt/kafka --config-entity-name 1
   kafkacli --config-describe-topic --topic my-topic --kafka-home /opt/kafka

------------------------------------------------------------------------
§10 生产环境上线前自检（可逐项勾选）
------------------------------------------------------------------------
  [ ] 各节点已安装兼容版本 Java，--kafka-home 路径正确，数据目录权限与磁盘空间满足要求
  [ ] 网络与安全组放行业务端口与 Controller 端口，各节点之间按规划互通
  [ ] --controller-quorum-bootstrap-servers 与实际监听地址、端口一致；broker 的 --cluster-id 与集群一致
  [ ] 各节点的 --node-id 已在整集群范围内核对，无与其它 controller 或 broker 重复
  [ ] 跨不可信网络时优先采用 TLS（如 SASL_SSL），避免长期使用未加密的 SASL_PLAINTEXT
  [ ] 部署后执行 --status 或 --metrics；承载业务前创建 topic，并确认 min.insync.replicas 等策略

------------------------------------------------------------------------
快速索引（与选项表对照）
------------------------------------------------------------------------
  部署+清理   → --deploy / --clean / --clean-first / --clean-data / --kafka-home / --verify
  验收与指标 → --status / --metrics / --bootstrap-server
  认证       → --command-config / --kafka-user / --deploy-sasl-plain / --deploy-sasl-ssl
  Topic/Group→ --topic-* / --group-* / --consumer-group
  远程与批量 → 单机用 --target-host；多机用 --batch + JSON（串行顺序见 §5 与 --batch 说明）
"""
    print(examples)


def main():
    parser = argparse.ArgumentParser(
        description=KAFKACLI_PARSER_DESCRIPTION,
        formatter_class=_KCliHelpFormatter,
        add_help=False,
    )

    g_help = parser.add_argument_group(
        "帮助",
        "请阅读下方各参数分组；含义见每行末说明。未指定任何操作类选项时脚本不执行实质任务（例如须指定 --deploy 等）。",
    )
    g_help.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="打印本帮助（含全部分组选项说明），并输出文末「分步示例」可复制命令。",
    )

    g_cfg = parser.add_argument_group(
        "配置文件与版本",
        "与命令行合并；JSON 键名与长选项一致（下划线）。说明须与文件头「文档一致性」、分步示例同步维护。",
    )
    g_cfg.add_argument(
        "--config",
        metavar="PATH",
        help="JSON 配置文件路径；可与命令行混用，命令行优先覆盖同名字段。",
    )
    g_cfg.add_argument(
        "--assume-kafka-version",
        metavar="X.Y.Z",
        help="当无法从安装路径或 libs/kafka-server-common-*.jar 推断版本时手工指定（如 3.7.0）；或环境变量 KAFKA_CLI_ASSUME_VERSION。",
    )
    g_cfg.add_argument(
        "--skip-kraft-version-check",
        action="store_true",
        help="跳过本脚本对 Kafka ≥3.3.0（KRaft 部署）的版本检查；不推荐用于未知版本环境。",
    )

    g_dep = parser.add_argument_group(
        "部署 KRaft（须配合 --deploy 与 --kafka-home）",
        "standalone=单节点 broker+controller；controller / broker=多节点角色拆分。"
        " 同一套参数可重复执行（覆盖配置并 systemctl restart，见文件头「幂等约定」）。"
        " 多节点时 --node-id 对应 node.id，须在全集群（全部 controller 与 broker）内唯一。"
        " 写入配置前会做数据目录 meta.properties 中 cluster.id 跨路径一致性检查；失败回滚见【部署前置与失败回滚】。"
        " 与 --deploy-sasl-plain / --deploy-sasl-ssl 见「连接与认证」分组。",
    )
    g_dep.add_argument(
        "--deploy",
        choices=["standalone", "controller", "broker"],
        metavar="{standalone,controller,broker}",
        help="要部署的角色：standalone 单机 combined；controller 仅元数据控制器；broker 仅数据节点（须已有 Quorum）。",
    )
    g_dep.add_argument(
        "--kafka-home",
        metavar="PATH",
        help="Kafka 解压目录（须含 bin/kafka-storage.sh）；几乎所有子命令都需要。",
    )
    g_dep.add_argument(
        "--log-dirs",
        metavar="PATH[,PATH...]",
        help="server.properties 的 log.dirs。standalone、controller、broker 部署均须显式指定（或 JSON 的 log_dirs）；无隐式默认路径。",
    )
    g_dep.add_argument(
        "--metadata-log-dir",
        metavar="PATH",
        help="Controller：metadata.log.dir，部署 controller 时必填（或 JSON 的 metadata_log_dir）。",
    )
    g_dep.add_argument(
        "--node-id",
        type=int,
        help="server.properties 的 node.id；standalone、controller、broker 部署均须显式指定（或 JSON 的 node_id）。"
        " 同一集群内须全局唯一。",
    )
    g_dep.add_argument(
        "--cluster-id",
        metavar="ID",
        help="KRaft 集群 UUID。与 --generate-cluster-id、--use-disk-cluster-id 三选一（standalone/controller 部署；broker 仅使用本项加入已有集群）。",
    )
    g_dep.add_argument(
        "--generate-cluster-id",
        action="store_true",
        help="由 kafka-storage 生成新的 cluster.id（与 --cluster-id、--use-disk-cluster-id 互斥；用于全新格式化）。",
    )
    g_dep.add_argument(
        "--use-disk-cluster-id",
        action="store_true",
        help="采用数据目录已有 meta.properties 中的 cluster.id（与另两种互斥）。",
    )
    g_dep.add_argument(
        "--controller-quorum-bootstrap-servers",
        metavar="HOST:PORT[,...]",
        help="写入 controller.quorum.bootstrap.servers；controller 与 broker 部署时通常均需指定，且与集群内各台 Controller 监听地址一致。",
    )
    g_dep.add_argument(
        "--controller-port",
        type=int,
        default=DEFAULT_CONTROLLER_PORT,
        help="本机 Controller 监听端口（CONTROLLER 协议），多机时须与 listeners 规划一致。",
    )
    g_dep.add_argument(
        "--listeners",
        metavar="STR",
        help="自定义 listeners 行；未使用 SASL 自动化部署时可设。standalone 未含 CONTROLLER 时脚本会追加。",
    )
    g_dep.add_argument(
        "--initial-controllers",
        metavar="LIST",
        help="多 controller 首次集群化时的 initial-controllers 参数（见 kafka-storage.sh format）。",
    )
    g_dep.add_argument("--java-home", metavar="PATH", help="运行 Kafka 的 JAVA_HOME；写入 systemd 与 env。")
    g_dep.add_argument(
        "--user",
        default="kafka",
        help="systemd 运行用户（须已存在）；开发机可改为当前用户。",
    )
    g_dep.add_argument("--group", default="kafka", help="systemd 运行组（须已存在）。")
    g_dep.add_argument(
        "--no-systemd",
        action="store_true",
        help="只写配置并 format，不注册 systemd；须自行前台启动 kafka-server-start.sh。",
    )
    g_dep.add_argument(
        "--verify",
        action="store_true",
        help="部署成功后自动验收：standalone/broker 等同 --status；controller 为 Quorum describe --status。",
    )
    g_dep.add_argument(
        "--force",
        action="store_true",
        help="单独 --deploy 已默认可重复执行并覆盖配置（无需本项）。与 --clean / --clean-first 联用时：配合删除 KRaft 数据目录（亦可用 --clean-data）。",
    )
    g_dep.add_argument(
        "--clean",
        action="store_true",
        help="仅卸载：停止并删除本脚本安装的 systemd 单元与生成配置；默认不删 log.dirs/metadata（与文档「清理与幂等」一致）。删数据请用 --clean-data，或与 --clean 同用 --force。须配合 --deploy 指定类型；执行完后退出，不会继续部署。",
    )
    g_dep.add_argument(
        "--clean-data",
        action="store_true",
        help="与 --clean 或 --clean-first 合用：在停止进程后删除解析到的 log.dirs / metadata.log.dir 整目录（破坏性）。路径来自待删配置文件与命令行（命令行优先）。",
    )
    g_dep.add_argument(
        "--clean-first",
        action="store_true",
        help="与 --deploy 合用：先执行与 --clean 相同的卸载，再执行本次部署。须清空磁盘元数据时加 --clean-data 或与 --force 联用。单独 --deploy 已可重复执行；本项用于需要先卸服务/删生成配置再装的场景。不要与仅 --clean 同条命令混用。",
    )

    g_conn = parser.add_argument_group(
        "连接、验收与客户端认证",
        "运维子命令（topic、metrics 等）共用；须能连上集群。客户端认证优先级顺序见本帮助最前「【认证优先级】」段；与源码文件头「客户端认证」及分步示例 §6 一致。",
    )
    g_conn.add_argument(
        "--bootstrap-server",
        default="localhost:9092",
        metavar="HOST:PORT",
        help="客户端连接入口（kafka 客户端 --bootstrap-server）；默认 localhost:9092。",
    )
    g_conn.add_argument(
        "--bootstrap-controller",
        metavar="HOST:PORT",
        help="仅连 Controller 元数据面（kafka-metadata-quorum）；部署 controller 后 --verify 默认 KAFKA_ADVERTISED_HOST:端口。",
    )
    g_conn.add_argument(
        "--status",
        action="store_true",
        help="输出集群验收报告（连通、Quorum、副本、Topic/Group、Lag）；须 --kafka-home 调用 bin 工具。",
    )
    g_conn.add_argument(
        "--command-config",
        metavar="PATH",
        help="显式客户端 properties（SASL_SSL/SCRAM 等）；在所有认证来源中优先级最高，完整顺序见运行 kafkacli -h 时屏幕上方【认证优先级】一节。",
    )
    g_conn.add_argument(
        "--kafka-user",
        metavar="NAME",
        help="SASL 用户名；与 --kafka-password 成对出现时可不写 JAAS 文件。环境变量 KAFKA_SASL_USERNAME / KAFKA_USER。",
    )
    g_conn.add_argument(
        "--kafka-password",
        metavar="STR",
        help="SASL 密码；建议用环境变量 KAFKA_SASL_PASSWORD / KAFKA_PASSWORD 传入以免进 shell 历史。",
    )
    g_conn.add_argument(
        "--kafka-sasl-mechanism",
        default=None,
        metavar="MECH",
        help="客户端 SASL 机制：PLAIN、SCRAM-SHA-256、SCRAM-SHA-512；默认 PLAIN 或读 KAFKA_SASL_MECHANISM。",
    )
    g_conn.add_argument(
        "--deploy-sasl-plain",
        action="store_true",
        help="与 --deploy 合用：服务端 SASL_PLAINTEXT+PLAIN，并写 config/kafkacli.client.properties；与 --deploy-sasl-ssl 互斥。",
    )
    g_conn.add_argument(
        "--deploy-sasl-ssl",
        action="store_true",
        help="与 --deploy 合用：服务端 SASL_SSL+PLAIN+ssl.*（自建 PKI）；并写客户端文件；需 --ssl-* 与账号。",
    )
    g_conn.add_argument(
        "--ssl-keystore-path",
        metavar="PATH",
        help="服务端 keystore（.jks 或 .p12）；环境变量 KAFKA_SSL_KEYSTORE_PATH。",
    )
    g_conn.add_argument(
        "--ssl-keystore-password",
        metavar="STR",
        help="Keystore 口令；环境变量 KAFKA_SSL_KEYSTORE_PASSWORD。",
    )
    g_conn.add_argument(
        "--ssl-key-password",
        metavar="STR",
        help="私钥口令；省略则与 keystore 口令相同；环境变量 KAFKA_SSL_KEY_PASSWORD。",
    )
    g_conn.add_argument(
        "--ssl-truststore-path",
        metavar="PATH",
        help="信任库（含 CA）；环境变量 KAFKA_SSL_TRUSTSTORE_PATH。",
    )
    g_conn.add_argument(
        "--ssl-truststore-password",
        metavar="STR",
        help="Truststore 口令；环境变量 KAFKA_SSL_TRUSTSTORE_PASSWORD。",
    )

    g_ssh = parser.add_argument_group(
        "远程执行（前置机经 SSH 在目标机执行本脚本）",
        "指定 --target-host 时：不在本机安装 Kafka，仅通过 SSH 在目标机执行同一条 kafkacli（未装脚本时会自动拷贝当前文件）。",
    )
    g_ssh.add_argument(
        "--target-host",
        metavar="HOST[:PORT]",
        help="目标机地址；与 --deploy 等组合时在远程执行。未装 kafkacli 时会自动拷贝当前脚本。",
    )
    g_ssh.add_argument("--ssh-user", default="root", help="SSH 登录用户。")
    g_ssh.add_argument("--ssh-port", type=int, default=22, help="SSH 端口。")
    g_ssh.add_argument("--ssh-key", default="~/.ssh/id_rsa", metavar="PATH", help="SSH 私钥路径。")
    g_ssh.add_argument(
        "--disable-ssh-host-check",
        action="store_true",
        help="等价 ssh -o StrictHostKeyChecking=no（仅在内网可控环境使用）。",
    )
    g_ssh.add_argument(
        "--remote-workdir",
        metavar="PATH",
        help="远程临时目录（拷贝脚本/配置用），默认 /tmp/kafka_deploy。",
    )
    g_ssh.add_argument(
        "--batch",
        action="store_true",
        help="须与 --config 联用：读取 JSON 中 nodes 数组，按下标从小到大依次向各 target_host 建立 SSH 并执行部署（串行，非并行）；"
        "上一节点成功后才执行下一节点，任一节点失败则中止后续步骤，已完成的节点不会自动回滚。"
        "KRaft 多节点时，nodes 的排列顺序须由你按集群依赖自行决定（通常先全部 controller，再 broker；broker 项须含与仲裁一致的 cluster_id），脚本不会自动排序。",
    )

    g_topic = parser.add_argument_group(
        "Topic（kafka-topics.sh）",
        "均须 --kafka-home 与 --bootstrap-server。--topic-create：已存在且工具报 already exists 时视为成功；"
        "--topic-delete：已不存在且工具报 unknown/does not exist 时视为成功。",
    )
    g_topic.add_argument("--topic-create", action="store_true", help="创建 Topic；配合 --topic、--partitions、--replication-factor。")
    g_topic.add_argument("--topic-delete", action="store_true", help="删除 Topic；配合 --topic。")
    g_topic.add_argument("--topic-describe", action="store_true", help="描述 Topic；可配合 --topic。")
    g_topic.add_argument("--topic-list", action="store_true", help="列出集群内全部 Topic。")
    g_topic.add_argument("--topic", metavar="NAME", help="Topic 名称。")
    g_topic.add_argument("--partitions", type=int, default=1, help="新建 Topic 的分区数。")
    g_topic.add_argument("--replication-factor", type=int, default=1, help="新建 Topic 的副本数。")

    g_cg = parser.add_argument_group("Consumer Group（kafka-consumer-groups.sh）", "须 --kafka-home。")
    g_cg.add_argument("--group-list", action="store_true", help="列出所有消费组。")
    g_cg.add_argument("--group-describe", action="store_true", help="描述消费组详情与 Lag；须 --consumer-group。")
    g_cg.add_argument("--consumer-group", metavar="NAME", help="消费组 id。")

    g_met = parser.add_argument_group("指标采集", "一次性汇总连通性、Quorum、副本、Lag 等。")
    g_met.add_argument("--metrics", action="store_true", help="人类可读多段输出。")
    g_met.add_argument("--metrics-json", action="store_true", help="同上，JSON 结构便于脚本解析。")

    g_cf = parser.add_argument_group("动态配置（kafka-configs.sh）", "须 --kafka-home。")
    g_cf.add_argument("--config-describe-broker", action="store_true", help="查看 broker 级配置；可 --config-entity-name 指定 broker id。")
    g_cf.add_argument("--config-describe-topic", action="store_true", help="查看 topic 级配置；须 --topic。")
    g_cf.add_argument("--config-entity-name", metavar="NAME", help="describe broker 时的实体名（如数字 broker id）。")

    g_q = parser.add_argument_group("KRaft Quorum", "须 --kafka-home。")
    g_q.add_argument("--quorum-add-controller", action="store_true", help="向现有 Quorum 动态添加 controller 节点。")

    g_br = parser.add_argument_group(
        "Broker 下线与副本迁移（kafka-reassign-partitions.sh）",
        "先 generate 再 execute，最后用 verify；停进程需另行 systemctl/kill。",
    )
    g_br.add_argument(
        "--broker-decommission-generate",
        action="store_true",
        help="生成迁出副本方案；须 --broker-list，可选 --topics-to-move-json-file。",
    )
    g_br.add_argument(
        "--broker-decommission-execute",
        action="store_true",
        help="执行 --reassignment-json-file 中的迁移计划；可选 --throttle。",
    )
    g_br.add_argument("--broker-decommission-verify", action="store_true", help="校验迁移是否完成。")
    g_br.add_argument(
        "--broker-list",
        metavar="ID,ID,...",
        help="要迁出副本的目标 broker id 列表（逗号分隔）。",
    )
    g_br.add_argument(
        "--topics-to-move-json-file",
        "--topics-to-move-json",
        dest="topics_to_move_json",
        metavar="PATH",
        help="限制只迁移部分 topic 时的 JSON 文件。",
    )
    g_br.add_argument(
        "--reassignment-json-file",
        metavar="PATH",
        help="execute/verify 使用的分区重分配 JSON 文件。",
    )
    g_br.add_argument("--throttle", type=int, metavar="BYTES", help="迁移带宽上限（字节/秒）。")

    args = parser.parse_args()

    if args.help:
        parser.print_help()
        print()
        print("=" * 72)
        print("分步示例与场景命令（可复制；与上方选项对应）")
        print("=" * 72)
        show_examples()
        return

    config = {}
    if args.config:
        config = load_json_config(args.config)

    # 远程部署：在前置机执行时，将命令转发到目标机并回显日志
    target_host = config.get("target_host") or args.target_host
    if target_host:
        try:
            _parse_target_host(target_host, args.ssh_port or 22)
        except ValueError as e:
            logger.error(f"无效的 target_host: {target_host}，{e}", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        if not _is_local_host(target_host):
            _run_remote_deploy(target_host, args, config, sys.argv)
            return

    # 批量部署：按 config.nodes 下标顺序串行 SSH（非并行）；单节点失败则退出，见 --batch 帮助说明。
    if getattr(args, "batch", False):
        _require(args.config, "--batch 需同时指定 --config（含 nodes 数组的 JSON 文件）")
        nodes = config.get("nodes")
        _require(nodes, "--batch 需在配置文件中提供 nodes 数组")
        _require(isinstance(nodes, list) and len(nodes) > 0, "--batch 需在配置文件中提供非空 nodes 数组")
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            th = node.get("target_host")
            if not th:
                logger.warning(f"nodes[{i}] 缺少 target_host，跳过", extra={"to_stdout": True})
                continue
            logger.info(f"=== 批量部署 [{i+1}/{len(nodes)}] 目标: {th} ===", extra={"to_stdout": True})
            # 合并 node 与 config，构建远程执行的 argv（不含 --target-host / --batch）
            argv_parts = [sys.argv[0]]
            for k, v in node.items():
                if k in ("target_host", "ssh_user", "ssh_port", "ssh_key", "remote_workdir", "disable_ssh_host_check"):
                    continue
                key = "--" + k.replace("_", "-")
                if isinstance(v, bool):
                    if v:
                        argv_parts.append(key)
                elif v is not None and str(v).strip() != "":
                    argv_parts.append(key)
                    argv_parts.append(str(v))
            # 全局 config 中的 kafka_home 等若 node 未覆盖则从 config 取
            base = config.get("kafka_home") or args.kafka_home
            if base and "--kafka-home" not in argv_parts:
                argv_parts.extend(["--kafka-home", base])
            ok = _run_remote_deploy(th, args, config, argv_parts, exit_on_finish=False)
            if ok is False:
                logger.error(f"批量部署在节点 {th} 失败，终止", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        logger.info("=== 批量部署全部完成 ===", extra={"to_stdout": True})
        return

    kafka_home = args.kafka_home or config.get("kafka_home")

    # 仅查看状态：完整验收需 --kafka-home；否则仅 TCP 探测
    if args.status and not (args.deploy or config.get("deploy")):
        if kafka_home:
            try:
                deployer = KafkaDeployer(
                    kafka_home,
                    user=config.get("user", "kafka"),
                    group=config.get("group", "kafka"),
                    **_kafka_deployer_kwargs(args, config),
                )
                cc, auth_line = _resolve_kafka_client_config(args, config, kafka_home)
                ok = deployer.show_cluster_status(
                    bootstrap_server=args.bootstrap_server or config.get("bootstrap_server", "localhost:9092"),
                    command_config=cc,
                    auth_summary=auth_line,
                )
                sys.exit(EXIT_OK if ok else EXIT_ERROR)
            except ValueError as e:
                logger.error(str(e), extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        else:
            logger.error(
                "完整集群验收需指定 --kafka-home（以便调用 bin/ 下工具）。"
                "未指定时仅能做端口探测（无法验证 SASL、Quorum、副本与 Lag）。",
                extra={"to_stdout": True},
            )
            host, port = _parse_bootstrap_server(
                args.bootstrap_server or config.get("bootstrap_server", "localhost:9092"), DEFAULT_BROKER_PORT
            )
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    s.connect((host, port))
                logger.info(f"TCP 端口可连: {host}:{port}", extra={"to_stdout": True})
            except Exception as e:
                logger.error(f"TCP 无法连接 {host}:{port}: {e}", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        return

    # Topic / Group / 指标 / 配置 / Quorum / Broker 下线 运维子命令
    bootstrap = args.bootstrap_server or config.get("bootstrap_server", "localhost:9092")
    cmd_config, _ = _resolve_kafka_client_config(args, config, kafka_home)

    if getattr(args, "topic_create", False):
        _require(kafka_home, "--topic-create 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic")
        _require(topic, "--topic-create 需指定 --topic")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if mgr.create(topic, args.partitions, args.replication_factor) else EXIT_ERROR)

    if getattr(args, "topic_delete", False):
        _require(kafka_home, "--topic-delete 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic")
        _require(topic, "--topic-delete 需指定 --topic")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if mgr.delete(topic) else EXIT_ERROR)

    if getattr(args, "topic_describe", False):
        _require(kafka_home, "--topic-describe 需指定 --kafka-home")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe(_get_opt(args, config, "topic"))
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "topic_list", False):
        _require(kafka_home, "--topic-list 需指定 --kafka-home")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        out = mgr.list()
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "group_list", False):
        _require(kafka_home, "--group-list 需指定 --kafka-home")
        mgr = KafkaConsumerGroupManager(kafka_home, bootstrap, cmd_config)
        out = mgr.list_groups()
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "group_describe", False):
        _require(kafka_home, "--group-describe 需指定 --kafka-home")
        grp = _get_opt(args, config, "consumer_group")
        _require(grp, "--group-describe 需指定 --consumer-group")
        mgr = KafkaConsumerGroupManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe_group(grp)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "metrics", False) or getattr(args, "metrics_json", False):
        _require(kafka_home, "--metrics 需指定 --kafka-home")
        coll = KafkaMetricsCollector(kafka_home, bootstrap, cmd_config)
        data = coll.collect_all()
        if getattr(args, "metrics_json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("=== broker_connect ===")
            print(json.dumps(data.get("broker_connect", {}), ensure_ascii=False))
            print("=== Quorum ===")
            print(json.dumps(data.get("quorum", {}), ensure_ascii=False))
            print("=== Partition 健康（Under-Replicated）===")
            print(json.dumps(data.get("partition_health", {}), ensure_ascii=False))
            print("=== Topic 数量 ===")
            print(json.dumps(data.get("topic_inventory", {}), ensure_ascii=False))
            print("=== Consumer Group 数量 ===")
            print(json.dumps(data.get("consumer_group_inventory", {}), ensure_ascii=False))
            print("=== Consumer Lag ===")
            print(json.dumps(data.get("consumer_lag", {}), ensure_ascii=False))
        sys.exit(EXIT_OK)

    if getattr(args, "config_describe_broker", False):
        _require(kafka_home, "--config-describe-broker 需指定 --kafka-home")
        mgr = KafkaConfigManager(kafka_home, bootstrap, cmd_config)
        entity = _get_opt(args, config, "config_entity_name")
        bid = int(entity) if entity and str(entity).isdigit() else None
        out = mgr.describe_broker(bid)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "config_describe_topic", False):
        _require(kafka_home, "--config-describe-topic 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic") or _get_opt(args, config, "config_entity_name")
        _require(topic, "--config-describe-topic 需指定 --topic 或 --config-entity-name")
        mgr = KafkaConfigManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe_topic(topic)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "quorum_add_controller", False):
        _require(kafka_home, "--quorum-add-controller 需指定 --kafka-home")
        qm = KafkaQuorumManager(kafka_home, bootstrap_server=bootstrap, command_config=cmd_config)
        sys.exit(EXIT_OK if qm.add_controller() else EXIT_ERROR)

    if getattr(args, "broker_decommission_generate", False):
        _require(kafka_home, "--broker-decommission-generate 需指定 --kafka-home")
        blist = _get_opt(args, config, "broker_list")
        _require(blist, "--broker-decommission-generate 需指定 --broker-list（目标 Broker ID 列表）")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        out = dec.generate(blist, _get_opt(args, config, "topics_to_move_json"))
        if out:
            print(out)
            logger.info(
                "将 --generate 输出中的 reassignment JSON 保存为文件后传给 --broker-decommission-execute",
                extra={"to_stdout": True},
            )
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "broker_decommission_execute", False):
        _require(kafka_home, "--broker-decommission-execute 需指定 --kafka-home")
        path = _get_opt(args, config, "reassignment_json_file")
        _require(path, "--broker-decommission-execute 需指定 --reassignment-json-file")
        _require(Path(path).exists(), f"reassignment 文件不存在: {path}")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if dec.execute(path, _get_opt(args, config, "throttle")) else EXIT_ERROR)

    if getattr(args, "broker_decommission_verify", False):
        _require(kafka_home, "--broker-decommission-verify 需指定 --kafka-home")
        path = _get_opt(args, config, "reassignment_json_file")
        _require(path, "--broker-decommission-verify 需指定 --reassignment-json-file")
        _require(Path(path).exists(), f"reassignment 文件不存在: {path}")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if dec.verify(path) else EXIT_ERROR)

    _require(kafka_home, "必须指定 --kafka-home 或配置文件中的 kafka_home")

    try:
        deployer = KafkaDeployer(
            kafka_home,
            user=args.user or config.get("user", "kafka"),
            group=args.group or config.get("group", "kafka"),
            **_kafka_deployer_kwargs(args, config),
        )
    except ValueError as e:
        logger.error(str(e), extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

    clean_first = bool(getattr(args, "clean_first", False) or config.get("clean_first", False))

    if args.clean and clean_first:
        logger.error(
            "--clean 与 --clean-first 不要同时使用：仅卸载请用 --clean；要「先卸再装」请用 --deploy … --clean-first",
            extra={"to_stdout": True},
        )
        sys.exit(EXIT_ERROR)

    if args.clean:
        _dt_clean = args.deploy or config.get("deploy")
        if not _dt_clean:
            logger.error("--clean 需指定 --deploy standalone|controller|broker", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        deploy_kind_clean: str = str(_dt_clean)
        node_id = args.node_id or config.get("node_id")
        log_dirs_clean = args.log_dirs or config.get("log_dirs")
        meta_clean = args.metadata_log_dir or config.get("metadata_log_dir")
        clean_data = bool(getattr(args, "clean_data", False) or (bool(args.force) and bool(args.clean)))
        if not deployer.clean_deployment(
            deploy_kind_clean,
            node_id=node_id,
            backup_config=True,
            clean_data=clean_data,
            log_dirs=log_dirs_clean,
            metadata_log_dir=meta_clean,
        ):
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_OK)

    _dt_main = args.deploy or config.get("deploy")
    if not _dt_main:
        logger.error("必须指定 --deploy standalone|controller|broker 或配置文件中的 deploy", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    deploy_type: str = str(_dt_main)

    if clean_first:
        node_id_cf = args.node_id or config.get("node_id")
        log_dirs_cf = args.log_dirs or config.get("log_dirs")
        meta_cf = args.metadata_log_dir or config.get("metadata_log_dir")
        clean_data_cf = bool(getattr(args, "clean_data", False) or bool(args.force))
        if not deployer.clean_deployment(
            deploy_type,
            node_id=node_id_cf,
            backup_config=True,
            clean_data=clean_data_cf,
            log_dirs=log_dirs_cf,
            metadata_log_dir=meta_cf,
        ):
            sys.exit(EXIT_ERROR)

    enable_systemd = not (args.no_systemd or config.get("no_systemd", False))
    java_home = args.java_home or config.get("java_home")
    extra_properties = config.get("extra_properties") or config.get("server_properties")

    want_plain = bool(getattr(args, "deploy_sasl_plain", False) or config.get("deploy_sasl_plain"))
    want_ssl = bool(getattr(args, "deploy_sasl_ssl", False) or config.get("deploy_sasl_ssl"))
    if want_plain and want_ssl:
        logger.error("--deploy-sasl-plain 与 --deploy-sasl-ssl 不能同时使用", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    ssl_mat: Optional[Tuple[str, str, str, str, str]] = None
    if want_ssl:
        ssl_mat = _resolve_deploy_sasl_ssl_material(args, config)
    ku_acc: Optional[str] = None
    kp_acc: Optional[str] = None
    if want_plain:
        _, ku_acc, kp_acc = _resolve_deploy_sasl_plain_credentials(args, config)
    elif want_ssl:
        ku_acc, kp_acc = _resolve_kafka_sasl_account(args, config)
        if not ku_acc or not kp_acc:
            logger.error(
                "--deploy-sasl-ssl 需 --kafka-user 与 --kafka-password（或 KAFKA_SASL_*）",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
        mech_ssl = _get_opt(args, config, "kafka_sasl_mechanism") or os.getenv("KAFKA_SASL_MECHANISM") or "PLAIN"
        mech_ssl = (mech_ssl or "PLAIN").strip().upper()
        if mech_ssl != "PLAIN":
            logger.error("--deploy-sasl-ssl 部署侧当前仅支持 PLAIN", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
    need_sasl_creds = want_plain or want_ssl

    def _cid_mode_triple() -> Tuple[Optional[str], bool, bool]:
        cid_arg = args.cluster_id if getattr(args, "cluster_id", None) not in (None, "") else None
        if cid_arg is None:
            cid_arg = config.get("cluster_id")
        cid_s = (str(cid_arg).strip() if cid_arg is not None else "") or None
        gen = bool(getattr(args, "generate_cluster_id", False) or config.get("generate_cluster_id"))
        use_disk = bool(getattr(args, "use_disk_cluster_id", False) or config.get("use_disk_cluster_id"))
        return cid_s, gen, use_disk

    if deploy_type == "standalone":
        log_dirs = args.log_dirs or config.get("log_dirs")
        if not (log_dirs or "").strip():
            logger.error("standalone 部署必须指定 --log-dirs（或配置文件 log_dirs）", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        log_dirs = str(log_dirs).strip()
        node_id_sa = args.node_id if getattr(args, "node_id", None) is not None else config.get("node_id")
        if node_id_sa is None:
            logger.error("standalone 部署必须指定 --node-id（或配置文件 node_id）", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        cid_s, gen_cid, use_disk_cid = _cid_mode_triple()
        if bool(cid_s) + gen_cid + use_disk_cid != 1:
            logger.error(
                "standalone 部署须且仅能指定一种 cluster.id 来源：--cluster-id，或 --generate-cluster-id，或 --use-disk-cluster-id",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
        if not InputValidator.validate_path(log_dirs.split(",")[0].strip()):
            logger.error("无效的 --log-dirs 路径", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_standalone(
            log_dirs=log_dirs,
            cluster_id=cid_s,
            generate_cluster_id=gen_cid,
            use_disk_cluster_id=use_disk_cid,
            node_id=int(node_id_sa),
            listeners=None if need_sasl_creds else (args.listeners or config.get("listeners")),
            java_home=java_home,
            enable_systemd=enable_systemd,
            extra_properties=extra_properties,
            enable_sasl_plain=want_plain and not want_ssl,
            sasl_username=ku_acc if need_sasl_creds else None,
            sasl_password=kp_acc if need_sasl_creds else None,
            sasl_ssl_material=ssl_mat,
        )
    elif deploy_type == "controller":
        node_id = args.node_id if getattr(args, "node_id", None) is not None else config.get("node_id")
        if node_id is None:
            logger.error("部署 Controller 必须指定 --node-id（或配置文件 node_id）", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        quorum = args.controller_quorum_bootstrap_servers or config.get("controller_quorum_bootstrap_servers")
        if not quorum:
            logger.error("必须指定 --controller-quorum-bootstrap-servers", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        mld = args.metadata_log_dir or config.get("metadata_log_dir")
        lds = args.log_dirs or config.get("log_dirs")
        if not (mld or "").strip():
            logger.error(
                "部署 Controller 必须指定 --metadata-log-dir（或配置文件 metadata_log_dir）",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
        if not (lds or "").strip():
            logger.error("部署 Controller 必须指定 --log-dirs（或配置文件 log_dirs）", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        cid_s, gen_cid, use_disk_cid = _cid_mode_triple()
        if bool(cid_s) + gen_cid + use_disk_cid != 1:
            logger.error(
                "controller 部署须且仅能指定一种 cluster.id 来源：--cluster-id，或 --generate-cluster-id，或 --use-disk-cluster-id",
                extra={"to_stdout": True},
            )
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_controller(
            node_id=int(node_id),
            controller_quorum_bootstrap_servers=str(quorum).strip(),
            controller_listener_port=args.controller_port or config.get("controller_port", DEFAULT_CONTROLLER_PORT),
            metadata_log_dir=str(mld).strip(),
            log_dirs=str(lds).strip(),
            cluster_id=cid_s,
            generate_cluster_id=gen_cid,
            use_disk_cluster_id=use_disk_cid,
            initial_controllers=args.initial_controllers or config.get("initial_controllers"),
            java_home=java_home,
            enable_systemd=enable_systemd,
            extra_properties=extra_properties,
            enable_sasl_plain=want_plain and not want_ssl,
            sasl_username=ku_acc if need_sasl_creds else None,
            sasl_password=kp_acc if need_sasl_creds else None,
            sasl_ssl_material=ssl_mat,
        )
    else:  # broker
        node_id = args.node_id or config.get("node_id")
        if node_id is None:
            logger.error("部署 Broker 必须指定 --node-id", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        quorum = args.controller_quorum_bootstrap_servers or config.get("controller_quorum_bootstrap_servers")
        if not quorum:
            logger.error("必须指定 --controller-quorum-bootstrap-servers", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        log_dirs = args.log_dirs or config.get("log_dirs")
        if not log_dirs:
            logger.error("必须指定 --log-dirs", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        cluster_id = args.cluster_id or config.get("cluster_id")
        if not cluster_id:
            logger.error("Broker 必须指定 --cluster-id", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_broker(
            node_id=node_id,
            controller_quorum_bootstrap_servers=quorum,
            log_dirs=log_dirs,
            listeners=None if need_sasl_creds else (args.listeners or config.get("listeners")),
            cluster_id=cluster_id,
            java_home=java_home,
            enable_systemd=enable_systemd,
            extra_properties=extra_properties,
            enable_sasl_plain=want_plain and not want_ssl,
            sasl_username=ku_acc if need_sasl_creds else None,
            sasl_password=kp_acc if need_sasl_creds else None,
            sasl_ssl_material=ssl_mat,
        )

    if success:
        if enable_systemd:
            logger.info(
                "=== 部署成功：systemd 已为 active，且必备监听端口 TCP 探测通过 ===",
                extra={"to_stdout": True},
            )
        else:
            logger.info(
                "=== 部署完成（未使用 systemd）：配置与 storage format 已执行；进程未由本脚本启动，TCP 在线状态未检测 ===",
                extra={"to_stdout": True},
            )
        _log_post_deploy_production_practices(
            cast(str, deploy_type), want_plain and not want_ssl, want_ssl
        )

        if args.verify and deploy_type == "controller":
            time.sleep(2)
            ctrl_port = args.controller_port or config.get("controller_port", DEFAULT_CONTROLLER_PORT)
            bc_opt = _get_opt(args, config, "bootstrap_controller")
            bc_for_q: str = (str(bc_opt).strip() if bc_opt else "") or f"{_advertised_host()}:{ctrl_port}"
            cc, _auth_line = _resolve_kafka_client_config(args, config, kafka_home)
            if enable_systemd and not deployer.verify_controller_started("127.0.0.1", ctrl_port):
                logger.error("Controller 端口 TCP 验收失败", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
            if not deployer.verify_controller_quorum(bc_for_q, cc):
                logger.error("部署后 Controller 元数据验收未通过", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        elif args.verify and deploy_type in ("standalone", "broker"):
            time.sleep(2)
            if not deployer.verify_broker_started():
                sys.exit(EXIT_ERROR)
            bs = args.bootstrap_server or config.get("bootstrap_server")
            if not bs:
                bs = f"127.0.0.1:{DEFAULT_BROKER_PORT}"
            cc, auth_line = _resolve_kafka_client_config(args, config, kafka_home)
            if not deployer.show_cluster_status(bs, cc, auth_summary=auth_line):
                logger.error("部署后验收未通过（见上文报告）", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
    else:
        logger.error("=== 部署失败 ===", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n操作被用户中断", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    except Exception as exc:
        logger.error(f"程序异常: {exc}", exc_info=True, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
