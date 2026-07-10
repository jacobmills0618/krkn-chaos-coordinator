"""Tests for pluggable label category registry."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from src.labels.registry import (
    LabelCategoryConfig,
    _load_label_category_config,
    discover_label_categories,
    format_label_categories_bullet_list,
    format_label_category_display_name,
    format_label_category_prompt_option,
    format_label_discovery_disclaimer,
    list_label_category_prompt_options,
    list_label_discovery_ask_options,
    parse_category_ids_from_prompt_options,
    resolve_label_substrings,
)


def _write_yaml(directory: Path, name: str, data: dict) -> Path:
    path = directory / f"{name}.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


class TestLoadLabelCategoryConfig:

    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "openshift_virtualization", {
            "name": "openshift_virtualization",
            "description": "Virt labels",
            "label_substrings": ["cnv", "kubevirt"],
        })
        config = _load_label_category_config(path)

        assert config.name == "openshift_virtualization"
        assert config.description == "Virt labels"
        assert config.label_substrings == ("cnv", "kubevirt")
        assert isinstance(config, LabelCategoryConfig)

    def test_raises_on_missing_name(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "bad", {
            "label_substrings": ["cnv"],
        })
        with pytest.raises(ValueError, match="missing required 'name'"):
            _load_label_category_config(path)

    def test_raises_on_empty_substrings(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "bad", {
            "name": "bad",
            "label_substrings": [],
        })
        with pytest.raises(ValueError, match="empty"):
            _load_label_category_config(path)


class TestDiscoverLabelCategories:

    def test_discovers_all_yaml_files(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "alpha", {
            "name": "alpha", "label_substrings": ["a"],
        })
        _write_yaml(tmp_path, "beta", {
            "name": "beta", "label_substrings": ["b"],
        })

        categories = discover_label_categories(config_dir=tmp_path)

        assert len(categories) == 2
        assert "alpha" in categories
        assert "beta" in categories

    def test_ignores_scan_prompt_yaml(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "real", {
            "name": "real", "label_substrings": ["x"],
        })
        (tmp_path / "scan_prompt.yaml").write_text("fixed_labels: {}\n")

        categories = discover_label_categories(config_dir=tmp_path)

        assert len(categories) == 1

    def test_discovers_real_config_categories(self) -> None:
        categories = discover_label_categories()
        assert "openshift_virtualization" in categories
        virt = categories["openshift_virtualization"]
        assert "kubevirt" in virt.label_substrings
        assert "cnv" in virt.label_substrings


class TestListLabelCategoryPromptOptions:

    def test_uses_fixed_label_from_scan_prompt(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "openshift_virtualization", {
            "name": "openshift_virtualization",
            "label_substrings": ["cnv"],
        })
        (tmp_path / "scan_prompt.yaml").write_text(
            "fixed_labels:\n"
            "  openshift_virtualization: Fixed Virt Label (openshift_virtualization)\n"
        )
        options = list_label_category_prompt_options(config_dir=tmp_path)
        assert options == ["Fixed Virt Label (openshift_virtualization)"]


class TestResolveLabelSubstrings:

    def test_merges_substrings_from_categories(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "alpha", {
            "name": "alpha", "label_substrings": ["cnv", "shared"],
        })
        _write_yaml(tmp_path, "beta", {
            "name": "beta", "label_substrings": ["kubevirt", "shared"],
        })
        merged = resolve_label_substrings(["alpha", "beta"], config_dir=tmp_path)
        assert merged == ("cnv", "kubevirt", "shared")

    def test_raises_on_unknown_category(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown label category"):
            resolve_label_substrings(["missing"], config_dir=tmp_path)


class TestParseCategoryIdsFromPromptOptions:

    def test_extracts_ids_from_option_labels(self) -> None:
        options = [
            "OpenShift Virtualization — CNV, KubeVirt, VM labels (openshift_virtualization)",
            "Other — example (other_category)",
        ]
        assert parse_category_ids_from_prompt_options(options) == [
            "openshift_virtualization",
            "other_category",
        ]


class TestFormatLabelCategoryPromptOption:

    def test_uses_prompt_label_when_set(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "virt", {
            "name": "virt",
            "label_substrings": ["cnv"],
            "prompt_label": "Custom Virt (virt)",
        })
        config = _load_label_category_config(path)
        assert format_label_category_prompt_option(config) == "Custom Virt (virt)"

    def test_derives_label_from_name_and_description(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "foo_bar", {
            "name": "foo_bar",
            "description": "Example labels",
            "label_substrings": ["foo"],
        })
        config = _load_label_category_config(path)
        assert format_label_category_prompt_option(config) == (
            "Foo Bar — Example labels (foo_bar)"
        )


class TestLabelDiscoveryDisclaimer:

    def test_formats_disclaimer_with_category_id(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "openshift_virtualization", {
            "name": "openshift_virtualization",
            "description": "Virt labels",
            "label_substrings": ["cnv"],
        })
        disclaimer = format_label_discovery_disclaimer(
            ["openshift_virtualization"], config_dir=tmp_path,
        )
        assert "label discovery using:" in disclaimer
        assert "openshift_virtualization" in disclaimer
        assert 'select "No" for label discovery' in disclaimer


class TestListLabelDiscoveryAskOptions:

    def test_returns_no_and_input_like_agents(self) -> None:
        options = list_label_discovery_ask_options()
        assert options == [
            "No — component search only (Recommended)",
            "Input Label Category(s)",
        ]

    def test_format_label_categories_bullet_list(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "openshift_virtualization", {
            "name": "openshift_virtualization",
            "description": "Virt labels",
            "label_substrings": ["cnv"],
        })
        (tmp_path / "scan_prompt.yaml").write_text(
            "fixed_labels:\n"
            "  openshift_virtualization: OpenShift Virt (openshift_virtualization)\n"
        )
        bullets = format_label_categories_bullet_list(config_dir=tmp_path)
        assert bullets == "- openshift_virtualization — OpenShift Virt"

