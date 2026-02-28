# StarRocks 企业级技术白皮书

> **文档版本：** v2.1  
> **最后更新：** 2026-02-05  
> **基于：** StarRocks 官方文档 v3.5+  
> **适用范围：** 企业级生产环境部署与运维  
> **参考来源：** [StarRocks 官方文档](https://docs.starrocks.io/zh/docs/introduction/Architecture/)

---

## 目录

1. [架构设计理念](#第一章-架构设计理念)
2. [FE 元数据管理体系](#第二章-fe-元数据管理体系)
3. [BE 存储与执行引擎](#第三章-be-存储与执行引擎)
4. [CN 计算节点与存算分离](#第四章-cn-计算节点与存算分离)
5. [存储架构与介质管理](#第五章-存储架构与介质管理)
6. [查询执行引擎](#第六章-查询执行引擎)
7. [高可用与容错机制](#第七章-高可用与容错机制)
8. [性能优化策略](#第八章-性能优化策略)
9. [运维管理体系](#第九章-运维管理体系)
10. [企业级部署实践](#第十章-企业级部署实践)
11. [SLA 与服务等级协议](#第十一章-sla-与服务等级协议)
12. [成本优化策略](#第十二章-成本优化策略)
13. [性能调优深度指南](#第十三章-性能调优深度指南)
14. [附录](#附录)

---

## 第一章 架构设计理念

### 1.1 StarRocks 架构概述

StarRocks 采用**简洁高效的两层架构设计**，通过 FE（Frontend）和 BE/CN（Backend/Compute Node）的协同工作，实现高性能的实时分析能力。该架构设计遵循"简单即美"的工程哲学，在保证高性能的同时，最大化降低系统复杂度。

#### 1.1.1 核心架构组件

```
┌─────────────────────────────────────────────────────────┐
│                    Client Applications                   │
│        (BI Tools, Applications, APIs, JDBC/ODBC)        │
└────────────────────┬────────────────────────────────────┘
                     │ SQL / HTTP
         ┌───────────┴───────────┐
         │                       │
    ┌────▼────┐            ┌─────▼─────┐
    │   FE    │            │  BE / CN  │
    │Cluster  │◀──────────▶│  Cluster  │
    │         │  Metadata  │           │
    │Metadata │  & Query   │  Storage  │
    │Management│  Planning │  & Compute│
    └─────────┘            └───────────┘
         │                       │
         │                       │
    ┌────▼────┐            ┌─────▼─────┐
    │ BDB JE  │            │ Local Disk │
    │ (Raft)  │            │ or Object │
    │         │            │  Storage  │
    └─────────┘            └───────────┘
```

**架构设计原则：**

1. **职责分离**
   - FE 专注于元数据管理和查询规划
   - BE/CN 专注于数据存储和计算执行
   - 清晰的边界，降低耦合度

2. **水平扩展**
   - FE 集群支持动态添加节点
   - BE/CN 集群支持弹性扩容
   - 无单点瓶颈

3. **高可用设计**
   - FE 通过 Raft 协议保证元数据一致性
   - BE 通过多副本保证数据可靠性
   - CN 通过无状态设计实现快速恢复

4. **性能优化**
   - 列式存储减少 I/O
   - 向量化执行充分利用 CPU
   - 智能缓存提升查询效率

### 1.2 存算一体 vs 存算分离

#### 1.2.1 存算一体架构（BE）

**设计理念：** 数据本地化存储，最大化查询性能

```
┌─────────────────────────────────────────┐
│              BE Node                     │
│  ┌──────────────┐  ┌──────────────┐    │
│  │   Storage    │  │   Compute    │    │
│  │   Engine     │  │   Engine     │    │
│  │              │  │              │    │
│  │ Local Disk   │  │ SQL Execution│    │
│  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────────┘
```

**技术优势：**
- **数据本地性**：数据与计算在同一节点，避免网络传输
- **低延迟访问**：本地磁盘 I/O，延迟 < 1ms（SSD）
- **高吞吐量**：充分利用本地 I/O 带宽（10Gbps+）
- **简化架构**：单一组件，降低运维复杂度

**性能指标：**
- 查询延迟：P99 < 100ms（热数据）
- 写入吞吐：100MB/s+ per node
- I/O 利用率：> 80%

**适用场景：**
- 实时分析场景（OLAP）
- 高频查询业务（QPS > 1000）
- 对延迟敏感的应用（P99 < 200ms）
- 中小规模数据（< 100TB）

**成本分析：**
- 存储成本：高（本地 SSD/HDD）
- 计算成本：中等（与存储绑定）
- 总拥有成本（TCO）：中等

#### 1.2.2 存算分离架构（CN）

**设计理念：** 计算与存储解耦，实现弹性扩展和成本优化

```
┌─────────────────────────────────────────┐
│              CN Node                     │
│  ┌──────────────┐  ┌──────────────┐    │
│  │   Compute    │  │   Cache      │    │
│  │   Engine     │  │   Layer      │    │
│  │              │  │              │    │
│  │ SQL Execution│  │ Memory/Disk  │    │
│  └──────┬───────┘  └──────┬───────┘    │
│         │                  │            │
└─────────┼──────────────────┼────────────┘
          │                  │
          └──────────┬───────┘
                     │
          ┌──────────▼──────────┐
          │   Object Storage    │
          │  (S3/HDFS/MinIO)    │
          └─────────────────────┘
```

**技术优势：**
- **弹性扩展**：计算节点秒级扩容/缩容（< 10s）
- **成本优化**：对象存储成本低（$0.023/GB/month）
- **高可用性**：无状态设计，故障快速恢复
- **云原生**：适配 Kubernetes，支持自动扩缩容

**性能指标：**
- 查询延迟：P99 < 500ms（缓存命中），< 2s（缓存未命中）
- 缓存命中率：> 80%（热数据场景）
- 扩容时间：< 10 秒

**适用场景：**
- 大数据量存储（PB 级）
- 计算资源波动大（峰谷差异 > 3x）
- 成本敏感场景（存储成本占比高）
- 云原生部署（Kubernetes）

**成本分析：**
- 存储成本：低（对象存储 $0.023/GB）
- 计算成本：按需（弹性扩展）
- 总拥有成本（TCO）：低（大规模场景）

#### 1.2.3 架构选择决策树

```
Data Volume?
├── < 10TB → BE (Co-located)
├── 10-100TB → BE or CN (Hybrid)
└── > 100TB → CN (Separated)

Query Pattern?
├── Real-time, Low Latency → BE
├── Batch, High Throughput → CN
└── Mixed → Hybrid (BE + CN)

Cost Sensitivity?
├── Performance Priority → BE
└── Cost Priority → CN

Deployment Model?
├── On-premise → BE
└── Cloud-native → CN
```

### 1.3 架构设计原则

#### 1.3.1 高可用设计

- **FE 集群**：基于 Raft 协议的多数派共识机制
- **BE 集群**：多副本数据冗余机制
- **CN 集群**：无状态设计，快速故障恢复

#### 1.3.2 可扩展性设计

- **水平扩展**：支持动态添加/删除节点
- **自动均衡**：数据自动重新分布
- **弹性计算**：CN 节点秒级扩容

#### 1.3.3 性能优化设计

- **列式存储**：减少 I/O 开销
- **向量化执行**：充分利用 CPU 能力
- **智能缓存**：多层缓存体系

---

## 第二章 FE 元数据管理体系

### 2.1 FE 核心职责

Frontend（FE）节点是 StarRocks 集群的**元数据管理和查询协调中心**，作为整个集群的"大脑"，负责：

#### 2.1.1 元数据管理

**管理范围：**
- **数据库元数据**：数据库定义、字符集、权限信息
- **表元数据**：表结构、列定义、索引信息
- **分区元数据**：分区策略、分区键、分区范围
- **Tablet 元数据**：Tablet 分布、副本位置、状态信息
- **节点元数据**：BE/CN 节点信息、负载状态、健康度
- **用户权限**：用户账号、角色、权限矩阵
- **资源配额**：资源组、查询限制、并发控制

**元数据规模：**
- 单表元数据：~10-50KB
- 1000 表集群：~10-50MB
- 10,000 表集群：~100-500MB
- 元数据完全存储在内存，访问延迟 < 1ms

#### 2.1.2 查询规划与优化

**查询规划流程：**
```
SQL Query
    ↓
Parser (语法解析)
    ↓
Analyzer (语义分析)
    ↓
Optimizer (查询优化)
    ├── Rule-based Optimization
    ├── Cost-based Optimization
    └── Statistics-based Optimization
    ↓
Planner (执行计划生成)
    ├── Data Locality Optimization
    ├── Parallel Execution Planning
    └── Resource Allocation
    ↓
Physical Plan (分发到 BE/CN)
```

**优化技术：**
- **分区裁剪**：自动过滤不需要的分区
- **列裁剪**：只读取查询需要的列
- **谓词下推**：将过滤条件下推到存储层
- **Join 优化**：选择最优 Join 算法和顺序
- **聚合优化**：利用物化视图和预聚合

#### 2.1.3 集群协调

**协调功能：**
- **节点管理**：BE/CN 节点的注册、心跳、状态监控
- **负载均衡**：查询请求分发、数据分布均衡
- **故障恢复**：自动检测故障、调度副本恢复
- **资源调度**：查询资源分配、并发控制

### 2.2 元数据存储架构

#### 2.2.1 存储引擎：BDB JE

StarRocks FE 使用 **Berkeley DB Java Edition (BDB JE)** 作为元数据存储引擎，这是一个高性能的嵌入式事务数据库。

**技术特性：**

1. **内存存储架构**
   - 元数据完全存储在内存中（JVM Heap）
   - 访问延迟：< 1ms（内存访问）
   - 支持百万级元数据对象

2. **持久化机制**
   ```
   Memory (BDB JE)
       │
       │ Transaction Log (WAL)
       ▼
   Disk (meta_dir/image/)
       │
       │ Snapshot
       ▼
   Persistent Storage
   ```
   - **事务日志（WAL）**：所有变更先写日志，保证持久性
   - **快照机制**：定期生成元数据快照，加速恢复
   - **崩溃恢复**：通过日志重放恢复元数据

3. **高并发支持**
   - 支持多线程并发读写
   - 读写锁机制保证一致性
   - 无锁数据结构优化读性能

4. **事务支持**
   - ACID 特性保证
   - 支持多操作事务
   - 自动回滚机制

**元数据存储结构：**
- **Image 文件**：元数据快照，定期生成
- **Edit Log**：增量变更日志
- **VERSION 文件**：版本号管理

#### 2.2.2 Raft 协议实现

所有 FE 节点通过 **Raft 分布式一致性协议**同步元数据，保证强一致性。

**Raft 协议核心概念：**

1. **Leader 选举**
   ```
   Election Process:
   Follower → Candidate → Leader
       │         │          │
       │    Request Vote    │
       │◀───────────────────┘
       │    Majority Vote
       └───────────────────▶
   ```

2. **日志复制**
   ```
   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
   │  FE Leader  │────▶│ FE Follower  │────▶│ FE Follower  │
   │             │     │             │     │             │
   │  Append     │     │  Receive    │     │  Receive    │
   │  Log Entry  │────▶│  Log Entry  │────▶│  Log Entry  │
   │             │     │             │     │             │
   │  Commit     │◀────│  Acknowledge│◀────│  Acknowledge│
   │  (Majority) │     │             │     │             │
   └─────────────┘     └─────────────┘     └─────────────┘
   ```

**详细工作流程：**

**阶段1：日志追加**
```
1. Client 发送元数据变更请求到 Leader
2. Leader 将变更封装为 Log Entry
3. Leader 追加到本地日志（未提交）
4. Leader 并行发送 Log Entry 到所有 Follower
```

**阶段2：多数派确认**
```
5. Follower 接收 Log Entry，追加到本地日志
6. Follower 发送 Acknowledge 给 Leader
7. Leader 统计确认数
8. 当超过半数 Follower 确认后，Leader 提交 Log Entry
   （官方文档：只有在元数据更改同步到超过一半的 Follower 节点后，数据写入才被认为成功）
```

**阶段3：应用变更**
```
9. Leader 应用变更到本地状态机（BDB JE）
10. Leader 通知所有 Follower 应用变更
11. Follower 应用变更到本地状态机
12. 所有节点达到一致状态
```

**性能特性：**
- **延迟**：元数据同步延迟 < 10ms（同机房）
- **吞吐**：支持 1000+ 元数据变更/秒
- **一致性**：强一致性保证（线性一致性）

### 2.3 多数派共识机制

#### 2.3.1 SIMPLE_MAJORITY 策略

StarRocks FE 采用 **SIMPLE_MAJORITY（简单多数派）** 策略保证元数据一致性：

**核心规则：**
```
对于 N 个节点的集群，需要至少 ⌈(N+1)/2⌉ 个节点在线才能正常工作
```

**数学公式：**
```
Minimum Online Nodes = ⌈(Total Nodes + 1) / 2⌉
```

**容错能力表：**

| Total Nodes | Majority Requirement | Minimum Online | Fault Tolerance |
|-------------|----------------------|----------------|-----------------|
| 1 | 1 | 1 | 0 |
| 2 | 2 | 2 | 0 |
| 3 | 2 | 2 | 1 |
| 4 | 3 | 3 | 1 |
| 5 | 3 | 3 | 2 |
| 7 | 4 | 4 | 3 |

#### 2.3.2 共识机制的应用场景

**1. 元数据写入：**
- 只有 Leader 可以写入元数据
- 写入必须获得多数派节点确认
- 保证元数据变更的强一致性

**2. Leader 选举：**
- 当 Leader 故障时，Follower 自动发起选举
- 需要超过半数 Follower 在线才能选举成功
- 保证集群始终有唯一的 Leader

**3. 集群状态变更：**
- 节点加入/退出需要多数派确认
- 配置变更需要多数派确认
- 保证集群状态的一致性

### 2.4 FE 角色体系

#### 2.4.1 Leader 角色

**职责：**
- 唯一可写入元数据的节点
- 处理所有 DDL 和 DML 操作
- 协调查询计划的生成和分发
- 管理 BE/CN 节点的生命周期

**选举机制（官方描述）：**
- Leader 从 Follower 中自动选举产生（不是从所有 FE 中选举）
- 需要超过一半的 Follower 节点处于活动状态才能选举
- 选举成功后立即提供服务
- 元数据更改必须同步到超过一半的 Follower 节点后才被认为成功

#### 2.4.2 Follower 角色

**职责：**
- 参与 Leader 选举投票
- 同步 Leader 的元数据变更
- 提供只读查询服务
- 分担查询负载

**同步机制：**
- 实时接收 Leader 的日志条目
- 异步应用元数据变更
- 保证与 Leader 的最终一致性

#### 2.4.3 Observer 角色

**职责：**
- 同步 Leader 的元数据变更
- 提供只读查询服务
- **不参与 Leader 选举**

**设计目的：**
- 扩展读并发能力
- 不影响写性能（不参与选举）
- 适合高并发查询场景

**角色对比表：**

| Feature | Leader | Follower | Observer |
|---------|--------|----------|----------|
| Metadata Write | ✅ | ❌ | ❌ |
| Metadata Read | ✅ | ✅ | ✅ |
| Leader Election | ✅ (Elected) | ✅ (Vote) | ❌ |
| Query Planning | ✅ | ✅ | ✅ |
| Load Balancing | ✅ | ✅ | ✅ |

### 2.5 高可用部署模式

#### 2.5.1 最小高可用配置

**推荐配置：3 个 Follower 节点**

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ FE Node 1   │     │ FE Node 2   │     │ FE Node 3   │
│ (Leader)    │────▶│ (Follower)  │────▶│ (Follower)  │
│             │     │             │     │             │
│ Edit Log    │     │ Edit Log    │     │ Edit Log    │
│ Port: 9010  │     │ Port: 9010  │     │ Port: 9010  │
│ Query Port: │     │ Query Port: │     │ Query Port: │
│     9030    │     │     9030    │     │     9030    │
└─────────────┘     └─────────────┘     └─────────────┘
```

**特性：**
- 可容忍 1 个节点故障
- 满足多数派要求（2/3 在线）
- 自动故障转移和恢复

#### 2.5.2 大规模部署配置

**推荐配置：3 个 Follower + N 个 Observer**

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ FE Node 1   │     │ FE Node 2   │     │ FE Node 3   │
│ (Leader)    │     │ (Follower)  │     │ (Follower)  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────┬───────┴───────────┬───────┘
                   │                   │
         ┌─────────▼─────────┐ ┌───────▼────────┐
         │  Observer Node 1  │ │ Observer Node N│
         │  (Read Only)      │ │  (Read Only)   │
         └───────────────────┘ └────────────────┘
```

**特性：**
- 高可用：3 个 Follower 保证元数据一致性
- 高并发：Observer 节点扩展读能力
- 写性能：Observer 不参与选举，不影响写延迟

### 2.6 元数据一致性保证

#### 2.6.1 强一致性模型

StarRocks FE 采用**强一致性模型**保证元数据一致性：

**保证机制：**
1. **写入一致性**：元数据写入必须同步到多数派节点
2. **读取一致性**：所有节点读取到相同的元数据状态
3. **顺序一致性**：所有节点按相同顺序应用变更

#### 2.6.2 故障场景处理

**场景1：Leader 故障**
```
1. Follower 检测到 Leader 心跳超时
2. 剩余 Follower 发起选举（需要多数派在线）
3. 新 Leader 选举成功，继续提供服务
4. 原 Leader 恢复后降级为 Follower
```

**场景2：Follower 故障**
```
1. Leader 检测到 Follower 心跳超时
2. 如果剩余节点满足多数派，集群继续工作
3. 故障节点恢复后自动同步元数据
```

**场景3：网络分区**
```
1. 集群被分割为多个分区
2. 包含多数派的分区继续工作
3. 少数派分区停止服务，等待恢复
```

---

## 第三章 BE 存储与执行引擎

### 3.0 数据模型（官方支持的 4 种模型）

StarRocks 官方支持**四种数据模型**，适用于不同的业务场景：

#### 3.0.1 明细模型（Duplicate Key Model）

**特点：**
- 不进行任何聚合，保留所有原始数据
- 支持任意列作为排序键（Duplicate Key）
- 适合需要保留完整明细数据的场景

**适用场景：**
- 日志分析
- 用户行为追踪
- 需要完整历史记录的业务

**示例：**
```sql
CREATE TABLE detail_table (
    user_id BIGINT,
    event_time DATETIME,
    event_type VARCHAR(50),
    event_data VARCHAR(500)
) DUPLICATE KEY(user_id, event_time)
DISTRIBUTED BY HASH(user_id) BUCKETS 32;
```

#### 3.0.2 聚合模型（Aggregate Key Model）

**特点：**
- 相同 Key 的数据自动聚合
- 支持 SUM、MAX、MIN、REPLACE 等聚合函数
- 适合预聚合场景，减少存储和查询成本

**适用场景：**
- 数据报表
- 统计分析
- 指标汇总

**示例：**
```sql
CREATE TABLE aggregate_table (
    date DATE,
    user_id BIGINT,
    page_views BIGINT SUM,
    unique_visitors BIGINT REPLACE
) AGGREGATE KEY(date, user_id)
DISTRIBUTED BY HASH(user_id) BUCKETS 32;
```

#### 3.0.3 更新模型（Unique Key Model）

**特点：**
- 相同 Key 的数据自动更新（保留最新值）
- 适合需要更新历史数据的场景
- 查询性能优于明细模型

**适用场景：**
- 用户画像
- 商品信息
- 需要更新的维度表

**示例：**
```sql
CREATE TABLE unique_table (
    user_id BIGINT,
    user_name VARCHAR(50),
    age INT,
    city VARCHAR(50)
) UNIQUE KEY(user_id)
DISTRIBUTED BY HASH(user_id) BUCKETS 32;
```

#### 3.0.4 主键模型（Primary Key Model）

**特点：**
- 基于主键进行 Upsert 操作
- 支持部分列更新
- 查询性能最优（主键索引）

**适用场景：**
- 实时数据更新
- 订单系统
- 需要 Upsert 的业务场景

**示例：**
```sql
CREATE TABLE primary_table (
    order_id BIGINT,
    user_id BIGINT,
    order_amount DECIMAL(10,2),
    order_status VARCHAR(20)
) PRIMARY KEY(order_id)
DISTRIBUTED BY HASH(order_id) BUCKETS 32;
```

**模型选择决策树：**
```
需要保留完整明细？
  ├── 是 → 明细模型（Duplicate Key）
  └── 否 → 需要聚合？
      ├── 是 → 聚合模型（Aggregate Key）
      └── 否 → 需要更新？
          ├── 是 → 主键模型（Primary Key）或更新模型（Unique Key）
          └── 否 → 明细模型
```

---

### 3.1 BE 核心架构

Backend（BE）节点是 StarRocks **存算一体架构**的核心组件，在同一节点内集成数据存储和计算能力。

#### 3.1.1 架构设计

```
┌─────────────────────────────────────────────────┐
│              BE Node Architecture               │
│                                                 │
│  ┌──────────────┐      ┌──────────────┐        │
│  │ Query        │      │ Storage      │        │
│  │ Coordinator  │─────▶│ Engine       │        │
│  │              │      │              │        │
│  │ - Plan       │      │ - Tablet     │        │
│  │   Execution  │      │   Management │        │
│  │ - Vectorized │      │ - Replica    │        │
│  │   Execution  │      │   Sync       │        │
│  └──────────────┘      └──────┬───────┘        │
│                               │                │
│                      ┌────────▼────────┐       │
│                      │  Local Storage  │       │
│                      │  (HDD/SSD)     │       │
│                      └─────────────────┘       │
└─────────────────────────────────────────────────┘
```

**核心组件：**
- **查询协调器**：接收 FE 的执行计划，协调本地计算
- **存储引擎**：管理 Tablet 和副本，处理数据读写
- **本地存储**：持久化数据到本地磁盘

### 3.2 数据分布架构

#### 3.2.1 数据分层模型

StarRocks 采用**三层数据分布模型**，实现数据的水平分片和分布式存储：

```
Database (Logical Container)
  │
  ├── Table (Logical Table)
  │     │
  │     ├── Partition (Logical Partition)
  │     │     │  Partition Types:
  │     │     │  - Range Partition (by date, value range)
  │     │     │  - List Partition (by enum values)
  │     │     │  - Expression Partition (by expression)
  │     │     │
  │     │     └── Tablet (Physical Shard, 100MB-1GB)
  │     │           │  Tablet Properties:
  │     │           │  - Size: 100MB-1GB (configurable)
  │     │           │  - Immutable: Cannot be split
  │     │           │  - Independent: Self-contained data unit
  │     │           │
  │     │           └── Replica (Default: 3 copies)
  │     │                 │  Replica Distribution:
  │     │                 │  - Replica 1 → BE Node A (Primary)
  │     │                 │  - Replica 2 → BE Node B (Secondary)
  │     │                 │  - Replica 3 → BE Node C (Tertiary)
  │     │                 │
  │     │                 └── Data Files (Columnar Format)
  │     │                       - .dat files (column data)
  │     │                       - .idx files (index)
  │     │                       - .meta files (metadata)
```

**设计优势：**
- **水平分片**：Tablet 作为基本管理单元，支持 PB 级数据
- **多副本冗余**：每个 Tablet 维护多个副本，保证高可用
- **负载均衡**：副本分布在不同 BE 节点，避免热点
- **并行处理**：多个 Tablet 可并行查询，提升性能

#### 3.2.2 数据分布算法

**Tablet 分布策略：**

```
Tablet Distribution Algorithm:
  1. Calculate hash value from partition key + bucket key
  2. Map hash to BE node using consistent hashing
  3. Ensure replicas distributed across different nodes
  4. Balance data size and query load
```

**负载均衡机制：**
- **容量均衡**：考虑节点存储容量
- **负载均衡**：考虑节点查询负载
- **网络拓扑**：考虑网络距离（如支持）
- **自动重平衡**：定期检查并调整分布

#### 3.2.3 Tablet 机制详解

**Tablet 物理结构：**

```
Tablet Directory Structure:
/data/be/storage/
  └── <table_id>/
      └── <partition_id>/
          └── <tablet_id>/
              ├── <schema_hash>/
              │   ├── xxx.dat  (Column data files)
              │   ├── xxx.idx (Index files)
              │   └── xxx.meta (Metadata files)
              └── .tabletmeta (Tablet metadata)
```

**Tablet 特性：**
- **大小范围**：通常 100MB-1GB，可配置（`tablet_size` 参数）
- **不可分割**：Tablet 是数据分布的最小单元，不支持动态分裂
- **独立管理**：每个 Tablet 独立存储和计算，可并行处理
- **列式存储**：数据按列存储，提升查询性能

**Tablet 生命周期：**
```
Tablet Lifecycle:
  Created → Writing → Committed → Queryable → Compacting → Merged
```

**Tablet 数量规划：**
```
Tablet Count = (Table Size / Tablet Size) × Partition Count × Bucket Count

示例：
- 表大小：1TB
- Tablet 大小：500MB
- 分区数：12（按月分区）
- 分桶数：32
- Tablet 总数：1TB / 500MB × 12 × 32 = 768 tablets
```

**Tablet 分布策略：**
- FE 根据负载均衡算法选择 Tablet 位置
- 考虑节点容量、负载、网络拓扑
- 自动避免数据倾斜
- 支持手动指定 Tablet 位置（如需要）

### 3.3 多副本机制

#### 3.3.1 副本配置

**默认配置：**
- **副本数**：3 个（通过 `default_replication_num` 配置）
- **分布策略**：副本分布在不同的 BE 节点
- **同步机制**：异步同步，保证最终一致性

**副本数选择：**

| Replica Count | Reliability | Storage Cost | Fault Tolerance |
|---------------|-------------|--------------|-----------------|
| 1 | Low | 1x | 0 nodes |
| 2 | Medium | 2x | 0 nodes (no redundancy) |
| 3 | High | 3x | 1 node |
| 5 | Very High | 5x | 2 nodes |

#### 3.3.2 副本同步机制

```
┌─────────────┐
│  Write      │
│  Request    │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│  Primary Replica│  ──┐
│  (BE Node A)    │    │
└────────┬────────┘    │
         │             │ Async Replication
         │             │
    ┌────▼────┐   ┌────▼────┐
    │Replica 2│   │Replica 3│
    │(BE Node │   │(BE Node │
    │    B)   │   │    C)   │
    └─────────┘   └─────────┘
```

**同步流程：**
1. 写入请求发送到主副本（Primary Replica）
2. 主副本写入本地存储
3. 异步复制到其他副本（Replica 2、Replica 3）
4. 所有副本最终达到一致状态

#### 3.3.3 故障恢复机制

**自动恢复流程：**
```
1. FE 检测到 BE 节点故障（心跳超时）
   ↓
2. FE 识别受影响的数据副本
   ↓
3. FE 在其他 BE 节点上创建新副本
   ↓
4. 从健康副本复制数据到新副本
   ↓
5. 新副本同步完成后，集群恢复正常
```

**恢复时间估算：**
```
Recovery Time = (Data Size / Network Bandwidth) + Replication Overhead

示例：
- 受影响数据：100GB
- 网络带宽：1Gbps
- 恢复时间：约 15-20 分钟
```

### 3.4 存储空间规划

#### 3.4.1 存储空间计算公式

```
Total BE Storage = (Raw Data Size × Replication Factor) / Compression Ratio

其中：
- Raw Data Size: 原始数据大小
- Replication Factor: 副本数（默认 3）
- Compression Ratio: 压缩比（通常 3:1 到 5:1）

**压缩比参考值（官方支持的 4 种算法）：**
- **zlib**: 3:1 ~ 5:1
- **Zstandard (ZSTD)**: 3:1 ~ 5:1
- **LZ4**: 2:1 ~ 3:1
- **Snappy**: 2:1 ~ 3:1
```

**计算示例：**

| Raw Data | Replicas | Compression | Total Storage |
|----------|----------|-------------|---------------|
| 100TB | 3 | 4:1 | 75TB |
| 500TB | 3 | 4:1 | 375TB |
| 1PB | 3 | 4:1 | 750TB |

#### 3.4.2 存储预留策略

**推荐预留空间：**
- **元数据空间**：每个 Tablet 约 1-2MB
- **临时文件**：预留 10-20% 空间
- **压缩缓冲区**：预留 5-10% 空间
- **总预留**：建议预留 30-40% 额外空间

### 3.5 查询执行引擎

#### 3.5.1 向量化执行

StarRocks BE 采用**向量化执行引擎**，充分利用现代 CPU 的 SIMD 能力：

**执行模型：**
```
Traditional Row-based:
Row 1: [col1, col2, col3, ...]
Row 2: [col1, col2, col3, ...]
Row 3: [col1, col2, col3, ...]

Vectorized Column-based:
Column 1: [val1, val2, val3, ...]  ← SIMD processing
Column 2: [val1, val2, val3, ...]
Column 3: [val1, val2, val3, ...]
```

**性能优势：**
- **SIMD 加速**：单指令处理多行数据
- **缓存友好**：列式数据访问模式
- **并行处理**：充分利用多核 CPU

#### 3.5.2 执行流程

```
1. FE 生成物理执行计划
   ↓
2. FE 将计划分发到相关 BE 节点
   ↓
3. BE 节点并行执行
   ├── Scan: 扫描本地 Tablet
   ├── Filter: 过滤数据
   ├── Aggregate: 聚合计算
   └── Join: 表连接（如需要）
   ↓
4. BE 节点返回中间结果
   ↓
5. FE 汇总最终结果
```

### 3.6 BE 节点规划

#### 3.6.1 资源配置建议

**生产环境推荐配置：**

| Resource | Minimum | Recommended | Large Scale |
|----------|---------|-------------|-------------|
| CPU | 8 cores | 16 cores | 32+ cores |
| Memory | 32GB | 64GB | 128GB+ |
| Disk | 500GB | 2TB+ | 10TB+ |
| Network | 1Gbps | 10Gbps | 25Gbps+ |

#### 3.6.2 节点数量规划

**最小配置：**
- 3 个 BE 节点（保证高可用）

**推荐配置：**
- 5-10 个 BE 节点（平衡性能和成本）

**大规模配置：**
- 10+ 个 BE 节点（支持 PB 级数据）

**节点数量计算公式：**
```
BE Nodes = (Total Data Size × Replication Factor) / (Node Capacity × Utilization Rate)

其中：
- Utilization Rate: 建议 70-80%（预留空间）
```

---

## 第四章 CN 计算节点与存算分离

### 4.1 CN 架构设计

Compute Node（CN）是 StarRocks **存算分离架构**的核心组件，专注于计算能力，数据存储在远端对象存储。

#### 4.1.1 无状态设计

```
┌─────────────────────────────────────────────────┐
│              CN Node Architecture               │
│                                                 │
│  ┌──────────────┐      ┌──────────────┐        │
│  │ Query        │      │ Cache        │        │
│  │ Engine       │─────▶│ Manager      │        │
│  │              │      │              │        │
│  │ - Vectorized │      │ - Memory     │        │
│  │   Execution  │      │   Cache      │        │
│  │ - Parallel   │      │ - Disk       │        │
│  │   Processing │      │   Cache      │        │
│  └──────┬───────┘      └──────┬───────┘        │
│         │                     │                │
│         └──────────┬──────────┘                │
│                    │                           │
│            ┌───────▼────────┐                 │
│            │ Object Storage │                 │
│            │ (S3/HDFS/etc)  │                 │
│            └────────────────┘                 │
└─────────────────────────────────────────────────┘
```

**核心特性：**
- **无状态**：不存储持久化数据，可随时替换
- **弹性扩展**：秒级添加/删除节点
- **成本优化**：存储成本显著降低

### 4.2 存算分离架构优势

#### 4.2.1 架构对比

| Dimension | BE (Co-located) | CN (Separated) |
|-----------|----------------|----------------|
| **Storage** | Local Disk | Object Storage |
| **Compute** | Local | Local |
| **Scalability** | Data Rebalance Required | Instant Scaling |
| **Cost** | High (Storage) | Low (Object Storage) |
| **Latency** | Very Low | Medium (Cache Hit) |
| **Fault Recovery** | Data Replication | Instant Replacement |

#### 4.2.2 适用场景

**存算分离适合：**
- 大数据量存储（PB 级）
- 计算资源波动大
- 成本敏感场景
- 冷数据查询

**存算一体适合：**
- 实时分析场景
- 高频查询业务
- 延迟敏感应用

### 4.3 多层缓存体系

#### 4.3.1 缓存架构

```
Query Request
    │
    ▼
┌──────────────┐
│ Memory Cache │  ← L1: Fastest (μs)
│  (Hot Data)  │
└──────┬───────┘
       │ Miss
       ▼
┌──────────────┐
│  Disk Cache  │  ← L2: Fast (ms)
│ (Warm Data)  │
└──────┬───────┘
       │ Miss
       ▼
┌──────────────┐
│Object Storage│  ← L3: Medium (100ms+)
│ (Cold Data)  │
└──────────────┘
```

**缓存层次（官方架构）：**
1. **L1 - 内存缓存**：最热数据，延迟最低（微秒级）
2. **L2 - 本地磁盘缓存**：热数据，延迟低（毫秒级）
   - CN 节点本地磁盘用于缓存热数据（非一般存储）
   - 缓存命中时，查询性能可与存算一体架构相媲美
3. **L3 - 对象存储**：全量数据，延迟中等（100ms+）
   - 数据存储在对象存储（S3/HDFS/MinIO）或 HDFS
   - CN 节点无状态，不存储持久化数据

#### 4.3.2 缓存策略

**写入策略：**
```
Data Write
    │
    ├──→ Memory Cache (if enabled)
    ├──→ Disk Cache
    └──→ Object Storage
```

**读取策略：**
```
Data Read
    │
    ├──→ Check Memory Cache → Hit? Return
    ├──→ Check Disk Cache → Hit? Load to Memory → Return
    └──→ Load from Object Storage → Write to Cache → Return
```

**缓存管理：**
- **LRU 淘汰**：最近最少使用的数据被淘汰
- **预取机制**：预测性加载可能访问的数据
- **自动预热**：系统启动时自动加载热数据

### 4.4 弹性扩展机制

#### 4.4.1 秒级扩容

**扩容流程：**
```
1. 执行 ALTER SYSTEM ADD COMPUTENODE
   ↓
2. CN 节点启动并注册到 FE
   ↓
3. FE 开始分配查询任务到新节点
   ↓
4. 新节点按需加载缓存
   ↓
5. 扩容完成（秒级）
```

**特点：**
- **无需数据迁移**：数据在对象存储，无需复制
- **即时生效**：节点注册后立即参与计算
- **渐进式缓存**：随着查询逐步建立缓存

#### 4.4.2 秒级缩容

**缩容流程：**
```
1. 执行 ALTER SYSTEM DROP COMPUTENODE
   ↓
2. FE 停止分配新任务到该节点
   ↓
3. 等待现有任务完成
   ↓
4. 节点从集群移除
   ↓
5. 缩容完成（秒级）
```

**特点：**
- **无数据丢失风险**：数据在对象存储
- **平滑缩容**：等待任务完成，不影响服务
- **缓存可丢弃**：本地缓存可随时重建

### 4.5 CN 节点规划

#### 4.5.1 资源配置

**推荐配置：**

| Resource | Minimum | Recommended | High Performance |
|----------|---------|-------------|------------------|
| CPU | 8 cores | 16 cores | 32+ cores |
| Memory | 32GB | 64GB | 128GB+ |
| Cache Disk | 200GB | 500GB+ | 2TB+ |
| Network | 1Gbps | 10Gbps | 25Gbps+ |

#### 4.5.2 节点数量规划

**动态调整策略：**
- **查询高峰期**：增加 CN 节点，提升并发能力
- **查询低峰期**：减少 CN 节点，降低成本
- **自动扩缩容**：根据负载自动调整（如支持）

**计算公式：**
```
CN Nodes = (Peak QPS × Avg Query Time) / (Node Throughput × Target Utilization)

其中：
- Peak QPS: 峰值查询每秒
- Avg Query Time: 平均查询时间
- Node Throughput: 单节点吞吐量
- Target Utilization: 目标利用率（建议 70-80%）
```

---

## 第五章 存储架构与介质管理

### 5.1 存储架构设计

#### 5.1.1 单磁盘存储

**架构特点：**
```
┌─────────────────────┐
│     BE Node         │
│                     │
│  ┌───────────────┐  │
│  │  Single Disk  │  │
│  │               │  │
│  │  All Data     │  │
│  │  Stored Here  │  │
│  └───────────────┘  │
└─────────────────────┘
```

**适用场景：**
- 小规模部署
- 开发测试环境
- 数据量较小（< 1TB）

**局限性：**
- I/O 瓶颈：单磁盘成为性能瓶颈
- 容量限制：无法灵活扩展
- 故障风险：单点存储风险

#### 5.1.2 多磁盘存储

**架构特点：**
```
┌─────────────────────────────────────┐
│           BE Node                   │
│                                     │
│  ┌──────────┐  ┌──────────┐        │
│  │  Disk 1  │  │  Disk 2  │        │
│  │          │  │          │        │
│  │ Data A   │  │ Data B   │        │
│  └──────────┘  └──────────┘        │
│                                     │
│  ┌──────────┐  ┌──────────┐        │
│  │  Disk 3  │  │  Disk N  │        │
│  │          │  │          │        │
│  │ Data C   │  │ Data D   │        │
│  └──────────┘  └──────────┘        │
└─────────────────────────────────────┘
```

**配置格式：**
```
storage_root_path = /path1,medium:HDD;/path2,medium:SSD;/path3,medium:HDD
```

**核心优势：**
1. **I/O 并行度提升**：多磁盘并行读写，吞吐量线性增长
2. **容量灵活扩展**：动态添加磁盘，无需数据迁移
3. **自动负载均衡**：数据自动分散到各磁盘
4. **故障隔离**：单磁盘故障不影响其他磁盘

**性能提升：**
```
Single Disk Throughput: 200 MB/s
4 Disks Parallel: ~800 MB/s (接近线性扩展)
```

### 5.2 存储介质管理

#### 5.2.1 Medium 类型定义

**SSD (Solid State Drive)：**
- **特性**：高速、低延迟、高 IOPS
- **性能指标**：
  - 随机读取：50,000+ IOPS
  - 顺序读取：500+ MB/s
  - 延迟：< 1ms
- **成本**：较高（$/GB）

**HDD (Hard Disk Drive)：**
- **特性**：大容量、低成本、中等性能
- **性能指标**：
  - 随机读取：100-200 IOPS
  - 顺序读取：150-200 MB/s
  - 延迟：5-10ms
- **成本**：较低（$/GB）

#### 5.2.2 Medium 指定策略

**策略1：性能优先**
```
storage_root_path = /ssd1,medium:SSD;/ssd2,medium:SSD
```
- 所有数据存储在 SSD
- 最大化查询性能
- 适合实时分析场景

**策略2：成本优化**
```
storage_root_path = /hdd1,medium:HDD;/hdd2,medium:HDD
```
- 所有数据存储在 HDD
- 最小化存储成本
- 适合历史数据查询

**策略3：混合存储（推荐）**
```
storage_root_path = /ssd1,medium:SSD;/hdd1,medium:HDD;/hdd2,medium:HDD
```
- 热数据存储在 SSD
- 冷数据存储在 HDD
- 平衡性能和成本

#### 5.2.3 数据分层机制

**自动分层策略：**
```
Hot Data (Frequent Access)
    │
    ▼
┌──────────┐
│   SSD    │  ← Fast Access
└──────────┘
    │
    │ (After Cooling)
    ▼
┌──────────┐
│   HDD    │  ← Cost Effective
└──────────┘
```

**分层规则：**
- **访问频率**：高频访问数据 → SSD
- **数据年龄**：新数据 → SSD，旧数据 → HDD
- **业务重要性**：关键业务数据 → SSD

### 5.3 存储路径配置

#### 5.3.1 配置语法

**基本格式：**
```
storage_root_path = /path1;/path2;/path3
```

**带 Medium 指定：**
```
storage_root_path = /path1,medium:SSD;/path2,medium:HDD;/path3
```

**配置规则：**
- 多个路径用分号（`;`）分隔
- Medium 类型用逗号（`,`）分隔
- Medium 类型：`SSD` 或 `HDD`
- 未指定 Medium 时使用默认类型

#### 5.3.2 数据分布策略

**自动分布：**
- StarRocks 自动将数据分散到各存储路径
- 考虑路径容量和负载
- 避免数据倾斜

**分布算法：**
```
1. 计算各路径的可用容量
2. 计算各路径的当前负载
3. 选择容量充足且负载较低的路径
4. 写入数据到选定路径
```

### 5.4 存储性能优化

#### 5.4.1 I/O 优化

**多磁盘并行：**
- 数据分散到多个磁盘
- 并行 I/O 操作
- 提升整体吞吐量

**SSD 加速：**
- 热数据存储在 SSD
- 减少查询延迟
- 提升用户体验

#### 5.4.2 容量管理

**容量监控：**
- 实时监控各路径使用率
- 预警机制（> 80% 告警）
- 自动均衡数据分布

**扩展策略：**
- 动态添加新磁盘
- 自动重新平衡数据
- 无需停机操作

---

## 第六章 查询执行引擎

### 6.1 查询执行流程

#### 6.1.1 完整执行链路

```
┌─────────────┐
│   Client    │
│  SQL Query  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────┐
│         FE Cluster                  │
│                                     │
│  ┌──────────┐  ┌──────────┐        │
│  │  Parser  │→ │ Optimizer│        │
│  └────┬─────┘  └────┬─────┘        │
│       │             │              │
│       └──────┬──────┘              │
│              ▼                     │
│       ┌──────────┐                │
│       │ Planner  │                │
│       └────┬─────┘                │
└────────────┼──────────────────────┘
             │
             │ Physical Plan
             ▼
┌─────────────────────────────────────┐
│      BE/CN Cluster                  │
│                                     │
│  ┌──────────┐  ┌──────────┐        │
│  │ BE Node 1│  │ BE Node 2│        │
│  │ Execute  │  │ Execute  │        │
│  └────┬─────┘  └────┬─────┘        │
│       │             │              │
│       └──────┬──────┘              │
│              ▼                     │
│       ┌──────────┐                │
│       │  Result  │                │
│       └────┬─────┘                │
└────────────┼──────────────────────┘
             │
             ▼
┌─────────────┐
│   Client    │
│   Result    │
└─────────────┘
```

#### 6.1.2 执行阶段详解

**阶段1：SQL 解析（Parser）**
- 语法检查
- 生成抽象语法树（AST）
- 语义分析

**阶段2：查询优化（Optimizer）**
- 规则优化（Rule-based）
- 代价优化（Cost-based）
- 生成最优执行计划

**阶段3：计划分发（Planner）**
- 将执行计划分发到相关 BE/CN 节点
- 考虑数据本地性
- 负载均衡

**阶段4：并行执行（Execution）**
- BE/CN 节点并行执行
- 向量化处理
- 结果汇总

### 6.2 查询优化技术

#### 6.2.1 列式存储优化

**列式存储优势：**
```
Row-based Storage:
Row 1: [id:1, name:"Alice", age:25, city:"NYC"]
Row 2: [id:2, name:"Bob",   age:30, city:"LA"]
Row 3: [id:3, name:"Carol", age:28, city:"SF"]

Column-based Storage:
Column id:   [1, 2, 3]
Column name: ["Alice", "Bob", "Carol"]
Column age:  [25, 30, 28]
Column city: ["NYC", "LA", "SF"]
```

**查询优化：**
- **列裁剪**：只读取需要的列
- **压缩优化**：列式数据压缩率更高
- **向量化处理**：SIMD 指令加速

#### 6.2.2 向量化执行引擎

**执行模型：**
```
Traditional:
for each row:
    process(row)

Vectorized:
for each batch (1024 rows):
    process_batch(batch)  ← SIMD acceleration
```

**性能提升：**
- **SIMD 加速**：单指令处理多行
- **缓存友好**：连续内存访问
- **并行处理**：充分利用多核

#### 6.2.3 分区裁剪

**优化原理：**
```
Query: SELECT * FROM sales WHERE date = '2024-01-01'

Without Partition Pruning:
  Scan: All partitions (2023-01 to 2024-12)
  Filter: Apply date filter

With Partition Pruning:
  Scan: Only partition 2024-01
  Filter: Not needed (already filtered)
```

**性能提升：**
- 减少数据扫描量
- 降低 I/O 开销
- 提升查询速度

#### 6.2.4 物化视图

**预计算机制：**
```
Base Table: sales (100M rows)
  ↓
Materialized View: sales_mv (aggregated)
  ↓
Query: SELECT sum(amount) FROM sales WHERE date = '2024-01-01'
  ↓
Result: Directly from materialized view (much faster)
```

**适用场景：**
- 频繁的聚合查询
- 固定的查询模式
- 实时性要求不高的场景

### 6.3 数据写入流程

#### 6.3.1 写入路径

```
┌─────────────┐
│   Client     │
│ INSERT/LOAD  │
└──────┬───────┘
       │
       ▼
┌─────────────────┐
│  FE Leader      │
│  - Validate     │
│  - Plan         │
└──────┬──────────┘
       │
       │ Metadata Update
       ▼
┌─────────────────┐
│  FE Followers   │
│  - Replicate    │
│  - Commit       │
└─────────────────┘
       │
       │ Data Write
       ▼
┌─────────────────┐
│  BE Nodes       │
│  - Write Data   │
│  - Replicate    │
└─────────────────┘
```

#### 6.3.2 写入一致性

**强一致性保证：**
1. 元数据写入必须同步到多数派 FE
2. 数据写入到主副本（Primary Replica）
3. 异步复制到其他副本
4. 最终所有副本达到一致

**写入性能：**
- 受副本数影响
- 受网络带宽影响
- 可通过批量写入优化

---

## 第七章 高可用与容错机制

### 7.1 FE 高可用机制

#### 7.1.1 Raft 协议保障

**Leader 故障恢复：**
```
Normal State:
  Leader (Node 1) ──┐
  Follower (Node 2) │ Raft Protocol
  Follower (Node 3) ┘

Leader Failure:
  Follower (Node 2) ──┐
  Follower (Node 3) ──┘ Election

New Leader Elected:
  Leader (Node 2) ──┐
  Follower (Node 3) │ Continue Service
  (Node 1 offline)  ┘
```

**恢复时间：**
- 故障检测：3-5 秒（心跳超时）
- Leader 选举：1-2 秒
- 服务恢复：总计 5-10 秒

#### 7.1.2 多数派保护

**网络分区场景：**
```
Partition 1 (Majority): 2 nodes
  └── Continue Service ✅

Partition 2 (Minority): 1 node
  └── Stop Service ❌ (Wait for recovery)
```

**关键原则：**
- 只有包含多数派的分区可以继续服务
- 少数派分区自动停止，避免脑裂
- 网络恢复后自动合并

### 7.2 BE 高可用机制

#### 7.2.1 多副本冗余

**副本分布：**
```
Tablet T1:
  Replica 1 → BE Node A
  Replica 2 → BE Node B
  Replica 3 → BE Node C

BE Node A Failure:
  Replica 1: Lost ❌
  Replica 2: Available ✅
  Replica 3: Available ✅
  
  Service: Continue (2/3 replicas available)
```

**容错能力：**
- 3 副本：可容忍 1 个节点故障
- 5 副本：可容忍 2 个节点故障
- N 副本：可容忍 (N-1)/2 个节点故障

#### 7.2.2 自动故障恢复

**恢复流程：**
```
1. FE detects BE failure (heartbeat timeout)
   ↓
2. FE identifies affected replicas
   ↓
3. FE selects healthy BE nodes
   ↓
4. FE creates new replicas on healthy nodes
   ↓
5. Data replication from healthy replicas
   ↓
6. New replicas synchronized
   ↓
7. Cluster returns to normal state
```

**恢复时间估算：**
```
Recovery Time = (Affected Data Size) / (Network Bandwidth × Replication Efficiency)

Example:
- Affected Data: 100GB
- Network: 1Gbps
- Efficiency: 80%
- Recovery Time: ~15 minutes
```

### 7.3 CN 高可用机制

#### 7.3.1 无状态设计

**故障恢复：**
```
CN Node Failure:
  └── Instant Replacement
      ├── No data loss (data in object storage)
      ├── No data migration needed
      └── New node can start immediately
```

**恢复时间：**
- 故障检测：3-5 秒
- 节点替换：秒级（无需数据迁移）
- 缓存重建：渐进式（按需加载）

#### 7.3.2 负载均衡

**查询分发：**
- FE 自动将查询分发到可用 CN 节点
- 考虑节点负载和缓存命中率
- 故障节点自动从分发列表移除

### 7.4 数据可靠性保障

#### 7.4.1 多副本机制

**数据冗余：**
- 每个 Tablet 维护多个副本
- 副本分布在不同节点
- 单节点故障不影响数据可用性

#### 7.4.2 一致性保证

**写入一致性：**
- 元数据：强一致性（Raft 协议）
- 数据副本：最终一致性（异步复制）

**读取一致性：**
- 所有节点读取到相同的元数据
- 数据副本最终达到一致状态

### 7.5 故障场景处理

#### 7.5.1 FE 故障场景

**场景1：单个 Follower 故障**
- 影响：无（剩余节点满足多数派）
- 恢复：节点恢复后自动同步元数据

**场景2：Leader 故障**
- 影响：短暂服务中断（5-10 秒）
- 恢复：自动选举新 Leader

**场景3：多数派节点故障**
- 影响：集群不可用
- 恢复：需要手动修复或等待节点恢复

#### 7.5.2 BE 故障场景

**场景1：单个 BE 节点故障**
- 影响：部分数据查询可能变慢（等待副本恢复）
- 恢复：自动在其他节点补齐副本

**场景2：多个 BE 节点故障**
- 影响：取决于副本数（需保证至少 1 个副本可用）
- 恢复：逐步恢复，补齐所有副本

#### 7.5.3 CN 故障场景

**场景1：单个 CN 节点故障**
- 影响：查询自动路由到其他 CN 节点
- 恢复：可立即添加新节点替换

**场景2：所有 CN 节点故障**
- 影响：存算分离架构查询不可用
- 恢复：快速添加新 CN 节点（数据在对象存储）

---

## 第八章 性能优化策略

### 8.1 FE 性能优化

#### 8.1.1 Observer 节点扩展

**读能力扩展：**
```
Query Load Distribution:
  Leader:    20% queries
  Follower 1: 30% queries
  Follower 2: 30% queries
  Observer 1: 10% queries
  Observer 2: 10% queries
```

**优势：**
- 不参与选举，不影响写性能
- 分担读负载，提升并发能力
- 可动态添加/删除

#### 8.1.2 元数据优化

**优化策略：**
- 定期清理无用元数据
- 优化表结构设计
- 减少分区数量（如可能）

### 8.2 BE 性能优化

#### 8.2.1 存储优化

**多磁盘配置：**
- 提升 I/O 并行度
- 避免单磁盘瓶颈
- 自动负载均衡

**Medium 类型优化：**
- 热数据使用 SSD
- 冷数据使用 HDD
- 实现性能与成本平衡

#### 8.2.2 查询优化

**分区裁剪：**
- 合理设计分区策略
- 利用分区裁剪减少扫描量

**物化视图：**
- 预计算常用查询
- 提升查询性能

### 8.3 CN 性能优化

#### 8.3.1 缓存优化

**缓存配置：**
- 合理设置缓存大小
- 优化缓存淘汰策略
- 预加载热数据

#### 8.3.2 节点扩展

**弹性扩容：**
- 查询高峰期增加节点
- 查询低峰期减少节点
- 动态调整计算资源

### 8.4 查询性能优化

#### 8.4.1 SQL 优化建议

**1. 避免全表扫描：**
```sql
-- Bad
SELECT * FROM large_table;

-- Good
SELECT * FROM large_table WHERE partition_key = 'value';
```

**2. 利用索引：**
- 合理使用主键
- 利用分区键
- 避免不必要的排序

**3. 批量操作：**
```sql
-- Bad: Multiple single inserts
INSERT INTO table VALUES (...);
INSERT INTO table VALUES (...);

-- Good: Batch insert
INSERT INTO table VALUES (...), (...), (...);
```

#### 8.4.2 表设计优化

**分区策略：**
- 时间分区：按日期分区
- 列表分区：按枚举值分区
- 范围分区：按数值范围分区

**分桶策略：**
- 选择高基数列作为分桶键
- 避免数据倾斜
- 考虑查询模式

---

## 第九章 运维管理体系

### 9.1 集群监控体系

#### 9.1.1 FE 监控指标

**节点状态指标：**
- `fe_alive`: 节点存活状态（0/1）
- `fe_role`: 节点角色（Leader/Follower/Observer）
- `fe_is_master`: 是否为 Leader（0/1）
- `fe_cluster_id`: 集群 ID（一致性检查）

**元数据同步指标：**
- `replayed_journal_id`: 已重放的日志 ID
- `last_heartbeat`: 最后心跳时间
- `sync_lag`: 同步延迟（毫秒）
- `edit_log_port_status`: Edit Log 端口状态

**性能指标：**
- `query_qps`: 查询 QPS
- `query_latency_p50/p95/p99`: 查询延迟分位数
- `connection_count`: 当前连接数
- `max_connection_count`: 最大连接数

**资源使用指标：**
- `jvm_heap_used`: JVM 堆内存使用率
- `jvm_heap_max`: JVM 堆内存最大值
- `cpu_usage`: CPU 使用率
- `disk_usage`: 磁盘使用率

#### 9.1.2 BE 监控指标

**节点状态指标：**
- `be_alive`: 节点存活状态（0/1）
- `be_decommissioned`: 是否正在下线（0/1）
- `be_cluster_id`: 集群 ID
- `last_heartbeat`: 最后心跳时间

**存储指标：**
- `data_used_capacity`: 已使用存储容量（GB）
- `data_total_capacity`: 总存储容量（GB）
- `data_used_percent`: 存储使用率（%）
- `disk_available`: 可用磁盘空间（GB）

**副本指标：**
- `tablet_num`: Tablet 数量
- `replica_num`: 副本数量
- `replica_sync_lag`: 副本同步延迟
- `replica_unhealthy_num`: 不健康副本数

**性能指标：**
- `query_qps`: 查询 QPS
- `query_latency`: 查询延迟（毫秒）
- `scan_rows_per_second`: 扫描行数/秒
- `scan_bytes_per_second`: 扫描字节数/秒

**资源使用指标：**
- `cpu_usage`: CPU 使用率（%）
- `mem_usage`: 内存使用率（%）
- `disk_io_util`: 磁盘 I/O 利用率（%）
- `network_send_bytes`: 网络发送字节数
- `network_receive_bytes`: 网络接收字节数

#### 9.1.3 CN 监控指标

**节点状态指标：**
- `cn_alive`: 节点存活状态（0/1）
- `cn_work_group`: 工作组信息
- `last_heartbeat`: 最后心跳时间

**缓存指标：**
- `cache_hit_rate`: 缓存命中率（%）
- `cache_size`: 缓存大小（GB）
- `cache_used`: 已使用缓存（GB）
- `cache_eviction_rate`: 缓存淘汰率

**性能指标：**
- `query_qps`: 查询 QPS
- `query_latency`: 查询延迟（毫秒）
- `object_storage_access_latency`: 对象存储访问延迟
- `cache_load_time`: 缓存加载时间

#### 9.1.4 监控告警规则

**Critical 级别告警：**
- FE Leader 节点故障
- BE 节点故障数超过容错能力
- 存储使用率 > 90%
- 元数据同步延迟 > 10s

**Warning 级别告警：**
- FE Follower 节点故障
- BE 节点存储使用率 > 80%
- 查询延迟 P99 > 5s
- 副本同步延迟 > 60s

**Info 级别告警：**
- 节点加入/退出
- 配置变更
- 数据均衡开始/完成


### 9.2 备份与恢复策略

#### 9.2.1 元数据备份

**备份内容：**
- BDB JE 的 Image 文件（元数据快照）
- Edit Log 文件（增量变更日志）
- VERSION 文件（版本信息）

**备份策略：**
```
Backup Schedule:
  - Daily: Full backup (Image + Edit Log)
  - Hourly: Incremental backup (Edit Log only)
  - Retention: 7 days daily, 30 days weekly
```

**备份方法：**
```bash
# 1. 停止 FE 节点（可选，保证一致性）
systemctl stop starrocks-fe

# 2. 备份元数据目录
tar -czf fe_meta_backup_$(date +%Y%m%d).tar.gz /data/fe/meta

# 3. 上传到远程存储
rsync -av fe_meta_backup_*.tar.gz backup_server:/backup/starrocks/

# 4. 恢复 FE 节点
systemctl start starrocks-fe
```

**备份验证：**
- 定期验证备份文件完整性
- 测试恢复流程
- 记录备份时间戳

#### 9.2.2 数据恢复

**元数据恢复流程：**
```
1. 停止所有 FE 节点
   ↓
2. 清理损坏的元数据目录
   ↓
3. 从备份恢复元数据文件
   ↓
4. 启动 FE 节点
   ↓
5. 验证元数据完整性
   ↓
6. 检查集群状态
```

**数据恢复机制：**
- **多副本恢复**：从健康副本自动恢复数据
- **增量恢复**：只恢复缺失的数据块
- **并行恢复**：多节点并行恢复，加速过程

**恢复时间估算：**
```
Recovery Time = (Data Size / Network Bandwidth) / Parallelism + Overhead

示例：
- 数据量：1TB
- 网络：10Gbps
- 并行度：10
- 恢复时间：~15 分钟
```

#### 9.2.3 灾难恢复（DR）

**RTO/RPO 目标：**
- **RTO（恢复时间目标）**：< 1 小时
- **RPO（恢复点目标）**：< 15 分钟（取决于备份频率）

**灾难恢复方案：**
1. **同城容灾**：部署备用集群，实时同步
2. **异地容灾**：定期备份到异地存储
3. **云备份**：备份到云对象存储（S3/OSS）

### 9.3 扩容与缩容

#### 9.3.1 FE 扩容

**扩容步骤：**
1. 准备新节点（安装 StarRocks）
2. 配置元数据目录
3. 启动 FE 节点（指定 helper）
4. 执行 `ALTER SYSTEM ADD FOLLOWER`
5. 验证节点状态

#### 9.3.2 BE 扩容

**扩容步骤：**
1. 准备新节点（安装 StarRocks）
2. 配置存储路径
3. 启动 BE 节点
4. 执行 `ALTER SYSTEM ADD BACKEND`
5. 等待数据自动均衡

#### 9.3.3 缩容操作

**FE 缩容：**
1. 执行 `ALTER SYSTEM DROP FOLLOWER`
2. 等待集群确认
3. 停止节点服务
4. 清理本地文件

**BE 缩容：**
1. 执行 `ALTER SYSTEM DECOMMISSION BACKEND`
2. 等待副本补齐
3. 执行 `ALTER SYSTEM DROP BACKEND`
4. 停止节点服务

### 9.4 日志管理体系

#### 9.4.1 日志分类与级别

**FE 日志：**
- `fe.log`：主日志（INFO/WARN/ERROR）
- `fe.audit.log`：审计日志（用户操作记录）
- `fe.out`：标准输出（启动信息）

**BE 日志：**
- `be.INFO`：信息日志（正常运行信息）
- `be.WARNING`：警告日志（潜在问题）
- `be.ERROR`：错误日志（错误信息）
- `be.FATAL`：致命错误（严重故障）

**CN 日志：**
- `cn.INFO`：信息日志
- `cn.WARNING`：警告日志
- `cn.ERROR`：错误日志

#### 9.4.2 日志轮转策略

**配置参数：**
```
Log Rotation:
  - Max File Size: 100MB
  - Max Files: 20
  - Retention Days: 7
  - Compression: gzip (after rotation)
```

**日志清理：**
- 自动清理超过保留期的日志
- 定期归档重要日志
- 监控日志磁盘使用率

#### 9.4.3 日志分析

**关键日志模式：**
- **错误模式**：`ERROR|FATAL|Exception`
- **性能模式**：`slow query|timeout|latency`
- **故障模式**：`heartbeat timeout|node down|replica lost`

**日志聚合：**
- 使用 ELK/EFK 栈聚合日志
- 实时监控和告警
- 日志分析和可视化

---

## 第十章 企业级部署实践

### 10.1 部署架构设计

#### 10.1.1 小规模部署（< 10TB）

**配置（符合官方推荐）：**
- **FE 节点**：至少 3 个 Follower 节点
  - CPU: 8 核
  - 内存: 16GB
  - 存储: 100GB HDD（元数据存储）
- **BE 节点**：至少 3 个节点
  - CPU: 16 核
  - 内存: 64GB
  - 存储: 根据数据量计算（见 3.4.1 节存储空间计算公式）
- **存储配置**：单磁盘或双磁盘

**特点：**
- 成本适中
- 高可用保障（可容忍 1 个节点故障）
- 适合中小型企业

#### 10.1.2 中规模部署（10TB - 100TB）

**配置（符合官方推荐）：**
- **FE 节点**：3 个 Follower + 2-3 个 Observer
  - Follower: 8 核 CPU, 16GB 内存
  - Observer: 8 核 CPU, 16GB 内存（扩展查询并发）
- **BE 节点**：5-10 个节点
  - CPU: 16 核
  - 内存: 64GB
  - 存储: 多磁盘（SSD + HDD 混合）
- **存储配置**：多磁盘（SSD 用于热数据，HDD 用于温数据）

**特点：**
- 高性能
- 高并发支持（Observer 扩展读能力）
- 适合中大型企业

#### 10.1.3 大规模部署（> 100TB）

**配置：**
- FE: 3 个 Follower + 5+ 个 Observer
- BE: 10+ 个节点
- 存储: 多磁盘，SSD 加速
- 可选: CN 节点（存算分离）

**特点：**
- 超高性能
- 弹性扩展
- 适合大型企业

### 10.2 网络规划

#### 10.2.1 网络架构

```
┌─────────────┐
│   Client    │
│  Network    │
└──────┬──────┘
       │
       │ Public Network / DMZ
       │
┌──────▼──────────────────┐
│   Load Balancer         │
│   (Optional)            │
└──────┬──────────────────┘
       │
       │ Internal Network
       │
┌──────▼──────────────────┐
│   FE Cluster            │
│   (9030, 8030)          │
└──────┬──────────────────┘
       │
       │ Internal Network
       │
┌──────▼──────────────────┐
│   BE/CN Cluster         │
│   (9060, 8040, etc)     │
└─────────────────────────┘
```

#### 10.2.2 网络要求

**带宽要求：**
- FE 之间：1Gbps+（元数据同步）
- BE 之间：10Gbps+（数据复制）
- 客户端到 FE：1Gbps+（查询请求）

**延迟要求：**
- FE 之间：< 5ms
- BE 之间：< 10ms
- 客户端到 FE：< 50ms

### 10.3 安全体系

#### 10.3.1 访问控制

**用户认证：**
- **本地认证**：用户名密码认证
- **LDAP 集成**：企业目录服务集成
- **Kerberos 支持**：企业级认证协议

**权限管理：**
```
Permission Model:
  Database Level
    ├── SELECT
    ├── INSERT
    ├── UPDATE
    ├── DELETE
    ├── CREATE TABLE
    └── DROP TABLE
  
  Table Level
    ├── SELECT
    ├── INSERT
    ├── ALTER
    └── DROP
  
  Resource Level
    ├── Query Quota
    ├── Connection Limit
    └── Resource Group
```

**角色管理：**
- 创建角色并分配权限
- 用户继承角色权限
- 支持权限继承和覆盖

#### 10.3.2 网络安全

**网络隔离：**
```
Network Architecture:
  Internet
    │
    ▼
  DMZ / Load Balancer
    │
    ▼
  Firewall (Port 9030, 8030)
    │
    ▼
  Internal Network
    │
    ├── FE Cluster (9030, 8030, 9010, 9020)
    └── BE/CN Cluster (9060, 8040, 9050, etc)
```

**安全策略：**
- **防火墙规则**：只开放必要端口
- **内网部署**：FE/BE/CN 节点内网部署
- **VPN 访问**：客户端通过 VPN 访问
- **白名单机制**：限制客户端 IP 访问

#### 10.3.3 数据加密

**传输加密：**
- **TLS/SSL**：加密客户端到 FE 的连接
- **内部加密**：FE 到 BE/CN 的通信加密（如支持）

**存储加密：**
- **磁盘加密**：使用 LUKS/dm-crypt 加密本地磁盘
- **对象存储加密**：S3/OSS 的服务器端加密（SSE）
- **应用层加密**：敏感数据应用层加密

#### 10.3.4 审计日志

**审计内容：**
- 用户登录/登出
- DDL 操作（CREATE/DROP/ALTER）
- DML 操作（INSERT/UPDATE/DELETE）
- 权限变更
- 配置变更

**日志存储：**
- 本地文件：`fe.audit.log`
- 远程存储：发送到日志聚合系统
- 保留期限：根据合规要求（通常 1-3 年）

### 10.4 容量规划

#### 10.4.1 存储容量

**计算公式（与 3.4.1 节保持一致）：**
```
Total BE Storage = (Raw Data Size × Replication Factor) / Compression Ratio

其中：
- Raw Data Size: 原始数据大小
- Replication Factor: 副本数（默认 3）
- Compression Ratio: 压缩比（通常 3:1 到 5:1）

**额外空间需求：**
- 元数据开销：约 1-2MB per Tablet
- 临时文件缓冲：预留 20-30% 空间
- 增长缓冲：建议预留 30-50% 空间

**完整存储需求：**
Total Storage Required = 
    (Raw Data Size × Replication Factor) / Compression Ratio
    + Metadata Overhead
    + Temporary Buffer (20-30%)
    + Growth Buffer (30-50%)
```

#### 10.4.2 计算资源

**CPU 规划：**
```
Total CPU = (Peak QPS × Avg Query Time × CPU per Query) / Target Utilization

其中：
- Target Utilization: 70-80%
- CPU per Query: 根据查询复杂度估算
```

**内存规划：**
```
Total Memory = (Query Memory + Cache Memory + System Memory) × Nodes

其中：
- Query Memory: 并发查询内存需求
- Cache Memory: 缓存需求（如适用）
- System Memory: 系统预留（20%）
```

### 10.5 部署检查清单

#### 10.5.1 部署前检查

- [ ] 硬件资源满足要求
- [ ] 网络配置正确
- [ ] 存储路径规划完成
- [ ] 安全策略配置
- [ ] 监控系统就绪

#### 10.5.2 部署后验证

- [ ] 所有节点状态正常
- [ ] 元数据同步正常
- [ ] 数据副本分布正常
- [ ] 查询功能正常
- [ ] 监控告警正常

---

## 附录

### A. 关键参数参考

| Parameter | Default | Description |
|-----------|---------|-------------|
| `default_replication_num` | 3 | 数据副本数 |
| `storage_root_path` | - | 存储路径配置 |
| `edit_log_port` | 9010 | FE 编辑日志端口 |
| `query_port` | 9030 | FE 查询端口 |
| `be_port` | 9060 | BE 服务端口 |
| `heartbeat_service_port` | 9050 | BE 心跳端口 |

### B. 常用命令参考

**FE 管理：**
```sql
-- 查看 FE 节点
SHOW FRONTENDS;

-- 添加 Follower
ALTER SYSTEM ADD FOLLOWER "host:port";

-- 移除 Follower
ALTER SYSTEM DROP FOLLOWER "host:port";

-- 转移 Leader
TRANSFER LEADER TO "host:port";
```

**BE 管理：**
```sql
-- 查看 BE 节点
SHOW BACKENDS;

-- 添加 BE
ALTER SYSTEM ADD BACKEND "host:port";

-- 移除 BE
ALTER SYSTEM DECOMMISSION BACKEND "host:port";
ALTER SYSTEM DROP BACKEND "host:port";
```

**CN 管理：**
```sql
-- 查看 CN 节点
SHOW COMPUTE NODES;

-- 添加 CN
ALTER SYSTEM ADD COMPUTENODE "host:port";

-- 移除 CN
ALTER SYSTEM DROP COMPUTENODE "host:port";
```

### C. 性能基准参考

**查询性能：**
- 简单查询：< 100ms
- 复杂查询：< 1s
- 聚合查询：< 5s（取决于数据量）

**写入性能：**
- 单条插入：< 10ms
- 批量插入：1000+ rows/s
- 流式导入：100MB/s+

### D. 故障排查指南

#### D.1 FE 故障排查

**故障现象：FE 节点无法启动**

**排查步骤：**
```
1. 检查节点状态
   SHOW FRONTENDS;

2. 检查日志
   tail -f fe/log/fe.log
   grep ERROR fe/log/fe.log

3. 检查网络连接
   telnet <fe_host> 9010  # Edit Log Port
   telnet <fe_host> 9030  # Query Port

4. 检查磁盘空间
   df -h <meta_dir>

5. 检查元数据完整性
   ls -lh <meta_dir>/image/
   ls -lh <meta_dir>/*.jdb
```

**常见问题：**
- **元数据损坏**：从备份恢复或重新初始化
- **端口占用**：检查端口是否被占用
- **磁盘满**：清理日志或扩展磁盘
- **内存不足**：增加 JVM 堆内存

#### D.2 BE 故障排查

**故障现象：BE 节点离线或数据不可用**

**排查步骤：**
```
1. 检查节点状态
   SHOW BACKENDS;

2. 检查日志
   tail -f be/log/be.INFO
   tail -f be/log/be.ERROR

3. 检查存储空间
   df -h <storage_path>

4. 检查副本状态
   SHOW TABLETS FROM <table_name>;

5. 检查网络连接
   telnet <be_host> 9060  # BE Port
   telnet <be_host> 8040  # HTTP Port
```

**常见问题：**
- **存储空间不足**：扩展存储或清理数据
- **副本损坏**：从健康副本恢复
- **网络分区**：检查网络连接
- **进程崩溃**：检查系统日志（dmesg, /var/log/messages）

#### D.3 查询性能问题

**故障现象：查询慢或超时**

**排查步骤：**
```
1. 分析执行计划
   EXPLAIN <query>;

2. 检查分区裁剪
   EXPLAIN SELECT ... WHERE partition_key = ...;

3. 检查数据倾斜
   SHOW TABLETS FROM <table_name>;
   # 检查 Tablet 大小分布

4. 检查资源使用率
   SHOW PROC '/backends';
   # 查看 CPU、内存、磁盘 I/O

5. 检查慢查询日志
   # 启用慢查询日志
   SET enable_profile = true;
```

**优化建议：**
- **分区裁剪**：确保查询条件包含分区键
- **列裁剪**：只查询需要的列
- **索引优化**：合理使用主键和索引
- **物化视图**：预计算常用查询

#### D.4 集群状态异常

**故障现象：集群状态不一致**

**排查步骤：**
```
1. 检查 FE 集群状态
   SHOW FRONTENDS;
   # 检查 Leader、同步状态

2. 检查 BE 集群状态
   SHOW BACKENDS;
   # 检查节点健康度、副本状态

3. 检查元数据一致性
   # 对比各 FE 节点的元数据版本

4. 检查网络分区
   # 测试节点间网络连通性
```

**恢复方法：**
- **元数据不一致**：从 Leader 节点同步
- **副本不一致**：触发副本修复
- **网络分区**：修复网络后自动恢复

---

## 第十一章 SLA 与服务等级协议

### 11.1 可用性 SLA

#### 11.1.1 可用性定义

**可用性计算公式：**
```
Availability = (Total Time - Downtime) / Total Time × 100%

SLA Levels:
  - 99.9% (Three Nines): 8.76 hours downtime/year
  - 99.99% (Four Nines): 52.56 minutes downtime/year
  - 99.999% (Five Nines): 5.26 minutes downtime/year
```

#### 11.1.2 高可用配置

**99.9% 可用性（推荐配置）：**
- FE: 3 个 Follower 节点
- BE: 3 个节点，3 副本
- 可容忍 1 个节点故障

**99.99% 可用性（高可用配置）：**
- FE: 5 个 Follower 节点
- BE: 5 个节点，3 副本
- 可容忍 2 个节点故障
- 跨机房部署

**99.999% 可用性（极高可用配置）：**
- FE: 7 个 Follower 节点
- BE: 7+ 个节点，5 副本
- 可容忍 3 个节点故障
- 多机房部署
- 自动故障转移

### 11.2 性能 SLA

#### 11.2.1 查询性能 SLA

**性能目标：**
- **P50 延迟**：< 100ms（简单查询）
- **P95 延迟**：< 500ms（复杂查询）
- **P99 延迟**：< 2s（聚合查询）
- **查询成功率**：> 99.9%

#### 11.2.2 写入性能 SLA

**性能目标：**
- **批量写入吞吐**：> 100MB/s per node
- **写入延迟 P99**：< 5s
- **写入成功率**：> 99.9%

### 11.3 数据可靠性 SLA

**数据可靠性目标：**
- **数据持久性**：99.999999999%（11 个 9）
- **数据一致性**：强一致性保证
- **恢复时间**：< 1 小时（RTO）
- **恢复点**：< 15 分钟（RPO）

---

## 第十二章 成本优化策略

### 12.1 存储成本优化

#### 12.1.1 存储介质选择

**成本对比（以 AWS 为例）：**

| Storage Type | Cost per GB/Month | Performance | Use Case |
|--------------|------------------|-------------|----------|
| Local SSD | $0.10 | High | Hot Data |
| Local HDD | $0.05 | Medium | Warm Data |
| S3 Standard | $0.023 | Low | Cold Data |
| S3 Glacier | $0.004 | Very Low | Archive |

**优化策略：**
- 热数据：本地 SSD（性能优先）
- 温数据：本地 HDD（成本平衡）
- 冷数据：对象存储（成本优先）

#### 12.1.2 数据生命周期管理

**分层存储策略：**
```
Hot Data (0-30 days)
  └── Local SSD
      │
      │ Aging
      ▼
Warm Data (30-90 days)
  └── Local HDD
      │
      │ Aging
      ▼
Cold Data (90+ days)
  └── Object Storage (S3/OSS)
```

**成本节省：**
- 热数据占比 20%：使用 SSD
- 温数据占比 30%：使用 HDD
- 冷数据占比 50%：使用对象存储
- **总成本节省：40-60%**

### 12.2 计算成本优化

#### 12.2.1 CN 节点弹性扩展

**成本优化策略：**
- **按需扩容**：查询高峰期增加节点
- **自动缩容**：查询低峰期减少节点
- **Spot 实例**：使用云服务商的 Spot 实例（节省 70% 成本）

#### 12.2.2 资源利用率优化

**优化目标：**
- CPU 利用率：70-80%
- 内存利用率：< 80%
- 避免资源浪费

**优化方法：**
- 合理规划节点数量
- 动态调整资源分配
- 监控和优化资源使用

### 12.3 总拥有成本（TCO）分析

#### 12.3.1 TCO 组成

```
TCO = Hardware Cost + Software Cost + Operation Cost + Storage Cost

其中：
- Hardware Cost: 服务器硬件成本
- Software Cost: 软件许可成本（如适用）
- Operation Cost: 运维人力成本
- Storage Cost: 存储介质成本
```

#### 12.3.2 成本优化建议

**短期优化：**
- 使用 HDD 存储温冷数据
- 优化数据压缩比
- 合理设置副本数

**长期优化：**
- 迁移到存算分离架构（CN）
- 使用对象存储
- 自动化运维减少人力成本

---

## 第十三章 性能调优深度指南

### 13.1 查询性能调优

#### 13.1.1 执行计划分析

**查看执行计划：**
```sql
EXPLAIN <query>;
EXPLAIN VERBOSE <query>;  -- 详细信息
```

**关键指标：**
- **Scan Rows**：扫描行数（应尽量少）
- **Scan Bytes**：扫描字节数
- **Network Transfer**：网络传输量
- **Memory Usage**：内存使用量

#### 13.1.2 分区优化

**分区策略选择：**

| Partition Type | Use Case | Example |
|----------------|----------|---------|
| **Range Partition** | 时间序列数据 | `PARTITION BY RANGE(date)` |
| **List Partition** | 枚举值分区 | `PARTITION BY LIST(region)` |
| **Expression Partition** | 表达式分区 | `PARTITION BY (date_trunc('month', date))` |

**分区数量建议：**
- 单表分区数：< 1000（避免元数据膨胀）
- 分区大小：100MB-1GB（平衡查询和存储效率）

#### 13.1.3 分桶优化

**分桶键选择：**
- 选择高基数列（唯一值多）
- 避免数据倾斜
- 考虑查询模式（WHERE/JOIN 条件）

**分桶数量：**
- 小表：10-32 个桶
- 中表：32-128 个桶
- 大表：128-512 个桶

### 13.2 写入性能调优

#### 13.2.1 批量写入优化

**优化策略：**
```sql
-- Bad: 单条插入
INSERT INTO table VALUES (...);
INSERT INTO table VALUES (...);

-- Good: 批量插入
INSERT INTO table VALUES (...), (...), (...);

-- Best: Stream Load
curl --location-trusted -u user:passwd \
  -H "label:label1" \
  -H "column_separator:," \
  -T data.csv \
  http://fe_host:8030/api/db/table/_stream_load
```

**性能提升：**
- 单条插入：1000 rows/s
- 批量插入：10,000+ rows/s
- Stream Load：100MB/s+

#### 13.2.2 压缩优化

**压缩算法选择（官方支持的 4 种算法）：**

| Algorithm | Compression Ratio | Speed | CPU Usage | 官方推荐场景 |
|-----------|------------------|-------|-----------|------------|
| **LZ4** | 2-3:1 | Fast | Low | 写入频繁场景 |
| **zlib** | 3-5:1 | Medium | Medium | 存储优先场景 |
| **ZSTD** | 3-5:1 | Fast | Medium | 性能平衡场景 |
| **Snappy** | 2-3:1 | Very Fast | Very Low | 低延迟场景 |

**选择建议（基于官方文档）：**
- **写入频繁**：LZ4 或 Snappy（速度快，CPU 占用低）
- **存储优先**：zlib 或 ZSTD（压缩比高，3-5:1）
- **性能平衡**：ZSTD（压缩比和速度平衡）
- **低延迟**：Snappy（最快速度，最低 CPU 占用）

### 13.3 系统参数调优

#### 13.3.1 FE 参数调优

**关键参数：**
```properties
# JVM 堆内存（建议：16GB+）
JAVA_OPTS = "-Xmx16g -Xms16g"

# 查询超时时间
query_timeout = 300

# 最大连接数
max_connections = 4096
```

#### 13.3.2 BE 参数调优

**关键参数：**
```properties
# 存储路径
storage_root_path = /data1,medium:SSD;/data2,medium:HDD

# 副本数
default_replication_num = 3

# 压缩算法
compression_type = LZ4

# 查询超时
query_timeout = 300
```

---

**文档版本：** v2.1  
**最后更新：** 2026-02-05  
**基于：** StarRocks 官方文档 v3.5+  
**参考来源：** [StarRocks 官方文档](https://docs.starrocks.io/zh/docs/introduction/Architecture/)

