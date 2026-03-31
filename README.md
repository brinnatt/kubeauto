# 1、运维手册

kubeauto 项目为快速部署 k8s 集群及 cloud native 应用而生。该项目通过 ansible 实现安装部署逻辑，python 实现控制逻辑，结构清晰明了，可以轻松上手，也可基于该项目进行二次开发，为各自企业提供坚实的云底座。

## 1.1、前言

### 1.1.1、最小配置

最小配置只是一个相对概念，根据测试经验得到，主要目的是给我们一个参考，再根据手上的资源灵活分配，安装一个可行的 k8s 集群，专门用来测试和学习，理论上是不应该用来生产的，因为配置低，活动空间小，无法满足生产需求。

| 部署场景                   | 节点角色            | CPU  | 内存  | 存储空间 | 常规组件                                                     |
| -------------------------- | ------------------- | ---- | ----- | -------- | ------------------------------------------------------------ |
| 单节点部署<br />（1 节点） | 控制节点 + 工作节点 | 4 核 | 16 GB | 60 GB    | • kube-apiserver（集群 API 服务）<br/>• kube-controller-manager（控制器）<br/>• kube-scheduler（调度器）<br/>• etcd（集群存储）<br/>• kubelet（节点代理）<br/>• kube-proxy（服务转发）<br/>• containerd / docker（任一 CRI 运行时）<br/>• CNI 插件（如 calico/flannel/cilium）<br/>• CoreDNS（集群内 DNS 服务）<br/>• kubeadm/kubectl（管理工具） |
| 多节点部署<br />（3 节点） | 控制节点            | 4 核 | 16 GB | 60 GB    | • kube-apiserver（集群 API 服务）<br/>• kube-controller-manager（控制器）<br/>• kube-scheduler（调度器）<br/>• etcd（集群存储）<br/>• kubelet（节点代理）<br/>• kube-proxy（服务转发）<br/>• containerd / docker（任一 CRI 运行时）<br/>• CNI 插件（如 calico/flannel/cilium）<br/>• CoreDNS（集群内 DNS 服务）<br/>• keepalived + haproxy / nginx（API Server VIP 负载均衡） |
| 多节点部署<br />（2 节点） | 工作节点            | 2 核 | 8 GB  | 30 GB    | • kubelet（节点代理）<br/>• kube-proxy（服务转发）<br/>• containerd / docker（任一 CRI 运行时）<br/>• CNI 插件（与控制面一致） |

### 1.1.2、架构

从最小配置描述中可以看到，我们的部署方案有两种，一种是 allinone 安装，一般用来学习和测试；一种是高可用安装，用于生产环境。

使用独立负载均衡器的高可用架构很经典，成熟且高效，如下图所示：

![k8s_tranditional_arch](./images/k8s_traditional_arch.png)

使用集成负载均衡器的高可用架构同样高效，如下图所示：

![k8s_new_arch](./images/k8s_new_arch.png)

本项目默认使用第二种集成负载均衡器的高可用架构。

### 1.1.3、容器运行时

k8s 作为一个容器编排工具，发展之初，借用稳定可靠的 docker 作为底层的容器运行时，顺理成章，但是 docker 是一个独立的 C/S 架构的容器工具，对容器有完整的生命周期定义，并非为 k8s 而生。

随着 k8s 的发展，社区有了一些新的理解，符合 CRI 标准的容器运行时皆可对接 k8s，其中比较突出的有 containerd，CRI-O 等，我们可以根据自己的实际需求灵活搭配。

本项目根据 k8s 版本提供不同的默认容器运行时：

- k8s 版本 < 1.24 时，支持 docker/containerd
- k8s 版本 >= 1.24 时，仅支持 containerd

从 kubecli 发布 v0.9.1 开始，推荐使用 containerd，想进一步了解 containerd，可参考社区文档：

- 安装指南 https://github.com/containerd/cri/blob/master/docs/installation.md
- 客户端 circtl 使用指南 https://github.com/containerd/cri/blob/master/docs/crictl.md
- man 文档 https://github.com/containerd/containerd/tree/master/docs/man

> 提示：可以从 k8s 的历史安装方法中加深对容器运行时的理解，参考[安装 Kubernetes](https://brinnatt.com/projects/cicd/6、安装-kubernetes)

## 1.2、单节点部署

单节点部署是以 allinone 的方式把所有组件都部署在一个节点上面，可以快速构建一个可用的 k8s，供学习和测试使用。

1、下载集群管理工具

```bash
# wget https://github.com/brinnatt/kubeauto/releases/download/v0.9.1/kubecli-amd64
# mv kubecli-amd64 /usr/local/bin/kubecli
# chmod +x /usr/local/bin/kubecli
# kubecli -h
usage: kubecli [-h] COMMAND ...

Kubeauto - Kubernetes cluster management tool

options:
  -h, --help    show this help message and exit

available commands:
  COMMAND
    new         Create a new cluster configuration
    setup       Setup a cluster with specific step
    list        List all managed clusters
    checkout    Switch to a cluster's kubeconfig
    start-aio   Quickly setup an all-in-one cluster with default settings
    start       Start all cluster services
    stop        Stop all cluster services
    upgrade     Upgrade the cluster components
    backup      Backup cluster state (etcd snapshot)
    restore     Restore cluster from backup
    destroy     Destroy the cluster
    add-etcd    Add an etcd node to the cluster
    add-master  Add a master node to the cluster
    add-node    Add a worker node to the cluster
    del-etcd    Remove an etcd node from the cluster
    del-master  Remove a master node from the cluster
    del-node    Remove a worker node from the cluster
    kca-renew   Force renew CA certificates and all other certs
    kcfg-adm    Manage kubeconfig users for the cluster
    download    Download required components with version control
    docker      Manage Docker containers
    system      Manage system environments
```

> 提示：该项目支持 amd64 和 arm64 两种架构，根据自己的系统架构选择对应的管理工具即可。

2、配置 kubecli 执行环境并下载 k8s 所需组件

```bash
# kubecli download -D
```

> 提示：kubecli 二进制包含所有的 python 库，但是不包含 ansible 工具，kubecli 会根据系统源安装 ansible 工具，如果安装源有问题导致安装失败，请自行手动安装，然后继续下面的步骤。

3、安装 aio 集群

使用 kubecli 工具安装 k8s 集群，要有 root 权限。

```bash
# kubecli start-aio
```

4、验证 aio 集群是否成功安装

```bash
# kubecli list
[2025-11-30 22:00:25] [INFO] [controller.cluster.cli] Managed clusters:
[2025-11-30 22:00:25] [INFO] [controller.cluster.cli]   -> 1: k8s-main
[2025-11-30 22:00:25] [INFO] [controller.cluster.cli] * -> 2: aio
# kubectl get nodes
NAME         STATUS   ROLES    AGE     VERSION
master-aio   Ready    master   2m46s   v1.33.6
# kubectl get pods -A
NAMESPACE     NAME                                       READY   STATUS    RESTARTS   AGE
kube-system   calico-kube-controllers-5d475c975d-jhlls   1/1     Running   0          2m44s
kube-system   calico-node-wtrn2                          1/1     Running   0          2m44s
kube-system   coredns-597f899fbb-sxfh7                   1/1     Running   0          2m21s
kube-system   metrics-server-88f86499-n5lzj              1/1     Running   0          2m20s
kube-system   node-local-dns-6qbst                       1/1     Running   0          2m20s
```

## 1.3、高可用部署

### 1.3.1、环境准备

| 角色        | 数量 | 描述                                                         |
| ----------- | ---- | ------------------------------------------------------------ |
| 部署节点    | 1    | 运行 kubecli 命令，管控多集群；可以复用 master 节点，但生产上建议准备一个单独的部署节点。 |
| etcd 节点   | 3    | etcd 集群需要 1, 3, 5, ... 奇数个节点，选举必需；可以复用 master 节点，但生产上建议使用性能高的磁盘单独部署。 |
| master 节点 | 2    | 高可用集群至少 2 个 master 节点                              |
| node 节点   | n    | 运行应用负载的节点，可根据需要提升机器配置或增加节点数       |

> 注意 1：集群时钟同步至关重要，时间一旦不同步，会出现无法预料的问题，且不易察觉，按下面步骤安装集群时，时间同步会自动初始化，你需要在安装完集群后，首先检查时间同步的有效性。
>
> 注意 2：默认配置下容器运行时和 kubelet 会占用 /var 的磁盘空间，如果磁盘分区特殊，可以设置 config.yml 中的容器运行时和 kubelet 数据目录：`CONTAINERD_STORAGE_DIR`，`DOCKER_STORAGE_DIR`，`KUBELET_ROOT_DIR`。
>
> 注意 3：确保在干净的系统上开始安装，不要使用曾经装过 kubeadm 或其他 k8s 发行版的环境。
>
> 注意 4：本项目开发环境是 Rockylinux 8.10，不建议使用低于该版本的系统安装 k8s 集群。

### 1.3.2、快速安装

以下示例创建一个 9 节点的多主高可用集群，文档中命令默认都需要 root 权限运行。

1、基础系统配置

- 2c/4g 内存 120g 硬盘（该配置仅测试用）
- 最小化安装 Rockylinux 8.10
- 配置基础网络、更新源、SSH 登录等

2、安装依赖

kubecli 把大部分依赖环境一起打包进了二进制，但 ansible_runner 库依赖操作系统的 ansible 环境，不过 `kubecli download -D` 在下载所有安装组件同时也解决了 ansible 依赖，如果因国内网络环境安装失败，可以手动安装 ansible 再继续后面步骤。

3、准备 ssh 免密登陆

配置从部署节点能够 ssh 免密登陆所有节点

```bash
# kubecli system -a 192.168.110.214 192.168.110.215 192.168.110.216 192.168.110.217 192.168.110.218 192.168.110.219 192.168.110.220 192.168.110.221 192.168.110.222
```

> 说明：如果密码相同，只需要输入一次，如果密码不同，需要输入各自的密码。

4、在部署节点安装 k8s 集群

4.1、下载对应的 x64 或 arm64 构架的 kubecli 工具

```bash
# wget https://github.com/brinnatt/kubeauto/releases/download/v0.9.1/kubecli-amd64
# mv kubecli-amd64 /usr/local/bin/kubecli
# chmod +x /usr/local/bin/kubecli
# kubecli -h
```

4.2、下载 k8s 所需所有组件以及 kubecli 运行时环境

```bash
# kubecli download -D
```

4.3、下载额外容器镜像（cilium，flannel，prometheus 等）

```bash
# kubecli download -E flannel
# kubecli download -E prometheus
```

4.4、创建集群配置实例

```bash
# kubecli new k8s-main
[2025-12-05 12:59:10] [INFO] [service.cluster.manager] -> Cluster k8s-main created. Next steps:
[2025-12-05 12:59:10] [INFO] [service.cluster.manager] 1. Configure /usr/local/kubeauto/clusters/k8s-main/hosts
[2025-12-05 12:59:10] [INFO] [service.cluster.manager] 2. Configure /usr/local/kubeauto/clusters/k8s-main/config.yml
```

然后根据提示配置 hosts 和 config.yml，根据前面节点规划修改hosts 文件和其他集群层面的主要配置选项；其他集群组件等配置项可以在config.yml 文件中修改。

4.5、开始安装

```bash
# 一键安装
kubecli setup k8s-main all

# 或者分步安装
kubecli setup k8s-main 01
kubecli setup k8s-main 02
...
```

### 1.3.3、分步安装

#### 1.3.3.1、创建证书和初始化环境

本步骤主要完成:

- (deprecated) role: os-harden，未更新上游项目，未验证最新 k8s 集群安装，不建议启用。但项目继续保留可选系统加固，需要客户根据实际情况自行适配，参见上游 [upstream](https://github.com/dev-sec/ansible-collection-hardening/tree/master/roles/os_hardening)
- (optional) role: chrony，集群中的时间同步至关重要，有条件的话，手动单独完成所有节点时间同步
- role: deploy，创建 CA 证书、集群组件访问 apiserver 所需的各种 kubeconfig
- role: prepare，系统基础环境初始化配置、分发 CA 证书、kubectl 客户端安装

##### 1.3.3.1.1、deploy 角色

> roles/deploy/tasks/main.yml

**1、创建 CA 证书**

kubernetes 系统各组件需要使用 TLS 证书对通信进行加密，使用 CloudFlare 的 PKI 工具集生成自签名的 CA 证书，用来签名后续创建的其它 TLS 证书。[参见官方项目](https://github.com/cloudflare/cfssl)。

根据认证对象可以将证书分成三类：服务器证书 `server cert`，客户端证书 `client cert`，对等证书 `peer cert`(既是 `server cert` 又是 `client cert`)，在kubernetes 集群中需要的证书种类如下：

- `etcd` 集群每个节点既需要标识自己服务的 `server cert`，也需要 `client cert` 与其它 `etcd` 集群节点交互，当然可以分别指定 2 个证书，为了更简洁，这里使用一个对等证书。
- `master` 节点既需要标识 apiserver 服务的 `server cert`，也需要 `client cert` 连接 `etcd` 集群，这里也使用一个对等证书。
- `kubectl` `calico` `kube-proxy` 只需要 `client cert`，因此证书请求中 `hosts` 字段可以为空。
- `kubelet` 既需要标识自己服务的 `server cert`，也需要 `client cert` 请求 `apiserver`，也使用一个对等证书。

整个集群要使用统一的 CA 证书，只需要在 ansible 控制端创建，然后分发给其他节点；为了保证安装的幂等性，如果已经存在 CA 证书，就跳过创建 CA 步骤。

创建 CA 配置文件 [ca-config.json.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/deploy/templates/ca-config.json.j2)

```bash
{
  "signing": {
    "default": {
      "expiry": "{{ CERT_EXPIRY }}"
    },
    "profiles": {
      "kubernetes": {
        "usages": [
            "signing",
            "key encipherment",
            "server auth",
            "client auth"
        ],
        "expiry": "{{ CERT_EXPIRY }}"
      },
      "kcfg": {
        "usages": [
            "signing",
            "key encipherment",
            "client auth"
        ],
        "expiry": "{{ CUSTOM_EXPIRY }}"
      }
    }
  }
}
```

- `signing`：表示该证书可用于签名其它证书；生成的 ca.pem 证书中 `CA=TRUE`；
- `server auth`：表示可以用该 CA 对 server 提供的证书进行验证；
- `client auth`：表示可以用该 CA 对 client 提供的证书进行验证；
- `profile kubernetes`：包含了 `server auth` 和 `client auth`，所以可以签发三种不同类型证书；expiry 证书有效期，默认 50 年。
- `profile kcfg`：在后面客户端 kubeconfig 证书管理中用到。

创建 CA 证书签名请求 [ca-csr.json.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/deploy/templates/ca-csr.json.j2)

```bash
{
  "CN": "kubernetes-ca",
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "k8s",
      "OU": "System"
    }
  ],
  "ca": {
    "expiry": "876000h"
  }
}
```

- `ca expiry`：指定 ca 证书的有效期，默认 100 年。

生成 CA 证书和私钥

```bash
cfssl gencert -initca ca-csr.json | cfssljson -bare ca
```

**2、生成 kubeconfig 配置文件**

kubectl 使用 `~/.kube/config` 配置文件与 kube-apiserver 进行交互，且拥有管理 K8S 集群的完全权限。

准备 kubectl 使用的 admin 证书签名请求 [admin-csr.json.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/deploy/templates/admin-csr.json.j2)

```bash
{
  "CN": "admin",
  "hosts": [],
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "system:masters",
      "OU": "System"
    }
  ]
}
```

- kubectl 使用客户端证书可以不指定 hosts 字段。
- 证书请求中 `O` 指定该证书的 Group 为 `system:masters`，而 `RBAC` 预定义的 `ClusterRoleBinding` 将 Group `system:masters` 与 ClusterRole `cluster-admin` 绑定，这就赋予了 kubectl **所有集群权限**。

```bash
$ kubectl describe clusterrolebinding cluster-admin
Name:         cluster-admin
Labels:       kubernetes.io/bootstrapping=rbac-defaults
Annotations:  rbac.authorization.kubernetes.io/autoupdate=true
Role:
  Kind:  ClusterRole
  Name:  cluster-admin
Subjects:
  Kind   Name            Namespace
  ----   ----            ---------
  Group  system:masters  
```

生成 admin 用户证书

```bash
cfssl gencert -ca=ca.pem -ca-key=ca-key.pem -config=ca-config.json -profile=kubernetes admin-csr.json | cfssljson -bare admin
```

生成 `~/.kube/config` 配置文件

使用 `kubectl config` 生成 kubeconfig 自动保存到 `~/.kube/config`，生成后 `cat ~/.kube/config` 可以验证配置文件包含 kube-apiserver 地址、证书、用户名等信息。

```bash
kubectl config set-cluster kubernetes --certificate-authority=ca.pem --embed-certs=true --server=127.0.0.1:8443
kubectl config set-credentials admin --client-certificate=admin.pem --embed-certs=true --client-key=admin-key.pem
kubectl config set-context kubernetes --cluster=kubernetes --user=admin
kubectl config use-context kubernetes
```

**3、生成 kube-proxy.kubeconfig 配置文件**

创建 kube-proxy 证书请求 [kube-proxy-csr.json.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/deploy/templates/kube-proxy-csr.json.j2)

```bash
{
  "CN": "system:kube-proxy",
  "hosts": [],
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "k8s",
      "OU": "System"
    }
  ]
}
```

- kube-proxy 使用客户端证书可以不指定 hosts 字段。
- CN 指定该证书的 User 为 `system:kube-proxy`，预定义的 ClusterRoleBinding `system:node-proxier` 将 User `system:kube-proxy` 与 Role `system:node-proxier` 绑定，授予了调用 kube-apiserver Proxy 相关 API 的权限；

```bash
$ kubectl describe clusterrolebinding system:node-proxier
Name:         system:node-proxier
Labels:       kubernetes.io/bootstrapping=rbac-defaults
Annotations:  rbac.authorization.kubernetes.io/autoupdate=true
Role:
  Kind:  ClusterRole
  Name:  system:node-proxier
Subjects:
  Kind  Name               Namespace
  ----  ----               ---------
  User  system:kube-proxy  
```

生成 `system:kube-proxy` 用户证书

```bash
cfssl gencert -ca=ca.pem -ca-key=ca-key.pem -config=ca-config.json -profile=kubernetes kube-proxy-csr.json | cfssljson -bare kube-proxy
```

生成 kube-proxy.kubeconfig

使用 `kubectl config` 生成 kubeconfig 自动保存到 kube-proxy.kubeconfig

```bash
kubectl config set-cluster kubernetes --certificate-authority=ca.pem --embed-certs=true --server=127.0.0.1:8443 --kubeconfig=kube-proxy.kubeconfig
kubectl config set-credentials kube-proxy --client-certificate=kube-proxy.pem --embed-certs=true --client-key=kube-proxy-key.pem --kubeconfig=kube-proxy.kubeconfig
kubectl config set-context default --cluster=kubernetes --user=kube-proxy --kubeconfig=kube-proxy.kubeconfig
kubectl config use-context default --kubeconfig=kube-proxy.kubeconfig
```

**4、生成其它组件 kubeconfig 配置文件**

创建 kube-controller-manager 和 kube-scheduler 组件的 kubeconfig 文件，过程与创建 kube-proxy.kubeconfig 类似，略。

##### 1.3.3.1.2、prepare 角色

请在另外窗口打开[roles/prepare/tasks/main.yml](https://github.com/brinnatt/kubeauto/blob/master/roles/prepare/tasks/main.yml) 文件，比较简单直观

1. 设置基础操作系统软件和系统参数，请阅读脚本中的注释内容。
2. 创建一些基础文件目录、环境变量以及添加本地镜像仓库 `registry.talkschool.cn` 的域名解析。
3. 分发 kubeconfig 等配置文件。

#### 1.3.3.2、安装 etcd 集群

kuberntes 集群使用 etcd 存储所有数据，是最重要的组件之一，注意 etcd 集群需要奇数个节点(1, 3, 5 ...)，本文档使用 3 个节点做集群。

请在另外窗口打开 [roles/etcd/tasks/main.yml](https://github.com/brinnatt/kubeauto/blob/master/roles/etcd/tasks/main.yml) 文件，对照看以下讲解内容。

1、创建 etcd 证书

> 注意：证书是在部署节点创建好之后推送到目标 etcd 节点上去的，以增加 ca 证书的安全性

创建 ectd 证书请求 [etcd-csr.json.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/etcd/templates/etcd-csr.json.j2)

```bash
{
  "CN": "etcd",
  "hosts": [
{% for host in groups['etcd'] %}
    "{{ host }}",
{% endfor %}
    "127.0.0.1"
  ],
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "k8s",
      "OU": "System"
    }
  ]
}
```

- etcd 使用对等证书，hosts 字段必须指定授权使用该证书的 etcd 节点 IP，这里枚举了所有 ectd 节点的地址。

2、创建 etcd 服务文件 [etcd.service.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/etcd/templates/etcd.service.j2)

```bash
[Unit]
Description=Etcd Server
After=network.target
After=network-online.target
Wants=network-online.target
Documentation=https://github.com/coreos

[Service]
Type=notify
WorkingDirectory={{ ETCD_DATA_DIR }}
ExecStart={{ bin_dir }}/etcd \
  --name=etcd-{{ inventory_hostname }} \
  --cert-file={{ ca_dir }}/etcd.pem \
  --key-file={{ ca_dir }}/etcd-key.pem \
  --peer-cert-file={{ ca_dir }}/etcd.pem \
  --peer-key-file={{ ca_dir }}/etcd-key.pem \
  --trusted-ca-file={{ ca_dir }}/ca.pem \
  --peer-trusted-ca-file={{ ca_dir }}/ca.pem \
  --initial-advertise-peer-urls=https://{{ inventory_hostname }}:2380 \
  --listen-peer-urls=https://{{ inventory_hostname }}:2380 \
  --listen-client-urls=https://{{ inventory_hostname }}:2379,http://127.0.0.1:2379 \
  --advertise-client-urls=https://{{ inventory_hostname }}:2379 \
  --initial-cluster-token=etcd-cluster-0 \
  --initial-cluster={{ ETCD_NODES }} \
  --initial-cluster-state={{ CLUSTER_STATE }} \
  --data-dir={{ ETCD_DATA_DIR }} \
  --wal-dir={{ ETCD_WAL_DIR }} \
  --snapshot-count=50000 \
  --auto-compaction-retention=1 \
  --auto-compaction-mode=periodic \
  --max-request-bytes=10485760 \
  --quota-backend-bytes=8589934592
Restart=always
RestartSec=15
LimitNOFILE=65536
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
```

- 完整参数列表请使用 `etcd --help` 查询
- 注意 etcd 即需要服务器证书也需要客户端证书，为了方便这里使用一个 peer 证书代替两个证书。
- `--initial-cluster-state` 值为 `new` 时，`--name` 的参数值必须位于 `--initial-cluster` 列表中。
- `--snapshot-count` `--auto-compaction-retention` 一些性能优化参数，请查阅 etcd 项目文档。
- 设置 `--data-dir` 和 `--wal-dir` 使用不同磁盘目录，可以避免磁盘 io 竞争，提高性能，具体请参考 etcd 项目文档。

3、验证 etcd 集群状态

- systemctl status etcd 查看服务状态
- journalctl -u etcd 查看运行日志
- 在任一 etcd 集群节点上执行如下命令

```bash
# 根据hosts中配置设置shell变量 $NODE_IPS
export NODE_IPS="192.168.1.1 192.168.1.2 192.168.1.3"
for ip in ${NODE_IPS}; do
  etcdctl \
  --endpoints=https://${ip}:2379  \
  --cacert=/etc/kubernetes/ssl/ca.pem \
  --cert=/etc/kubernetes/ssl/etcd.pem \
  --key=/etc/kubernetes/ssl/etcd-key.pem \
  endpoint health; done
https://192.168.110.220:2379 is healthy: successfully committed proposal: took = 6.082221ms
https://192.168.110.221:2379 is healthy: successfully committed proposal: took = 4.447803ms
https://192.168.110.222:2379 is healthy: successfully committed proposal: took = 5.397452ms
```

```bash
for ip in ${NODE_IPS}; do
  etcdctl \
  --endpoints=https://${ip}:2379  \
  --cacert=/etc/kubernetes/ssl/ca.pem \
  --cert=/etc/kubernetes/ssl/etcd.pem \
  --key=/etc/kubernetes/ssl/etcd-key.pem \
  --write-out=table endpoint status; done
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
|           ENDPOINT           |        ID        | VERSION | STORAGE VERSION | DB SIZE | IN USE | PERCENTAGE NOT IN USE | QUOTA  | IS LEADER | IS LEARNER | RAFT TERM | RAFT INDEX | RAFT APPLIED INDEX | ERRORS | DOWNGRADE TARGET VERSION | DOWNGRADE ENABLED |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
| https://192.168.110.220:2379 | a68459b5c6f543d5 |   3.6.4 |           3.6.0 |  6.4 MB | 1.7 MB |                   74% | 8.6 GB |      true |      false |         8 |      32691 |              32691 |        |                          |             false |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
|           ENDPOINT           |        ID        | VERSION | STORAGE VERSION | DB SIZE | IN USE | PERCENTAGE NOT IN USE | QUOTA  | IS LEADER | IS LEARNER | RAFT TERM | RAFT INDEX | RAFT APPLIED INDEX | ERRORS | DOWNGRADE TARGET VERSION | DOWNGRADE ENABLED |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
| https://192.168.110.221:2379 | b3e2d7b9c045016c |   3.6.4 |           3.6.0 |  6.4 MB | 1.6 MB |                   75% | 8.6 GB |     false |      false |         8 |      32691 |              32691 |        |                          |             false |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
|           ENDPOINT           |        ID        | VERSION | STORAGE VERSION | DB SIZE | IN USE | PERCENTAGE NOT IN USE | QUOTA  | IS LEADER | IS LEARNER | RAFT TERM | RAFT INDEX | RAFT APPLIED INDEX | ERRORS | DOWNGRADE TARGET VERSION | DOWNGRADE ENABLED |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
| https://192.168.110.222:2379 | 1471d7a200f308b9 |   3.6.4 |           3.6.0 |  6.4 MB | 1.6 MB |                   75% | 8.6 GB |     false |      false |         8 |      32691 |              32691 |        |                          |             false |
+------------------------------+------------------+---------+-----------------+---------+--------+-----------------------+--------+-----------+------------+-----------+------------+--------------------+--------+--------------------------+-------------------+
```

- 所有节点健康：etcdctl endpoint health 对所有三个节点都返回 healthy；
- 有且仅有一个领导者：etcdctl endpoint status 显示一个节点 is leader: true，另外两个节点 is leader: false；
- Raft 任期一致：所有节点处于同一 Raft term，且近期未发生频繁 Leader 切换；
- Raft 索引同步：各节点 Raft index 与 Raft applied index 基本一致，日志同步正常；
- 无活跃告警：etcdctl alarm list 返回空。
- 节点间网络稳定：没有频繁的领导者切换（通过监控 etcd_server_leader_changes_seen_total 指标）。
- 磁盘空间充足：没有 NOSPACE 告警，且磁盘使用率在安全阈值内（例如低于80%）。

> **磁盘性能**：快速的磁盘是 etcd 部署性能和稳定性的最关键因素。
>
> 磁盘速度慢会增加 etcd 请求延迟，并可能损害集群稳定性。由于 etcd 的 raft 协议依赖于将元数据持久地存储到日志中，因此大多数 etcd 集群成员必须将每个请求写入磁盘。此外，etcd 还会逐步将其状态检查点写入磁盘，以便截断此日志。如果这些写入耗时过长，心跳可能会超时并触发选举，从而损害集群的稳定性。通常，要判断磁盘速度是否足以满足 etcd 的要求，可以使用 fio 等基准测试工具。
>
> etcd 对磁盘写入延迟非常敏感。通常需要 50 的顺序 IOPS（例如，7200 RPM 磁盘）。对于负载较重的集群，建议使用 500 的顺序 IOPS（例如，典型的本地 SSD 或高性能虚拟化块设备）。请注意，大多数云提供商发布的是并发 IOPS，而不是顺序 IOPS；发布的并发 IOPS 可能比顺序 IOPS 高出 10 倍。要测量实际的顺序 IOPS，我们建议使用磁盘基准测试工具，例如 diskbench 或 fio。
>
> ```bash
> # 测试示例
> mkdir test-data
> fio --rw=write --ioengine=sync --fdatasync=1 --directory=test-data --size=2200m --bs=2300 --name=mytest
> ```

#### 1.3.3.3、安装容器运行时

项目根据 k8s 版本提供不同的默认容器运行时：

- k8s 版本 < 1.24 时，支持 docker 或 containerd
- k8s 版本 >= 1.24 时，仅支持 containerd

kubeauto 集成安装 containerd：

- 注意：k8s 1.24 以后，项目已经设置默认容器运行时为 containerd，无需手动修改
- 执行安装：分步安装 `kubecli setup k8s-main 03`，一键安装 `kubecli setup k8s-main all`

命令对比：

| 命令           | docker         | crictl（推荐）  | ctr                    |
| -------------- | -------------- | --------------- | ---------------------- |
| 查看容器列表   | docker ps      | crictl ps       | ctr -n k8s.io c ls     |
| 查看容器详情   | docker inspect | crictl inspect  | ctr -n k8s.io c info   |
| 查看容器日志   | docker logs    | crictl logs     | 无                     |
| 容器内执行命令 | docker exec    | crictl exec     | 无                     |
| 挂载容器       | docker attach  | crictl attach   | 无                     |
| 容器资源使用   | docker stats   | crictl stats    | 无                     |
| 创建容器       | docker create  | crictl create   | ctr -n k8s.io c create |
| 启动容器       | docker start   | crictl start    | ctr -n k8s.io run      |
| 停止容器       | docker stop    | crictl stop     | 无                     |
| 删除容器       | docker rm      | crictl rm       | ctr -n k8s.io c del    |
| 查看镜像列表   | docker images  | crictl images   | ctr -n k8s.io i ls     |
| 查看镜像详情   | docker inspect | crictl inspecti | 无                     |
| 拉取镜像       | docker pull    | crictl pull     | ctr -n k8s.io i pull   |
| 推送镜像       | docker push    | 无              | ctr -n k8s.io i push   |
| 删除镜像       | docker rmi     | crictl rmi      | ctr -n k8s.io i rm     |
| 查看Pod列表    | 无             | crictl pods     | 无                     |
| 查看Pod详情    | 无             | crictl inspectp | 无                     |
| 启动Pod        | 无             | crictl runp     | 无                     |
| 停止Pod        | 无             | crictl stopp    | 无                     |

> 提示：如果你觉得 crictl 和 ctr 不顺手，那就使用 [nerdctl](https://github.com/containerd/nerdctl)，跟 docker 的使用方式一样。

#### 1.3.3.4、安装 kube_master 节点

部署 master 节点主要包含三个组件 `apiserver` `scheduler` `controller-manager`，其中：

- apiserver 提供集群管理的 REST API 接口，包括认证授权、数据校验以及集群状态变更等
  - 只有 API Server 才直接操作 etcd
  - 其他模块通过 API Server 查询或修改数据
  - 提供其他模块之间的数据交互和通信的枢纽
- scheduler 负责分配调度 Pod 到集群内的 node 节点
  - 监听 kube-apiserver，查询还未分配 Node 的 Pod
  - 根据调度策略为这些 Pod 分配节点
- controller-manager 由一系列的控制器组成，它通过 apiserver 监控整个集群的状态，并确保集群处于预期的工作状态

**高可用机制：**

- apiserver：无状态服务，可以通过独立负载均衡器或集成负载均衡器实现高可用，如前言中架构描述。
- controller-manager：通过开启 `--leader-elect=true`，多副本选举，成为 leader 才能提供服务，其它副本不提供服务，leader 选举机制是基于 k8s 资源锁实现的，k8s 支持 ConfigMap 和 Endpoint 两种类型的资源锁，谁抢到锁，谁就是 leader。
- scheduler：同 controller-manager。

**安装流程：**

```bash
cat playbooks/04.kube-master.yml
- hosts: kube_master
  roles:
  - kube-lb        # 四层负载均衡，监听在127.0.0.1:6443，转发到真实master节点apiserver服务
  - kube-master    #
  - kube-node      # 因为网络、监控等daemonset组件，master节点也推荐安装kubelet和kube-proxy服务
  ... 
```

**创建 kubernetes 证书签名请求：**

```bash
{
  "CN": "kubernetes",
  "hosts": [
    "127.0.0.1",
{% if groups['ex_lb']|length > 0 %}
    "{{ hostvars[groups['ex_lb'][0]]['EX_APISERVER_VIP'] }}",
{% endif %}
{% for host in groups['kube_master'] %}
    "{{ host }}",
{% endfor %}
    "{{ CLUSTER_KUBERNETES_SVC_IP }}",
{% for host in MASTER_CERT_HOSTS %}
    "{{ host }}",
{% endfor %}
    "kubernetes",
    "kubernetes.default",
    "kubernetes.default.svc",
    "kubernetes.default.svc.cluster",
    "kubernetes.default.svc.cluster.local"
  ],
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "k8s",
      "OU": "System"
    }
  ]
}
```

kubernetes apiserver 使用对等证书，创建时 hosts 字段需要配置：

- 如果配置 ex_lb，需要把 EX_APISERVER_VIP 也配置进去
- 如果需要外部访问 apiserver，可选在 config.yml 配置 MASTER_CERT_HOSTS
- `kubectl get svc` 将看到集群中由 api-server 创建的默认服务 `kubernetes`，因此也要把 `kubernetes` 服务名和各个服务域名也添加进去

**创建 apiserver 的服务配置文件：**

```bash
[Unit]
Description=Kubernetes API Server
Documentation=https://github.com/GoogleCloudPlatform/kubernetes
After=network.target

[Service]
ExecStart={{ bin_dir }}/kube-apiserver \
  --allow-privileged=true \
  --anonymous-auth=false \
  --api-audiences=api,istio-ca \
  --authorization-mode=Node,RBAC \
  --bind-address={{ inventory_hostname }} \
  --client-ca-file={{ ca_dir }}/ca.pem \
  --endpoint-reconciler-type=lease \
  --etcd-cafile={{ ca_dir }}/ca.pem \
  --etcd-certfile={{ ca_dir }}/kubernetes.pem \
  --etcd-keyfile={{ ca_dir }}/kubernetes-key.pem \
  --etcd-servers={{ ETCD_ENDPOINTS }} \
  --kubelet-certificate-authority={{ ca_dir }}/ca.pem \
  --kubelet-client-certificate={{ ca_dir }}/kubernetes.pem \
  --kubelet-client-key={{ ca_dir }}/kubernetes-key.pem \
  --secure-port={{ SECURE_PORT }} \
  --service-account-issuer=https://kubernetes.default.svc \
  --service-account-signing-key-file={{ ca_dir }}/ca-key.pem \
  --service-account-key-file={{ ca_dir }}/ca.pem \
  --service-cluster-ip-range={{ SERVICE_CIDR }} \
  --service-node-port-range={{ NODE_PORT_RANGE }} \
  --tls-cert-file={{ ca_dir }}/kubernetes.pem \
  --tls-private-key-file={{ ca_dir }}/kubernetes-key.pem \
  --requestheader-client-ca-file={{ ca_dir }}/ca.pem \
  --requestheader-allowed-names= \
  --requestheader-extra-headers-prefix=X-Remote-Extra- \
  --requestheader-group-headers=X-Remote-Group \
  --requestheader-username-headers=X-Remote-User \
  --proxy-client-cert-file={{ ca_dir }}/aggregator-proxy.pem \
  --proxy-client-key-file={{ ca_dir }}/aggregator-proxy-key.pem \
  --enable-aggregator-routing=true \
  --v=2
Restart=always
RestartSec=5
Type=notify
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

- Kubernetes 对 API 访问需要依次经过认证、授权和准入控制(admission controll)，认证解决用户是谁的问题，授权解决用户能做什么的问题，Admission Control 则是资源管理方面的作用。
- 关于 authorization-mode=Node,RBAC v1.7+ 支持 Node 授权，配合 NodeRestriction 准入控制来限制 kubelet 仅可访问 node、endpoint、pod、service 以及 secret、configmap、PV 和 PVC 等相关的资源；需要注意的是 v1.7 中 Node 授权是默认开启的，v1.8 中需要显式配置开启，否则 Node 无法正常工作
- 详细参数配置请参考 `kube-apiserver --help`，关于认证、授权和准入控制可以参考阅读[社区文档](https://github.com/feiskyer/kubernetes-handbook/blob/master/components/apiserver.md)
- 增加了访问 kubelet 使用的证书配置，防止匿名访问 kubelet 的安全漏洞。

**创建 controller-manager 的服务文件：**

```bash
[Unit]
Description=Kubernetes Controller Manager
Documentation=https://github.com/GoogleCloudPlatform/kubernetes

[Service]
ExecStart={{ bin_dir }}/kube-controller-manager \
  --allocate-node-cidrs=true \
  --authentication-kubeconfig=/etc/kubernetes/kube-controller-manager.kubeconfig \
  --authorization-kubeconfig=/etc/kubernetes/kube-controller-manager.kubeconfig \
  --bind-address=0.0.0.0 \
  --cluster-cidr={{ CLUSTER_CIDR }} \
  --cluster-name=kubernetes \
  --cluster-signing-cert-file={{ ca_dir }}/ca.pem \
  --cluster-signing-key-file={{ ca_dir }}/ca-key.pem \
  --kubeconfig=/etc/kubernetes/kube-controller-manager.kubeconfig \
  --leader-elect=true \
  --node-cidr-mask-size={{ NODE_CIDR_LEN }} \
  --root-ca-file={{ ca_dir }}/ca.pem \
  --service-account-private-key-file={{ ca_dir }}/ca-key.pem \
  --service-cluster-ip-range={{ SERVICE_CIDR }} \
  --use-service-account-credentials=true \
  --v=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- --cluster-cidr：指定 Cluster 中 Pod 的 CIDR 范围，该网段在各 Node 间必须路由可达(flannel/calico 等网络插件实现)
- --service-cluster-ip-range：参数指定 Cluster 中 Service 的 CIDR 范围，必须和 kube-apiserver 中的参数一致
- `--cluster-signing-*`：指定的证书和私钥文件用来签名为 TLS BootStrap 创建的证书和私钥
- --root-ca-file：用来对 kube-apiserver 证书进行校验，指定该参数后，才会在 Pod 容器的 ServiceAccount 中放置该 CA 证书文件
- --leader-elect=true：如上面高可用机制所述，使用多节点选主的方式选择主节点。只有主节点才会启动所有控制器，而其他从节点则仅执行选主算法并作为故障备用

**创建 scheduler 的服务文件：**

```bash
[Unit]
Description=Kubernetes Scheduler
Documentation=https://github.com/GoogleCloudPlatform/kubernetes

[Service]
ExecStart={{ bin_dir }}/kube-scheduler \
  --authentication-kubeconfig=/etc/kubernetes/kube-scheduler.kubeconfig \
  --authorization-kubeconfig=/etc/kubernetes/kube-scheduler.kubeconfig \
  --bind-address=0.0.0.0 \
  --kubeconfig=/etc/kubernetes/kube-scheduler.kubeconfig \
  --leader-elect=true \
  --v=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- --leader-elect=true：如上面高可用机制所述，使用多节点选主的方式选择主节点。只有主节点才会启动所有控制器，而其他从节点则仅执行选主算法并作为故障备用

**master 节点安装 node 服务 kubelet kube-proxy**

本项目默认使用 DaemonSet 方式安装网络插件，如果 master 节点不安装 kubelet 服务，就无法启动容器，也就不能安装网络插件；

如果 master 节点不安装网络插件，那么通过 `apiserver` 方式无法访问 `dashboard` `kibana` 等 POD 资源。

> 注意：在 master 节点也同时成为 node 节点后，默认业务 POD 也会调度到 master 节点；可以使用 `kubectl cordon` 命令禁止业务 POD 调度到 master 节点。

**master 集群的验证**

运行 `kubecli setup k8s-main 04.kube-master.yml` 成功后，验证 master 节点的主要组件：

```bash
# 查看进程状态
systemctl status kube-apiserver
systemctl status kube-controller-manager
systemctl status kube-scheduler
# 查看进程运行日志
journalctl -u kube-apiserver
journalctl -u kube-controller-manager
journalctl -u kube-scheduler
```

#### 1.3.3.5、安装 kube_node 节点

kube_node 是集群中运行工作负载的节点，前置条件需要先部署好 kube_master 节点，kube_node 需要部署如下组件：

```bash
cat playbooks/05.kube-node.yml
- hosts: kube_node
  roles:
  - { role: kube-lb, when: "inventory_hostname not in groups['kube_master']" }
  - { role: kube-node, when: "inventory_hostname not in groups['kube_master']" }
```

- kube-lb：由 nginx 裁剪编译的四层负载均衡，用于将请求转发到主节点的 apiserver 服务
- kubelet：kube_node 上最主要的组件，管理容器、数据采集、日志等 k8s 资源
- kube-proxy：发布应用服务与负载均衡

**创建 cni 基础网络插件配置文件：**

因为后续需要用 `DaemonSet Pod` 方式运行 k8s 网络插件，所以 kubelet.server 服务必须开启 cni 相关参数，并且提供 cni 网络配置文件。

**创建 kubelet 的服务文件：**

- 根据官方建议独立使用 kubelet 配置文件，详见 [roles/kube-node/templates/kubelet-config.yaml.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/kube-node/templates/kubelet-config.yaml.j2)
- 必须先创建工作目录 `/var/lib/kubelet`

```bash
[Unit]
Description=Kubernetes Kubelet
Documentation=https://github.com/GoogleCloudPlatform/kubernetes

[Service]
WorkingDirectory=/var/lib/kubelet
ExecStartPre=/bin/mount -o remount,rw '/sys/fs/cgroup'
{% if KUBE_RESERVED_ENABLED == "yes" or SYS_RESERVED_ENABLED == "yes" %}
# Kubernetes 组件
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpu/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpuacct/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpuset/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/memory/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/pids/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/systemd/podruntime.slice

# 系统服务（systemd 服务）
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpu/system.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpuacct/system.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpuset/system.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/memory/system.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/pids/system.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/systemd/system.slice

ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/hugetlb/podruntime.slice
ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/hugetlb/system.slice
{% endif %}
ExecStart={{ bin_dir }}/kubelet \
  --config=/var/lib/kubelet/config.yaml \
  --container-runtime-endpoint=unix:///run/containerd/containerd.sock \
  --hostname-override={{ K8S_NODENAME }} \
  --kubeconfig=/etc/kubernetes/kubelet.kubeconfig \
  --root-dir={{ KUBELET_ROOT_DIR }} \
  --v=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- `--ExecStartPre=/bin/mkdir -p xxx`：对于某些系统（centos7）**cpuset 和 hugetlb 控制器默认不初始化 system.slice**，需要手动创建，否则在启用 `--kube-reserved-cgroup` 时会报错，因为 kubelet 尝试在这些控制器下创建子目录时会失败。

  ```bash
  Failed to start ContainerManager Failed to enforce System Reserved Cgroup Limits
  ```

- 关于 kubelet 资源预留相关配置请参考 https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/

**创建 kube-proxy kubeconfig 文件：**

该步骤已经在 deploy 节点完成，[roles/deploy/tasks/main.yml](https://github.com/brinnatt/kubeauto/blob/master/roles/deploy/tasks/main.yml)

- 生成的 kube-proxy.kubeconfig 配置文件需要移动到 /etc/kubernetes/ 目录，后续 kube-proxy 服务启动参数里面需要指定。

**创建 kube-proxy 服务文件：**

```bash
[Unit]
Description=Kubernetes Kube-Proxy Server
Documentation=https://github.com/GoogleCloudPlatform/kubernetes
After=network.target

[Service]
WorkingDirectory=/var/lib/kube-proxy
ExecStart={{ bin_dir }}/kube-proxy \
  --config=/var/lib/kube-proxy/kube-proxy-config.yaml
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

请注意 [kube-proxy-config](https://github.com/brinnatt/kubeauto/blob/master/roles/kube-node/templates/kube-proxy-config.yaml.j2) 文件的注释说明。

验证 node 状态：

```bash
systemctl status kubelet	# 查看状态
systemctl status kube-proxy
journalctl -u kubelet		# 查看日志
journalctl -u kube-proxy 
```

#### 1.3.3.6、安装网络组件

首先回顾下 K8S 网络设计原则，在配置集群网络插件或者部署 K8S 应用、服务时，请参考以下总结：

- 每个 Pod 都拥有一个独立 IP 地址，Pod 内所有容器共享一个网络命名空间
- 集群内所有 Pod 都在一个直接连通的扁平网络中，可通过 IP 直接访问
  - 所有容器之间无需 NAT 就可以直接互相访问
  - 所有 Node 和所有容器之间无需 NAT 就可以直接互相访问
  - 容器自己看到的 IP 跟其他容器看到的一样
- Service cluster IP 只可在集群内部访问，外部请求需要通过 NodePort、LoadBalance 或者 Ingress 来访问

`Container Network Interface (CNI)` 是目前 CNCF 主推的网络模型，它由两部分组成：

- CNI Plugin 负责给容器配置网络，它包括两个基本的接口
  - 配置网络：AddNetwork(net *NetworkConfig, rt *RuntimeConf) (types.Result, error)
  - 清理网络：DelNetwork(net *NetworkConfig, rt *RuntimeConf) error
- IPAM Plugin 负责给容器分配 IP 地址

Kubernetes Pod 的网络是这样创建的：

- 每个 Pod 除了创建时指定的容器外，都有一个 kubelet 启动时指定的`基础容器`，即 `pause` 容器
- kubelet 创建`基础容器`生成 network namespace
- kubelet 调用网络 CNI driver，由它根据配置调用具体的 CNI 插件
- CNI 插件给`基础容器`配置网络
- Pod 中其他的容器共享使用`基础容器`的网络

本项目基于 CNI driver 调用各种网络插件来配置 kubernetes 的网络，常用 CNI 插件有 `flannel` `calico` `cilium`等等，这些插件各有优势，也在互相借鉴学习优点。

比如在所有 node 节点都在一个二层网络时候，flannel 提供 hostgw 实现，避免 vxlan 实现的 udp 封装开销，估计是目前最高效的；calico 也针对 L3 Fabric，推出了 IPinIP 的选项，利用了 GRE 隧道封装；因此这些插件都能适合很多实际应用场景。

项目当前内置支持的网络插件有：`calico` `cilium` `flannel` `kube-ovn` `kube-router`

##### 1.3.3.6.1、安装 calico 网络

calico 是 k8s 社区最流行的网络插件之一，也是 k8s-conformance test 默认使用的网络插件，功能丰富，支持 network policy；是当前 kubeauto 项目的默认网络插件。

以下是 calico 的工作机制流程图，大致方向没有问题，有助于排查细节问题。

```bash
+================================================================================+
|                          Kubernetes Control Plane                              |
|                                                                                |
|  kube-apiserver                                                                |
|                                                                                |
|  - Stores Calico CRDs (authoritative state only, no IP allocation)             |
|    * IPPool                                                                    |
|    * BlockAffinity                                                             |
|    * BGPPeer                                                                   |
|    * BGPConfiguration                                                          |
|    * NetworkPolicy / GlobalNetworkPolicy                                       |
|                                                                                |
|  Example: CRD YAML snippet                                                     |
|    apiVersion: projectcalico.org/v3                                            |
|    kind: IPPool                                                                |
|    metadata:                                                                   |
|      name: ippool-1                                                            |
|    spec:                                                                       |
|      cidr: 192.168.0.0/16                                                      |
|      ipipMode: CrossSubnet                                                     |
|      vxlanMode: Never                                                          |
|      natOutgoing: true                                                         |
|      disabled: false                                                           |
|                                                                                |
+================================================================================+
                                   |
                                   | watch / sync (CRD)
                                   v
+================================================================================+
|                 calico-kube-controllers (Deployment)                           |
|                                                                                |
|  node-controller                                                               |
|                                                                                |
|  - Watches Kubernetes Node lifecycle                                           |
|  - Creates BlockAffinity per node                                              |
|  - Reclaims unused IP blocks                                                   |
|                                                                                |
|  Example: node-controller config                                               |
|    FELIX_CONFIGURATION (ConfigMap / environment variables)                     |
|      DATASTORE_TYPE: "kubernetes"                                              |
|      ETCD_ENDPOINTS: "https://etcd:2379"                                       |
|      IPAM_TYPE: "calico-ipam"                                                  |
|                                                                                |
|  Notes:                                                                        |
|  - Does NOT allocate Pod IPs                                                   |
|  - Does NOT program kernel routes                                              |
|                                                                                |
+================================================================================+
                                   |
                                   | watch CRD
                                   v
+================================================================================+
|                    calico-node (DaemonSet, per node)                           |
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                                Felix                                       ||
|  |                                                                            ||
|  |  Control Plane -> Data Plane Translation                                   ||
|  |                                                                            ||
|  |  - Watches CRDs and Kubernetes API                                         ||
|  |  - Determines dataplane behavior                                           ||
|  |      * Encapsulation: IPIP / VXLAN / WireGuard                             ||
|  |      * Local routing view                                                  ||
|  |      * NetworkPolicy enforcement                                           ||
|  |                                                                            ||
|  |  - Programs Linux kernel                                                   ||
|  |      * ip route (Pod /32 or Node CIDR)                                     ||
|  |      * iptables / nftables / tc / eBPF                                     ||
|  |      * Creates tunnel interfaces                                           ||
|  |        - tunl0 (IPIP)                                                      ||
|  |        - vxlan.calico (VXLAN)                                              ||
|  |        - wireguard.calico (WireGuard)                                      ||
|  |                                                                            ||
|  |  Felix Configuration (ConfigMap / Environment)                             ||
|  |    FELIX_IPV6SUPPORT: "false"                                              ||
|  |    FELIX_IPV4POOL_CIDR: "192.168.0.0/16"                                   ||
|  |    FELIX_ENCAPSULATION: "IPIP"                                             ||
|  |    FELIX_BPFENABLED: "true"                                                ||
|  |                                                                            ||
|  |  NOTE: Felix does NOT run BGP                                              ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                           calico-ipam                                      ||
|  |                                                                            ||
|  |  - Allocates Pod IPs from IP blocks                                        ||
|  |  - Honors BlockAffinity ownership                                          ||
|  |  - Persists allocation state in etcd/datastore                             ||
|  |                                                                            ||
|  |  Example: calico-ipam config                                               ||
|  |    IPAM_TYPE: "calico-ipam"                                                ||
|  |    AUTO_ASSIGN_BLOCK_SIZE: 26                                              ||
|  |    DATASTORE_TYPE: "kubernetes"                                            ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                           BIRD / BIRDv2                                    ||
|  |                                                                            ||
|  |  BGP Control Plane                                                         ||
|  |                                                                            ||
|  |  - Establishes BGP sessions (TCP 179)                                      ||
|  |  - Peers with other nodes or Route Reflectors                              ||
|  |                                                                            ||
|  |  - Advertises routes (mode dependent):                                     ||
|  |      * Pod /32 routes (pure BGP, no encapsulation)                         ||
|  |      * Node CIDR routes (IPIP / VXLAN / WireGuard modes)                   ||
|  |  - Learns remote routes and exports them to Felix                          ||
|  |                                                                            ||
|  |  BIRD Configuration example (bird.conf / bird6.conf)                       ||
|  |    router id 192.168.0.101                                                 ||
|  |    protocol bgp Node-to-Node {                                             ||
|  |      local as 64512                                                        ||
|  |      neighbor 192.168.0.102 as 64512                                       ||
|  |      multihop 2;                                                           ||
|  |    }                                                                       ||
|  |                                                                            ||
|  |  NOTE: BIRD does NOT enforce policy or program kernel rules                ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
+================================================================================+
                                   |
                                   | iBGP / eBGP
                                   v
+================================================================================+
|                       Calico Route Reflectors (Detailed)                       |
|                                                                                |
|  Configuration / Working Mechanism:                                            |
|    ┌───────────────────────────────────────────────────────────────┐           |
|    │ 1. Configure RR cluster ID on node:                           │           |
|    │    calicoctl patch node <node> -p '{"spec":{"bgp":            │           | 
|    │    {"routeReflectorClusterID":"244.0.0.1"}}}'                 │           |
|    │                                                               │           |
|    │ 2. Label node as RR:                                          │           |
|    │    calicoctl patch node <node> -p '{"metadata":{"labels":     │           |
|    │    {"route-reflector":"true"}}}'                              │           |
|    │                                                               │           |
|    │ 3. Create BGPPeer to connect all nodes to RR nodes:           │           |
|    │    kind: BGPPeer                                              │           |
|    │    spec:                                                      │           |
|    │      nodeSelector: all()       # matches all nodes            │           |
|    │      peerSelector: route-reflector == 'true'                  │           |
|    │                                                               │           |
|    │ 4. Disable full node-to-node mesh:                            │           |
|    │    kind: BGPConfiguration                                     │           |
|    │    spec:                                                      │           |
|    │      nodeToNodeMeshEnabled: false                             │           |
|    │      asNumber: 64512                                          │           |
|    │                                                               │           |
|    │ 5. Working mechanism:                                         │           |
|    │      * RR receives BGP routes from all calico-nodes           │           |
|    │      * RR reflects routes to other nodes                      │           |
|    │      * Reduces full-mesh BGP connections                      │           |
|    │      * RR nodes do not run Felix or enforce policies          │           |
|    └───────────────────────────────────────────────────────────────┘           |
+================================================================================+
                                   |
                                   | route propagation
                                   v
+================================================================================+
|                        Linux Kernel Data Plane                                 |
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                        Kernel Routing Table                                ||
|  |                                                                            ||
|  |  Local Pod routes:                                                         ||
|  |    192.168.1.2/32  dev caliXXXX  scope link                                ||
|  |                                                                            ||
|  |  Remote routes (examples):                                                 ||
|  |    192.168.1.66/32 via 192.168.0.101 dev eth0                              ||
|  |    192.168.2.0/26  via tunl0 / vxlan.calico                                ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                  Encapsulation (controlled by IPPool)                      ||
|  |                                                                            ||
|  |  ipipMode: Detailed                                                        ||
|  |    ┌──────────────────────────────────────────────────────────────────┐    ||
|  |    │ Never       -> Direct L3 routing, no encapsulation               │    ||
|  |    │              * Linux kernel installs Pod /32 routes directly     │    || 
|  |    │              * Minimal CPU / MTU overhead                        │    ||
|  |    │ Always      -> Encapsulate all cross-node traffic                │    ||
|  |    │              * Creates tunl0 device                              │    ||
|  |    │              * Wraps Pod traffic in IPIP header                  │    ||
|  |    │              * Decapsulates on destination node                  │    ||
|  |    │ CrossSubnet -> Encapsulate only if nodes are in different subnets│    ||
|  |    │              * Local subnet traffic stays unencapsulated         │    ||
|  |    │              * Cross-subnet traffic uses tunl0                   │    ||
|  |    │              * Balances encapsulation overhead and isolation     │    ||
|  |    └──────────────────────────────────────────────────────────────────┘    ||
|  |                                                                            ||
|  |  vxlanMode: Never / Always / CrossSubnet                                   ||
|  |    -> vxlan.calico                                                         ||
|  |  wireguardEnabled: true                                                    ||
|  |    -> wireguard.calico                                                     ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                 Cross-Node Pod Traffic Flow                                ||
|  |                                                                            ||
|  |  Pod A (192.168.1.2)                                                       ||
|  |    -> veth                                                                 ||
|  |    -> caliXXXX                                                             ||
|  |    -> routing lookup                                                       ||
|  |    -> eth0 / tunl0 / vxlan.calico / wireguard.calico                       ||
|  |    -> Node B                                                               ||
|  |    -> decapsulation (if enabled)                                           ||
|  |    -> caliYYYY                                                             ||
|  |    -> Pod B (192.168.1.66)                                                 ||
|  +----------------------------------------------------------------------------+|
|                                                                                |
|  +----------------------------------------------------------------------------+|
|  |                    NetworkPolicy Enforcement                               ||
|  |                                                                            ||
|  |  Implemented by Felix at multiple hook points                              ||
|  |    - iptables / nftables                                                   ||
|  |    - tc                                                                    ||
|  |    - eBPF (eBPF dataplane mode)                                            ||
|  +----------------------------------------------------------------------------+|
+================================================================================+
```

如果需要安装 calico，请在 `clusters/xxxx/hosts` 文件中设置变量 `CLUSTER_NETWORK="calico"`。

```bash
$ tree .
.
|-- tasks
|   |-- calico-rr.yml
|   `-- main.yml
|-- templates
|   |-- bgp-default.yaml.j2
|   |-- bgp-rr.yaml.j2
|   |-- calico-csr.json.j2
|   |-- calico-v3.24.yaml.j2
|   |-- calico-v3.26.yaml.j2
|   |-- calico-v3.28.yaml.j2
|   `-- calicoctl.cfg.j2
`-- vars
    `-- main.yml
```

请在另外窗口打开 [roles/calico/tasks/main.yml](https://github.com/brinnatt/kubeauto/blob/master/roles/calico/tasks/main.yml) 文件，对照看以下讲解内容。

**创建 calico 证书申请：**

```bash
{
  "CN": "calico",
  "hosts": [],
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "CN",
      "ST": "HangZhou",
      "L": "XS",
      "O": "k8s",
      "OU": "System"
    }
  ]
}
```

calico 作为客户端连接 etcd，证书申请 hosts 字段可以为空：

- 服务器证书：用于 HTTPS 服务端验证，必须包含服务的域名/IP（如 `etcd-server.local`, `192.168.1.100`），客户端通过验证证书中的 `hosts` 来确认连接的是正确的服务器。
- 客户端证书：用于客户端身份认证，`hosts` 字段通常为空或包含客户端自身的标识。etcd 通过验证证书的 `CN`（Common Name）和 `O`（Organization）字段来识别客户端身份并授权。
- etcd 使用 **TLS 双向认证**：服务器验证客户端证书，客户端验证服务器证书。
  - 当 Calico 组件作为客户端连接 etcd 时，只需要提供有效的客户端证书，不需要在证书中声明要访问的服务端地址。
  - etcd 服务器端会检查客户端证书的 `CN` 和 `O` 字段，根据这些信息进行 RBAC 授权。

Calico 证书使用场景：

- calico/node 组件：运行在每个节点的 `calico-node` 容器访问 etcd 集群时，使用此客户端证书进行身份认证。
- Calico CNI 插件：当节点上的 CNI 插件（二进制文件）需要直接与 etcd 通信来注册 Pod 网络端点时，使用此证书。
- calicoctl 命令行工具：管理员使用 `calicoctl` 管理 Calico 资源（如创建 NetworkPolicy、查看 IP 池）时，需要此证书连接 etcd。
- calico/kube-controllers 控制器：该控制器同步 Kubernetes 资源到 Calico 数据存储时，通过此证书访问 etcd。

> 我们项目的 calico 使用的是 etcd 数据存储模式，也可以使用 **Kubernetes API 数据存储模式**。
>
> Kubernetes API 数据存储模式下 Calico 组件通过 ServiceAccount Token 与 Kubernetes API Server 通信，**不再需要单独的 etcd 客户端证书**。
>
> https://docs.tigera.io/calico/latest/operations/install-apiserver
>
> https://docs.tigera.io/calico/latest/operations/calicoctl/configure/kdd
>
> 说明（与 kubectl apply projectcalico.org/v3）：默认 etcd 存储安装不会在集群里注册 GlobalNetworkPolicy、HostEndpoint 等 CRD，因此 kubectl apply 这类清单会报 no matches for kind，但 calico-node 仍可正常运行。主机侧 Calico 策略请使用 calicoctl（本仓库下发的 /etc/calico/calicoctl.cfg 与证书）。使用 tools/k8stools/CalicoPolicyCli.py 做主机端口收敛时请加 --executor calicoctl，预演用 plan（calicoctl 无 kubectl 的 dry-run=server）；详见该脚本内 MAINTAINER_DOC 第 7 节。

**创建 calico DaemonSet yaml 文件和 rbac 文件：**

请对照 [roles/calico/templates/calico-v3.28.yaml.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/calico/templates/calico-v3.28.yaml.j2) 文件注释和以下注意内容

- 详细配置参数请参考 [calico官方文档](https://projectcalico.docs.tigera.io/reference/node/configuration)
- 配置 ETCD_ENDPOINTS 、CA、证书等，所有 `"{{ }}"` 变量与 ansible hosts 文件中设置对应
- 配置集群 POD 网络 CALICO_IPV4POOL_CIDR={{ CLUSTER_CIDR }}
- 配置 FELIX_DEFAULTENDPOINTTOHOSTACTION=ACCEPT 默认允许 Pod 到 Node 的网络流量，更多 [felix配置选项](https://projectcalico.docs.tigera.io/reference/felix/configuration)

**安装 calico 网络：**

- 安装前检查主机名，不能有大写字母，只能由 `小写字母` `-` `.`组成，calico-node v3.0.6 以上已经解决主机大写字母问题。
- 安装前必须确保各节点主机名不重复，calico node name 由节点主机名决定，如果重复，那么重复节点在 etcd 中只存储一份配置，BGP 邻居也不会建立。
- 安装之前必须确保 `kube_master` 和 `kube_node` 节点已经成功部署
- 删除前面安装 kube_node 时默认的 cni 网络配置，轮询等待 calico 网络插件安装完成

[可选]配置 calicoctl 工具 [calicoctl.cfg.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/calico/templates/calicoctl.cfg.j2)

```bash
apiVersion: projectcalico.org/v3
kind: CalicoAPIConfig
metadata:
spec:
  datastoreType: "etcdv3"
  etcdEndpoints: {{ ETCD_ENDPOINTS }}
  etcdKeyFile: /etc/calico/ssl/calico-key.pem
  etcdCertFile: /etc/calico/ssl/calico.pem
  etcdCACertFile: {{ ca_dir }}/ca.pem
```

**验证 calico 网络：**

执行 calico 安装成功后可以验证如下：(需要等待镜像下载完成，有时候即便上一步已经配置了 docker 国内加速，还是可能比较慢，请确认以下容器运行起来以后，再执行后续验证步骤)

```bash
kubectl get pods -A
NAMESPACE     NAME                                       READY   STATUS    RESTARTS        AGE
kube-system   calico-kube-controllers-5d475c975d-kjmtm   1/1     Running   4 (2m25s ago)   2d17h
kube-system   calico-node-9b8b7                          1/1     Running   3 (2m32s ago)   2d17h
kube-system   calico-node-dsv65                          1/1     Running   3 (2m26s ago)   2d17h
kube-system   calico-node-jmcw2                          1/1     Running   0               18h
kube-system   calico-node-lrhhg                          1/1     Running   3 (2m25s ago)   2d17h
kube-system   calico-node-qx7qd                          1/1     Running   3 (2m36s ago)   2d17h
kube-system   coredns-597f899fbb-srj8q                   1/1     Running   3 (2m26s ago)   2d17h
kube-system   metrics-server-88f86499-8psq5              1/1     Running   4 (2m26s ago)   2d17h
kube-system   node-local-dns-5l7g4                       1/1     Running   3 (2m36s ago)   2d17h
kube-system   node-local-dns-dv64k                       1/1     Running   3 (2m26s ago)   2d17h
kube-system   node-local-dns-rcqlg                       1/1     Running   0               18h
kube-system   node-local-dns-vlf7l                       1/1     Running   3 (2m32s ago)   2d17h
kube-system   node-local-dns-zfvfb                       1/1     Running   3 (2m25s ago)   2d17h
```

**查看网卡和路由信息：**

```bash
kubectl run test --image=busybox -- sleep 3600
```

```bash
ip add show |grep -A 4 cali
10: cali1037a54e65e@if3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1480 qdisc noqueue state UP group default qlen 1000
    link/ether ee:ee:ee:ee:ee:ee brd ff:ff:ff:ff:ff:ff link-netns cni-e1078f04-ae36-2c6e-3536-b6d7831b08f7
    inet6 fe80::ecee:eeff:feee:eeee/64 scope link 
       valid_lft forever preferred_lft forever
```

- 可以看到包含类似 cali1cxxx 的网卡，是 calico 为测试 pod 生成的
- tunl0 网卡现在不用管，是默认生成的，当开启 IPIP 特性时使用的隧道

```bash
# 查看路由
route -n
Kernel IP routing table
Destination     Gateway         Genmask         Flags Metric Ref    Use Iface
0.0.0.0         192.168.110.1   0.0.0.0         UG    100    0        0 ens160
172.20.37.192   0.0.0.0         255.255.255.192 U     0      0        0 *
172.20.37.195   0.0.0.0         255.255.255.255 UH    0      0        0 cali1037a54e65e
172.20.133.128  192.168.110.216 255.255.255.192 UG    0      0        0 tunl0
172.20.171.0    192.168.110.217 255.255.255.192 UG    0      0        0 tunl0
172.20.184.64   192.168.110.214 255.255.255.192 UG    0      0        0 tunl0
172.20.222.0    192.168.110.215 255.255.255.192 UG    0      0        0 tunl0
192.168.110.0   0.0.0.0         255.255.255.0   U     100    0        0 ens160
```

**查看所有 calico 节点状态：**

```bash
calicoctl node status
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 08:50:40 | Established |
| 192.168.110.215 | node-to-node mesh | up    | 08:50:40 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 08:50:40 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 08:52:28 | Established |
+-----------------+-------------------+-------+----------+-------------+
```

**BGP 协议是通过 TCP 连接来建立邻居的，因此可以用 netstat 命令验证 BGP Peer**

```bash
netstat -antlp|grep ESTABLISHED|grep 179
tcp        0      0 192.168.110.218:54547   192.168.110.215:179     ESTABLISHED 2262/bird           
tcp        0      0 192.168.110.218:56349   192.168.110.217:179     ESTABLISHED 2262/bird           
tcp        0      0 192.168.110.218:38797   192.168.110.216:179     ESTABLISHED 2262/bird           
tcp        0      0 192.168.110.218:56419   192.168.110.214:179     ESTABLISHED 2262/bird         
```

**查看 etcd 中 calico 相关信息：**

因为这里 calico 网络使用 etcd 存储数据，所以可以在 etcd 集群中查看数据

- calico 3.x 版本默认使用 etcd v3 存储，**登录集群的一个etcd 节点**，查看命令：

```bash
# 查看所有calico相关数据
ETCDCTL_API=3 etcdctl --endpoints="http://127.0.0.1:2379" get --prefix /calico
# 查看 calico网络为各节点分配的网段
ETCDCTL_API=3 etcdctl --endpoints="http://127.0.0.1:2379" get --prefix /calico/ipam/v2/host
```

---

###### 1.3.3.6.1.1、BGP Route Reflectors

`Calico` 作为 `k8s` 的一个流行网络插件，它依赖 `BGP` 路由协议实现集群节点上的 `POD` 路由互通；而路由互通的前提是节点间建立 BGP Peer 连接。BGP 路由反射器（Route Reflectors，简称 RR）可以简化集群 BGP Peer 的连接方式，它是解决 BGP 扩展性问题的有效方式；具体来说：

- 没有 RR 时，所有节点之间需要两两建立连接（IBGP 全互联），节点数量增加将导致连接数剧增、资源占用剧增
- 引入 RR 后，其他 BGP 路由器只需要与它建立连接并交换路由信息，节点数量增加连接数只是线性增加，节省系统资源

calico-node 版本 v3.3 开始支持内建路由反射器，非常方便，因此使用 calico 作为网络插件可以支持大规模节点数的 `K8S` 集群。

- 建议集群节点数大于 50 时，应用 BGP Route Reflectors 特性

**前提条件：**

k8s 集群使用 calico 网络插件部署成功。实验环境是 3 个 master，3 个 worker，3 个 etcd，calico 版本 v3.28.4。

```bash
# kubectl get nodes
NAME        STATUS                     ROLES    AGE     VERSION
master-01   Ready,SchedulingDisabled   master   3d23h   v1.33.6
master-02   Ready,SchedulingDisabled   master   3d23h   v1.33.6
master-03   Ready,SchedulingDisabled   master   47h     v1.33.6
worker-01   Ready                      node     3d23h   v1.33.6
worker-02   Ready                      node     3d23h   v1.33.6
worker-03   Ready                      node     3m28s   v1.33.6
# kubectl get pods -A |grep calico
kube-system   calico-kube-controllers-5d475c975d-kjmtm   1/1     Running   5 (10h ago)   3d23h
kube-system   calico-node-9b8b7                          1/1     Running   4 (10h ago)   3d23h
kube-system   calico-node-dsv65                          1/1     Running   4 (10h ago)   3d23h
kube-system   calico-node-jmcw2                          1/1     Running   1 (10h ago)   47h
kube-system   calico-node-lrhhg                          1/1     Running   4 (10h ago)   3d23h
kube-system   calico-node-qx7qd                          1/1     Running   4 (10h ago)   3d23h
kube-system   calico-node-w57lk                          1/1     Running   0             3m36s
```

查看当前集群中 BGP 连接情况：可以看到集群中 4 个节点两两建立了 BGP 连接

```bash
# ansible -i /usr/local/kubeauto/clusters/k8s-main/hosts kube_master,kube_node -m shell -a 'calicoctl node status'
192.168.110.214 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.215 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.218 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.219 | node-to-node mesh | up    | 13:57:46 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.218 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.215 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.219 | node-to-node mesh | up    | 13:57:46 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.217 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.215 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.218 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.219 | node-to-node mesh | up    | 13:57:46 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.216 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.215 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.218 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.219 | node-to-node mesh | up    | 13:57:46 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.215 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.218 | node-to-node mesh | up    | 03:18:21 | Established |
| 192.168.110.219 | node-to-node mesh | up    | 13:57:46 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.219 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+-------------------+-------+----------+-------------+
|  PEER ADDRESS   |     PEER TYPE     | STATE |  SINCE   |    INFO     |
+-----------------+-------------------+-------+----------+-------------+
| 192.168.110.214 | node-to-node mesh | up    | 13:57:47 | Established |
| 192.168.110.215 | node-to-node mesh | up    | 13:57:47 | Established |
| 192.168.110.216 | node-to-node mesh | up    | 13:57:47 | Established |
| 192.168.110.217 | node-to-node mesh | up    | 13:57:47 | Established |
| 192.168.110.218 | node-to-node mesh | up    | 13:57:47 | Established |
+-----------------+-------------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
```

---

###### 1.3.3.6.1.2、kubecli 启用 route reflector

- 修改 `/usr/local/kubeauto/clusters/k8s-main/config.yml` 文件，设置配置项 `CALICO_RR_ENABLED: true`
- 重新执行网络安装 `kubecli setup k8s-main 07`

执行完成，检查 bgp 连接验证即可。

###### 1.3.3.6.1.3、手动安装 route reflector

参考官方[Calico bgp route reflector 配置](https://docs.tigera.io/calico/latest/networking/configuring/bgp#configure-a-node-to-act-as-a-route-reflector)

选定节点并配置 Route Reflector，首先查看当前集群中的节点：

```bash
# calicoctl get node -o wide
NAME        ASN       IPV4                 IPV6   
master-01   (64512)   192.168.110.214/24          
master-02   (64512)   192.168.110.215/24          
master-03   (64512)   192.168.110.216/24          
worker-01   (64512)   192.168.110.217/24          
worker-02   (64512)   192.168.110.218/24          
worker-03   (64512)   192.168.110.219/24          
```

可以在集群中选择 1 个或多个节点作为 rr 节点，这里先选择节点 master-01

```bash
# 配置routeReflectorClusterID
calicoctl patch node master-01 -p '{"spec": {"bgp": {"routeReflectorClusterID": "244.0.0.1"}}}'

# 配置node label
calicoctl patch node master-01 -p '{"metadata": {"labels": {"route-reflector": "true"}}}'
```

> `routeReflectorClusterID`：BGP 集群标识符，防止路由环路，通常是 IPv4 格式的地址，在同一 AS 内必须唯一，当有多个 RR 时，所有 RR 应使用相同的 ClusterID
>
> `route-reflector`：通过 kv 标签标识 RR 节点，便于后续的节点选择器匹配。

配置 BGP node 与 Route Reflector 的连接建立规则：

```bash
# 让所有节点（nodeSelector: all()）与所有 RR 节点（peerSelector）建立 BGP 连接。
$ cat << EOF | calicoctl create -f -
kind: BGPPeer
apiVersion: projectcalico.org/v3
metadata:
  name: peer-with-route-reflectors
spec:
  nodeSelector: all()	# 匹配所有节点
  peerSelector: route-reflector == 'true'	# 连接到带有此标签的节点
EOF
```

配置全局禁用全连接（BGP full mesh）：

```bash
# 必须禁用全连接，否则 RR 配置无效，节点间仍会建立全连接。
$ cat << EOF | calicoctl create -f -
apiVersion: projectcalico.org/v3
kind: BGPConfiguration
metadata:
  name: default
spec:
  logSeverityScreen: Info
  nodeToNodeMeshEnabled: false	# 关键：关闭节点间全连接
  asNumber: 64512
EOF
```

验证增加 rr 之后的 bgp 连接情况：

```bash
# ansible -i /usr/local/kubeauto/clusters/k8s-main/hosts kube_master,kube_node -m shell -a 'calicoctl node status'
192.168.110.214 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.215 | node specific | up    | 13:52:58 | Established |
| 192.168.110.216 | node specific | up    | 13:52:58 | Established |
| 192.168.110.217 | node specific | up    | 13:52:58 | Established |
| 192.168.110.218 | node specific | up    | 13:52:58 | Established |
| 192.168.110.219 | node specific | up    | 13:52:58 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.218 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.214 | node specific | up    | 13:52:58 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.217 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.214 | node specific | up    | 13:52:58 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.216 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.214 | node specific | up    | 13:52:58 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.215 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.214 | node specific | up    | 13:52:59 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
192.168.110.219 | CHANGED | rc=0 >>
Calico process is running.

IPv4 BGP status
+-----------------+---------------+-------+----------+-------------+
|  PEER ADDRESS   |   PEER TYPE   | STATE |  SINCE   |    INFO     |
+-----------------+---------------+-------+----------+-------------+
| 192.168.110.214 | node specific | up    | 13:52:59 | Established |
+-----------------+---------------+-------+----------+-------------+

IPv6 BGP status
No IPv6 peers found.
```

可以看到所有其他节点都与所选 rr 节点建立 bgp 连接。

> 可以继续增加一个 rr 节点，步骤同上，添加成功后可以看到所有其他节点都与两个 rr 节点建立 bgp 连接，两个 rr 节点之间也建立 bgp 连接。对于节点数较多的 `K8S` 集群建议配置 2-3 个 RR 节点。

##### 1.3.3.6.2、安装 flannel 网络

`Flannel` 是最早应用到 k8s 集群的网络插件之一，简单高效，且提供多个后端 `backend` 模式供选择；本文介绍以 `DaemonSet Pod` 方式集成到 k8s 集群，需要在所有 master 节点和 worker 节点安装。

**kubeauto 集成安装 flannel：**

- 设置 `/usr/local/kubeauto/clusters/xxx/hosts` 文件中变量 `CLUSTER_NETWORK="flannel"`
- 下载额外镜像 `kubecli download -E flannel`
- 执行集群安装 `kubecli setup xxx all`

Flannel 的核心职责是：

1. **管理集群的子网分配**：为每个节点分配一个 Pod 子网（保存在 etcd 或 Kubernetes API 中）。
2. **提供网络配置模板**：它告诉每个节点上的 CNI “如何配置网络”。

Flannel 的典型配置文件（`/etc/cni/net.d/10-flannel.conflist`）结构如下：

```bash
{
  "name": "cbr0",
  "cniVersion": "0.3.1",
  "plugins": [
    {
      "type": "flannel",
      "delegate": {
        "hairpinMode": true,
        "isDefaultGateway": true
      }
    },
    {
      "type": "portmap",
      "capabilities": {
        "portMappings": true
      }
    },
    {
      "type": "bandwidth",
      "capabilities": {
        "bandwidth": true
      }
    }
  ]
}
```

Flannel 的配置文件定义了一个**插件链**，它自己作为链中的第一个插件，负责提供集群网络信息，然后**委托（调用）** 像 `bridge` 这样的底层插件去干活，最后还可以让 `portmap` 等插件进行功能增强。

这种设计使得 Flannel 非常灵活，可以专注于集群范围的子网管理，而将具体的网络设备操作委托给更专业的插件。以下是 flannel 的工作机制流程图，大致方向没有问题，有助于排查细节问题。

```bash
+================================================================================+
|                          Kubernetes Control Plane                              |
|                                                                                |
|  kube-apiserver                                                                |
|                                                                                |
|  - Allocates and records PodCIDR per Node                                      |
|                                                                                |
|    Node.spec.podCIDR = 10.244.X.0/24                                           |
|                                                                                |
+================================================================================+
                                   |
                                   | watch / patch
                                   v
+================================================================================+
|                         flanneld (DaemonSet)                                   |
|                         One flanneld per Node                                  |
|                                                                                |
|  1. Retrieve local Node PodCIDR                                                |
|     - Source: Kubernetes API                                                   |
|                                                                                |
|  2. Initialize backend                                                         |
|     - vxlan      -> prepare VXLAN device                                       |
|     - host-gw    -> prepare direct routing                                     |
|     - wireguard  -> prepare encrypted tunnel                                   |
|                                                                                |
|  3. Create backend resources                                                   |
|     - vxlan      -> create flannel.1                                           |
|     - host-gw    -> no tunnel, route only                                      |
|                                                                                |
|  4. Write runtime network configuration (CORE)                                 |
|                                                                                |
|     /run/flannel/subnet.env                                                    |
|     +----------------------------------------------------------------------+   |
|     | FLANNEL_NETWORK=10.244.0.0/16                                          | |
|     | FLANNEL_SUBNET=10.244.1.1/24                                           | |
|     | FLANNEL_MTU=1450                                                       | |
|     | FLANNEL_BACKEND=vxlan                                                  | |
|     +----------------------------------------------------------------------+   |
|                                                                                |
|  5. Configure inter-node forwarding                                            |
|     - vxlan   -> FDB entries + ARP + UDP 8472                                  |
|     - host-gw -> ip route add                                                  |
|                                                                                |
|  NOTE: flanneld does NOT create Pod interfaces                                 |
|        flanneld does NOT run CNI lifecycle                                     |
|                                                                                |
+================================================================================+
                                   |
                                   | configuration only
                                   v
+================================================================================+
|              /etc/cni/net.d/10-flannel.conflist (CRITICAL)                     |
|                                                                                |
|  Generated during Flannel installation                                         |
|                                                                                |
|  {                                                                             |
|    "name": "cbr0",                                                             |
|    "plugins": [                                                                |
|      {                                                                         |
|        "type": "flannel",                                                      |
|        "delegate": {                                                           |
|          "type": "bridge",                                                     |
|          "bridge": "cni0",                                                     |
|          "isGateway": true,                                                    |
|          "ipMasq": false,                                                      |
|          "hairpinMode": true                                                   |
|        }                                                                       |
|      },                                                                        |
|      {                                                                         |
|        "type": "portmap",                                                      |
|        "capabilities": { "portMappings": true }                                |
|      }                                                                         |
|    ]                                                                           |
|  }                                                                             |
|                                                                                |
|  Semantics:                                                                    |
|  - flannel CNI reads subnet.env                                                |
|  - flannel CNI passes parameters to delegate plugin                            |
|  - actual interface creation is done by bridge / host-local                    |
|                                                                                |
+================================================================================+
                                   |
                                   | kubelet invokes CNI
                                   v
+================================================================================+
|                          CNI Execution Phase                                   |
|                                                                                |
|  6. kubelet creates Pod sandbox                                                |
|                                                                                |
|  7. flannel CNI plugin                                                         |
|     - Reads /run/flannel/subnet.env                                            |
|     - Determines Pod CIDR and MTU                                              |
|     - Calculates Pod IP (10.244.X.Y)                                           |
|     - Delegates to bridge plugin                                               |
|                                                                                |
|  8. bridge / host-local plugin                                                 |
|     - Create veth pair                                                         |
|     - Pod namespace: eth0                                                      |
|     - Host side: vethXXXX -> cni0                                              |
|     - Assign Pod IP and default route                                          |
|                                                                                |
+================================================================================+
                                   |
                                   v
+================================================================================+
|                         Linux Kernel Data Plane                                |
|                                                                                |
|  Pod Network Namespace                                                         |
|     +----------------------+                                                   |
|     | eth0                 | 10.244.1.2                                        |
|     +----------------------+                                                   |
|                |                                                               |
|              veth                                                              |
|                |                                                               |
|     +----------------------+                                                   |
|     | cni0 (Linux bridge)  |                                                   |
|     +----------------------+                                                   |
|                |                                                               |
|     +----------------------------------------------------------------------+   |
|     | Flannel backend forwarding                                           |   |
|     |                                                                      |   |
|     | vxlan   -> flannel.1 -> UDP 8472 -> remote Node                      |   |
|     | host-gw -> physical NIC -> remote Node                               |   |
|     +----------------------------------------------------------------------+   |
|                                                                                |
|  Result:                                                                       |
|  - Flat Pod network across nodes                                               |
|  - No network policy enforcement                                               |
|  - No BGP, no routing protocol                                                 |
|                                                                                |
+================================================================================+
```

`Flannel DaemonSet Pod` 运行以后会生成 `/run/flannel/subnet.env` 文件，例如：

```bash
FLANNEL_NETWORK=10.1.0.0/16
FLANNEL_SUBNET=10.1.17.1/24
FLANNEL_MTU=1472
FLANNEL_IPMASQ=true
```

然后它利用这个文件信息去配置和调用 `bridge` 插件来生成容器网络，调用 `host-local` 来管理 `IP` 地址，例如：

```bash
{
	"name": "mynet",
	"type": "bridge",
	"mtu": 1472,
	"ipMasq": false,
	"isGateway": true,
	"ipam": {
		"type": "host-local",
		"subnet": "10.1.17.0/24"
	}
}
```

> 关联文档：[flannel kubernetes 集成](https://github.com/flannel-io/flannel/blob/master/Documentation/kubernetes.md)，[cni 插件](https://github.com/containernetworking/plugins)。

本项目配置文件 [kube-flannel.yaml.j2](https://github.com/brinnatt/kubeauto/blob/master/roles/flannel/templates/kube-flannel.yaml.j2)：

- 注意：本安装方式，flannel 通过 apiserver 接口读取 podCidr 信息，详见 https://github.com/flannel-io/flannel/issues/847

  因此想要修改节点 pod 网段掩码，请在 `clusters/xxxx/config.yml` 中修改 `NODE_CIDR_LEN` 配置项

- 配置相关 RBAC 权限和 `service account`

- 配置 `ConfigMap` 包含 CNI 配置和 flannel 配置(指定backend等)

**验证 flannel 网络：**

执行 flannel 安装成功后可以验证如下：

```bash
# kubectl get pods -A  |grep flannel
kube-system   kube-flannel-ds-49rzh           1/1     Running   0          38m
kube-system   kube-flannel-ds-djpbg           1/1     Running   0          38m
kube-system   kube-flannel-ds-ngpmp           1/1     Running   0          38m
kube-system   kube-flannel-ds-nxngw           1/1     Running   0          38m
kube-system   kube-flannel-ds-ppwst           1/1     Running   0          38m
kube-system   kube-flannel-ds-zwc59           1/1     Running   0          38m
```

每个节点上的 flannel pod 都必须处于 Running 状态才可以部署应用。

#### 1.3.3.7、集成插件

##### 1.3.3.7.1、DNS

DNS 是 k8s 集群首要部署的组件，它为集群中的其他 pods 提供域名解析服务；主要可以解析 `cluster sevice` 和 `Pod hostname`；目前建议部署 `coredns`。

NodeLocal DNSCache 在集群的上运行一个 dnsCache daemonset 来提高 clusterDNS 性能和可靠性。在 K8S 集群上的一些测试表明：相比于纯 coredns 方案，nodelocaldns + coredns 方案能够大幅降低 DNS 查询 timeout 的频次，提升服务稳定性。参考官方文档：https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/

**部署 dns**

配置文件参考 `https://github.com/kubernetes/kubernetes` 项目目录 `kubernetes/cluster/addons/dns`

目前 kubeauto 已经自动集成安装 coredns 和 nodelocaldns 组件，配置模板位于 `roles/cluster-addon/templates/` 目录。

```bash
# 默认已经集成安装，假设集群名为xxxx
kubecli setup xxxx all

# 如果需要分步安装
kubecli setup xxxx 07
```

**验证 dns 服务**

新建一个测试 nginx 服务

```bash
# kubectl run nginx --image=nginx --expose --port=80
service/nginx created
pod/nginx created
# kubectl get pod|grep nginx
nginx   1/1     Running   0          95s
# kubectl get svc|grep nginx
nginx        ClusterIP   10.68.155.123   <none>        80/TCP    116s
```

测试 pod alpine

```bash
# kubectl run test --rm -it --image=alpine /bin/sh
If you don't see a command prompt, try pressing enter.
/ # 
/ # cat /etc/resolv.conf
search default.svc.cluster.local svc.cluster.local cluster.local lan
nameserver 169.254.20.10
options ndots:5
```

测试集群内部服务解析

```bash
/ # nslookup nginx.default.svc.cluster.local
Server:         169.254.20.10
Address:        169.254.20.10:53


Name:   nginx.default.svc.cluster.local
Address: 10.68.155.123

/ # nslookup kubernetes.default.svc.cluster.local
Server:         169.254.20.10
Address:        169.254.20.10:53


Name:   kubernetes.default.svc.cluster.local
Address: 10.68.0.1
```

测试外部域名的解析，默认集成宿主机的 dns 解析

```bash
/ # nslookup www.baidu.com
Server:         169.254.20.10
Address:        169.254.20.10:53

Non-authoritative answer:
www.baidu.com   canonical name = www.a.shifen.com
Name:   www.a.shifen.com
Address: 183.2.172.17
Name:   www.a.shifen.com
Address: 183.2.172.177

Non-authoritative answer:
www.baidu.com   canonical name = www.a.shifen.com
Name:   www.a.shifen.com
Address: 240e:ff:e020:99b:0:ff:b099:cff1
Name:   www.a.shifen.com
Address: 240e:ff:e020:98c:0:ff:b061:c306
```

> 历史问题 1：如果你使用 `calico` 网络组件，安装完集群后，直接安装 dns 组件，可能会出现如下 BUG，分析是因为 calico 分配 pod 地址时候会从网段的第一个地址（网络地址）开始，详见提交的 [ISSUE #1710](https://github.com/projectcalico/calico/issues/1710)，临时解决办法为手动删除 POD，重新创建后获取后面的 IP 地址
>
> ```bash
> # BUG出现现象
> $ kubectl get pod --all-namespaces -o wide
> NAMESPACE     NAME                                       READY     STATUS             RESTARTS   AGE       IP              NODE
> default       busy-5cc98488d4-s894w                      1/1       Running            0          28m       172.20.24.193   192.168.97.24
> kube-system   calico-kube-controllers-6597d9c664-nq9hn   1/1       Running            0          1h        192.168.97.24   192.168.97.24
> kube-system   calico-node-f8gnf                          2/2       Running            0          1h        192.168.97.24   192.168.97.24
> kube-system   kube-dns-69bf9d5cc9-c68mw                  0/3       CrashLoopBackOff   27         31m       172.20.24.192   192.168.97.24
> 
> # 解决办法，删除pod，自动重建
> $ kubectl delete pod -n kube-system kube-dns-69bf9d5cc9-c68mw
> ```
>
> 历史问题 2：使用 ` kubectl run test -it --rm --image=busybox /bin/sh` 进行解析测试可能会失败，busybox 内的 nslookup 程序有 bug，详见 [kubernetes/dns#109](https://github.com/kubernetes/dns/issues/109)











