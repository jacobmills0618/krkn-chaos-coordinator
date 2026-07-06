"""Tests for OpenShift Virtualization keyword filter integration."""

from src.filter.chaos_filter import filter_bug, get_filter_keywords
from src.models import Bug


def _virt_bug(summary: str, description: str = "", component: str = "Virtualization") -> Bug:
    return Bug(
        key="OCPBUGS-9999",
        summary=summary,
        description=description,
        component=component,
        priority="Major",
        status="New",
        created="2026-01-01",
        url="https://issues.redhat.com/browse/OCPBUGS-9999",
    )


class TestOcpVirtKeywordLoading:
    def test_virtualization_agent_loads_ocp_virt_keywords(self):
        skip, chaos = get_filter_keywords("virtualization")
        assert "kubevirt" in chaos
        assert "virt-launcher" in chaos
        assert "vm migration" in " ".join(chaos).lower() or "live migration" in chaos
        assert "dpll pin" in skip

    def test_other_agents_do_not_load_ocp_virt_keywords(self):
        _, chaos = get_filter_keywords("control_plane")
        assert "virt-launcher" not in chaos


class TestOcpVirtFilterBug:
    def test_vm_migration_failure_is_chaos_relevant(self):
        result = filter_bug(
            _virt_bug(
                "VM migration failed during live migrate",
                "virt-launcher pod enters crashloop after migration timeout",
            ),
            agent_name="virtualization",
        )
        assert result.chaos_relevant
        assert result.injection_method is not None

    def test_virt_docs_bug_is_skipped(self):
        result = filter_bug(
            _virt_bug("documentation typo in virtctl help text"),
            agent_name="virtualization",
        )
        assert not result.chaos_relevant
        assert "documentation" in result.skip_reason.lower()

    def test_cve_virt_bug_is_skipped(self):
        result = filter_bug(
            _virt_bug("CVE-2026-12345 kubevirt auth bypass"),
            agent_name="virtualization",
        )
        assert not result.chaos_relevant

    def test_kubevirt_network_partition_is_relevant(self):
        result = filter_bug(
            _virt_bug(
                "kubevirt VM loses network after OVN partition",
                "virt-launcher pod cannot reach migration network during upgrade",
            ),
            agent_name="virtualization",
        )
        assert result.chaos_relevant
