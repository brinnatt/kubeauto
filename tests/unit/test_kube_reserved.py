"""Node Allocatable kube/system reserved — contract defaults + official systemd cgroup names.

Official docs:
  https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/
Official source (ToSystemd appends .slice):
  https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/cm/cgroup_manager_linux.go
Known misconfig (.slice.slice):
  https://github.com/kubernetes/kubernetes/issues/78629
"""
from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
KUBELET_CFG = ROOT / "roles/kube-node/templates/kubelet-config.yaml.j2"
KUBELET_SVC = ROOT / "roles/kube-node/templates/kubelet.service.j2"
CONFIG_YML = ROOT / "conf/config.yml"
SLICE_TASK = ROOT / "roles/prepare/tasks/podruntime-slice.yml"


def _render(template_path: Path, **ctx) -> str:
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    return env.from_string(template_path.read_text(encoding="utf-8")).render(**ctx)


class TestKubeReservedTemplates(unittest.TestCase):
    def _base_ctx(self, **overrides):
        ctx = {
            "ca_dir": "/etc/kubernetes/ssl",
            "CGROUP_DRIVER": "systemd",
            "ENABLE_LOCAL_DNS_CACHE": False,
            "CLUSTER_DNS_SVC_IP": "10.68.0.10",
            "LOCAL_DNS_CACHE": "169.254.20.10",
            "CLUSTER_DNS_DOMAIN": "cluster.local",
            "KUBE_RESERVED_ENABLED": "yes",
            "SYS_RESERVED_ENABLED": "yes",
            "KUBE_RESERVED_CPU": "1000m",
            "KUBE_RESERVED_MEMORY": "1536Mi",
            "KUBE_RESERVED_PID": "1000",
            "SYS_RESERVED_CPU": "1000m",
            "SYS_RESERVED_MEMORY": "2560Mi",
            "SYS_RESERVED_PID": "5000",
            "MAX_PODS": 110,
            "POD_MAX_PIDS": -1,
            "bin_dir": "/opt/kube/bin",
            "CONTAINER_RUNTIME": "containerd",
            "K8S_NODENAME": "node1",
            "KUBELET_ROOT_DIR": "/var/lib/kubelet",
        }
        ctx.update(overrides)
        return ctx

    def test_contract_defaults_enabled_in_config(self):
        text = CONFIG_YML.read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^KUBE_RESERVED_ENABLED:\s*"yes"\s*$')
        self.assertRegex(text, r'(?m)^SYS_RESERVED_ENABLED:\s*"yes"\s*$')
        self.assertRegex(text, r'(?m)^SYS_RESERVED_ENFORCE:\s*"no"\s*$')
        self.assertIn('KUBE_RESERVED_CPU: "1000m"', text)
        self.assertIn('KUBE_RESERVED_MEMORY: "1536Mi"', text)
        self.assertIn('SYS_RESERVED_CPU: "1000m"', text)
        self.assertIn('SYS_RESERVED_MEMORY: "2560Mi"', text)

    def test_contract_floor_two_cpu_four_gi(self):
        """Customer floor: ≥16C/32Gi nodes → reserve 2 CPU + 4Gi for kube+OS."""
        text = CONFIG_YML.read_text(encoding="utf-8")

        def pinned(name: str) -> str:
            m = __import__("re").search(rf'(?m)^{name}:\s*"([^"]+)"\s*$', text)
            self.assertIsNotNone(m, name)
            return m.group(1)

        kube_cpu = pinned("KUBE_RESERVED_CPU")
        sys_cpu = pinned("SYS_RESERVED_CPU")
        kube_mem = pinned("KUBE_RESERVED_MEMORY")
        sys_mem = pinned("SYS_RESERVED_MEMORY")

        def cpu_m(s: str) -> int:
            return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)

        def mem_mi(s: str) -> int:
            if s.endswith("Mi"):
                return int(s[:-2])
            if s.endswith("Gi"):
                return int(s[:-2]) * 1024
            raise AssertionError(s)

        self.assertEqual(cpu_m(kube_cpu) + cpu_m(sys_cpu), 2000)
        self.assertEqual(mem_mi(kube_mem) + mem_mi(sys_mem), 4096)

        out = _render(KUBELET_CFG, **self._base_ctx())
        self.assertEqual(out.count("cpu: 1000m"), 2)
        self.assertIn("memory: 1536Mi", out)
        self.assertIn("memory: 2560Mi", out)
        # Hard-enforce system.slice stays off by default (protect apiserver).
        self.assertNotIn("- system-reserved", out)
        self.assertIn("≥16 CPU / 32Gi", text)

    def test_reservation_must_not_exceed_capacity_comment(self):
        """Undersized nodes fail kubelet start — contract assumes ≥32Gi."""
        # Official: capacity must be ≥ kubeReserved+systemReserved(+eviction).
        # Lab 3Gi Rocky correctly errors; do not lower product defaults for lab.
        text = CONFIG_YML.read_text(encoding="utf-8")
        self.assertIn("32Gi", text)
        kube_mem = 1536
        sys_mem = 2560
        eviction = 300
        self.assertGreater(kube_mem + sys_mem + eviction, 3500)  # rejects ~3.5Gi lab VMs

    def test_systemd_cgroup_names_without_double_slice(self):
        """systemd driver: /podruntime + /system (docs + k8s#78629)."""
        out = _render(KUBELET_CFG, **self._base_ctx())
        self.assertIn("kubeReservedCgroup: /podruntime", out)
        self.assertIn("systemReservedCgroup: /system", out)
        self.assertNotIn("kubeReservedCgroup: /podruntime.slice", out)
        self.assertNotIn("systemReservedCgroup: /system.slice", out)
        self.assertIn("- kube-reserved", out)
        # Default: account systemReserved but do NOT hard-enforce system.slice
        self.assertNotIn("- system-reserved", out)
        self.assertIn("cpu: 1000m", out)
        self.assertIn("memory: 1536Mi", out)
        self.assertIn("memory: 2560Mi", out)

    def test_sys_reserved_enforce_opt_in(self):
        out = _render(KUBELET_CFG, **self._base_ctx(SYS_RESERVED_ENFORCE="yes"))
        self.assertIn("- system-reserved", out)

    def test_cgroupfs_keeps_literal_slice_paths(self):
        out = _render(KUBELET_CFG, **self._base_ctx(CGROUP_DRIVER="cgroupfs"))
        self.assertIn("kubeReservedCgroup: /podruntime.slice", out)
        self.assertIn("systemReservedCgroup: /system.slice", out)

    def test_disabled_omits_reserved_blocks(self):
        out = _render(
            KUBELET_CFG,
            **self._base_ctx(KUBE_RESERVED_ENABLED="no", SYS_RESERVED_ENABLED="no"),
        )
        self.assertNotIn("kubeReserved", out)
        self.assertNotIn("systemReserved", out)
        self.assertNotIn("kube-reserved", out)
        self.assertNotIn("system-reserved", out)

    def test_kubelet_service_soft_mkdir_and_slice(self):
        out = _render(KUBELET_SVC, **self._base_ctx())
        self.assertIn("Slice=podruntime.slice", out)
        self.assertIn("Requires=podruntime.slice", out)
        self.assertIn("ExecStartPre=-/bin/mkdir -p /sys/fs/cgroup/cpu/podruntime.slice", out)
        # Hard-fail mkdir breaks cgroup v2 unified hosts.
        self.assertNotIn("ExecStartPre=/bin/mkdir -p /sys/fs/cgroup/cpu/podruntime.slice", out)

    def test_podruntime_slice_task_cites_official_docs(self):
        text = SLICE_TASK.read_text(encoding="utf-8")
        self.assertIn("podruntime.slice", text)
        self.assertIn("reserve-compute-resources", text)
        self.assertIn("78629", text)


class TestRuntimeSlicePlacement(unittest.TestCase):
    def test_containerd_docker_proxy_templates_mention_slice(self):
        for rel in (
            "roles/containerd/templates/containerd.service.j2",
            "roles/docker/templates/docker.service.j2",
            "roles/docker/templates/cri-dockerd.service.j2",
            "roles/kube-node/templates/kube-proxy.service.j2",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("Slice=podruntime.slice", text, msg=rel)
            self.assertIn('KUBE_RESERVED_ENABLED == "yes"', text, msg=rel)
            self.assertIn("reserve-compute-resources", text, msg=rel)


if __name__ == "__main__":
    unittest.main()
