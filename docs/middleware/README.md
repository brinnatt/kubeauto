# Kubeauto 中间件文档

## 企业交付规范

[Kubeauto 中间件企业交付规范](delivery-playbook.md)统一规定全部中间件的能力模型、交付流程、供应链、测试、文档和验收要求。

## 组件文档

| 组件 | 状态 | 正式文档 | 当前专项矩阵 |
| --- | --- | --- | --- |
| Percona PXC | 已交付 | [用户与运维手册](perconaPXC/operations-manual.md) · [技术白皮书](perconaPXC/technical-whitepaper.md) · [开发手册](perconaPXC/development-manual.md) | [MySQL/PXC 矩阵](../../tests/mysql-test-matrix.yaml) |
| Apache Kafka on Kubernetes | 已交付 | [用户与运维手册](kafka/operations-manual.md) · [技术白皮书](kafka/technical-whitepaper.md) · [开发手册](kafka/development-manual.md) | [Kafka 矩阵](../../tests/kafka-test-matrix.yaml) |
| Prometheus 监控平台 | 已交付 | [用户与运维手册](prometheus/operations-manual.md) · [技术白皮书](prometheus/technical-whitepaper.md) · [开发手册](prometheus/development-manual.md) | [企业矩阵 Prometheus 分路](../../tests/enterprise-test-matrix.yaml) |
| EFK / Loki 日志平台 | 已交付 | [用户与运维手册](efk/operations-manual.md) · [技术白皮书](efk/technical-whitepaper.md) · [开发手册](efk/development-manual.md) | [日志矩阵](../../tests/logging-test-matrix.yaml) |

组件状态以当前专项矩阵为准。只有完整现场证据达到 100% PASS，并同时通过供应链、幂等、清理和文档门禁，组件状态才能标记为“已交付”。
