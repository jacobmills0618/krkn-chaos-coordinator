"""Tests for MAP→ANALYZE context forwarding helpers."""

from src.agents.base_agent import _merge_scenario_hits


class TestMergeScenarioHits:
    def test_prefers_map_hits_and_dedupes(self):
        map_hits = (
            {"path": "scenarios/a.yml", "text": "from map", "distance": 0.3},
            {"path": "scenarios/b.yml", "text": "map b", "distance": 0.4},
        )
        analyze_hits = [
            {"path": "scenarios/a.yml", "text": "analyze duplicate", "distance": 0.2},
            {"path": "scenarios/c.yml", "text": "analyze only", "distance": 0.5},
        ]
        merged = _merge_scenario_hits(map_hits, analyze_hits, limit=8)
        assert [h["path"] for h in merged] == [
            "scenarios/a.yml",
            "scenarios/b.yml",
            "scenarios/c.yml",
        ]
        assert merged[0]["text"] == "from map"

    def test_respects_limit(self):
        map_hits = tuple(
            {"path": f"scenarios/{i}.yml", "text": str(i)} for i in range(10)
        )
        merged = _merge_scenario_hits(map_hits, [], limit=3)
        assert len(merged) == 3
