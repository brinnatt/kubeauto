# OpenEBS LVM 运维提示

当前 StorageClass 启用 `thinProvision: "yes"`，驱动会创建或复用
`<VG>_thinpool`。thin pool **不是固定默认 10Gi**：OpenEBS LVM LocalPV
v1.7.0 在第一次创建 thin volume 且 pool 不存在时，根据请求容量与 VG
剩余空间计算初始大小。

生产必须持续检查：

```bash
lvs -a vg_k8s -o lv_name,lv_size,pool_lv,segtype,data_percent,metadata_percent,seg_monitor
vgs vg_k8s -o vg_name,vg_size,vg_free
```

`Data%` 或 `Meta%` 到 100% 会造成写入失败和数据损坏风险。扩容量必须依据
现场 VG 空闲容量与增长模型评审，禁止照抄固定数值：

```bash
lvextend -L +<approved-size> vg_k8s/vg_k8s_thinpool
```

完整原理、告警、扩容和恢复流程见：

- `docs/whitepaper/16-storage-openebs.md`
- `docs/operations-manual.md` §1.3.4
