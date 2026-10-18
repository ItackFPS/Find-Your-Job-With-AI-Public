"""Tests for the discovery query expansion layer.

Covers:
  - Static baseline is always returned (no LLM, no profile)
  - Profile domains/contracts/skills add extra queries
  - LLM queries are ADDITIVE: union(baseline, llm) not replacement
  - LLM failure leaves baseline intact
  - Noisy query rejection/replacement (_NOISY_QUERY_REPLACEMENTS)
  - Max query cap is respected
  - Duplicate queries are removed
  - Unknown source type falls back to minimal default
  - expand_all() returns queries keyed by source type
  - _parse_query_list() handles various LLM response formats
  - _deduplicate_preserve_order() removes case-insensitive duplicates
  - _apply_quality_filter() rejects/replaces noisy terms
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from src.discovery.query_expansion import (
    DiscoveryExpansionResult,
    DiscoveryQueryExpander,
    DiscoveryQuerySet,
    _FT_PARTITION_VOCABULARY,
    _apply_quality_filter,
    _deduplicate_preserve_order,
    _parse_query_list,
)
from src.models.profile import JobSearchProfile


# ---------------------------------------------------------------------------
# Profile fixtures
# ---------------------------------------------------------------------------

def _make_profile(
    domains: list[str] | None = None,
    skills: list[str] | None = None,
    contract_types: list[str] | None = None,
) -> JobSearchProfile:
    return JobSearchProfile(
        target_domains=domains or [],
        target_skills=skills or [],
        contract_types=contract_types or [],
    )


_FULL_PROFILE = _make_profile(
    domains=["software_development", "qa_testing", "automation", "data"],
    skills=["python", "javascript"],
)

_EMPTY_PROFILE = _make_profile()

# The 7 baseline queries for france_travail — used in many assertions.
_FT_BASELINE = [
    "informatique",
    "développement logiciel",
    "data",
    "cybersécurité",
    "intelligence artificielle",
    "test logiciel",
    "alternance informatique",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_response(queries: list[str]) -> MagicMock:
    """Build a mock requests.Response returning a JSON array."""
    import json
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [
            {"message": {"content": json.dumps(queries)}}
        ]
    }
    return resp


# ---------------------------------------------------------------------------
# Tests: _parse_query_list
# ---------------------------------------------------------------------------

class TestParseQueryList:
    def test_plain_json_array(self) -> None:
        result = _parse_query_list('["informatique", "développement", "logiciel"]')
        assert result == ["informatique", "développement", "logiciel"]

    def test_json_array_with_markdown_fence(self) -> None:
        text = '```json\n["informatique", "data"]\n```'
        result = _parse_query_list(text)
        assert result == ["informatique", "data"]

    def test_json_array_in_prose(self) -> None:
        text = 'Here are the terms: ["informatique", "logiciel"] — use them.'
        result = _parse_query_list(text)
        assert result == ["informatique", "logiciel"]

    def test_malformed_json_returns_empty(self) -> None:
        result = _parse_query_list("{not json at all}")
        assert result == []

    def test_empty_string_returns_empty(self) -> None:
        result = _parse_query_list("")
        assert result == []

    def test_non_list_json_returns_empty(self) -> None:
        result = _parse_query_list('{"key": "value"}')
        assert result == []

    def test_list_with_non_strings_filters_them(self) -> None:
        result = _parse_query_list('["informatique", 42, null, "logiciel"]')
        assert result == ["informatique", "logiciel"]

    def test_empty_strings_in_array_filtered(self) -> None:
        result = _parse_query_list('["informatique", "", "logiciel"]')
        assert result == ["informatique", "logiciel"]


# ---------------------------------------------------------------------------
# Tests: _deduplicate_preserve_order
# ---------------------------------------------------------------------------

class TestDeduplicatePreserveOrder:
    def test_removes_exact_duplicates(self) -> None:
        result = _deduplicate_preserve_order(["a", "b", "a"])
        assert result == ["a", "b"]

    def test_removes_case_insensitive_duplicates(self) -> None:
        result = _deduplicate_preserve_order(["Informatique", "informatique", "data"])
        assert result == ["Informatique", "data"]

    def test_preserves_insertion_order(self) -> None:
        result = _deduplicate_preserve_order(["z", "a", "m", "a"])
        assert result == ["z", "a", "m"]

    def test_empty_input(self) -> None:
        assert _deduplicate_preserve_order([]) == []

    def test_no_duplicates_unchanged(self) -> None:
        queries = ["informatique", "développement", "logiciel"]
        assert _deduplicate_preserve_order(queries) == queries


# ---------------------------------------------------------------------------
# Tests: _apply_quality_filter
# ---------------------------------------------------------------------------

class TestApplyQualityFilter:
    def test_noisy_term_reseau_replaced(self) -> None:
        accepted, rejected = _apply_quality_filter(["réseau"])
        assert accepted == ["réseau informatique"]
        assert rejected == []

    def test_noisy_term_numerique_rejected(self) -> None:
        accepted, rejected = _apply_quality_filter(["numérique"])
        assert accepted == []
        assert "numérique" in rejected

    def test_noisy_term_systeme_rejected(self) -> None:
        accepted, rejected = _apply_quality_filter(["système"])
        assert accepted == []
        assert "système" in rejected

    def test_good_term_passes_through(self) -> None:
        accepted, rejected = _apply_quality_filter(["développement logiciel"])
        assert accepted == ["développement logiciel"]
        assert rejected == []

    def test_too_long_query_rejected(self) -> None:
        long_q = "x" * 51
        accepted, rejected = _apply_quality_filter([long_q])
        assert accepted == []
        assert long_q in rejected

    def test_empty_string_ignored(self) -> None:
        accepted, rejected = _apply_quality_filter(["", "  ", "data"])
        assert accepted == ["data"]
        assert rejected == []

    def test_mixed_queries(self) -> None:
        queries = ["informatique", "réseau", "numérique", "data"]
        accepted, rejected = _apply_quality_filter(queries)
        assert "informatique" in accepted
        assert "réseau informatique" in accepted
        assert "data" in accepted
        assert "numérique" in rejected
        assert "réseau" not in accepted


# ---------------------------------------------------------------------------
# Tests: DiscoveryQueryExpander — baseline (no LLM)
# ---------------------------------------------------------------------------

class TestDiscoveryQueryExpanderBaseline:
    def _make_expander(self, max_queries: int = 12) -> DiscoveryQueryExpander:
        return DiscoveryQueryExpander(llm_endpoint=None, max_queries=max_queries)

    def test_no_llm_returns_baseline_for_france_travail(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")
        for q in _FT_BASELINE:
            assert q in result, f"Expected baseline query '{q}' in result"

    def test_empty_profile_still_returns_baseline(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")
        for q in _FT_BASELINE:
            assert q in result

    def test_baseline_always_non_empty(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")
        assert len(result) > 0

    def test_unknown_source_returns_informatique_fallback(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_EMPTY_PROFILE, source="unknown_source", language="en")
        assert "informatique" in result

    def test_result_has_no_duplicates(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")
        lower_result = [q.lower() for q in result]
        assert len(lower_result) == len(set(lower_result))


# ---------------------------------------------------------------------------
# Tests: DiscoveryQueryExpander — profile-aware additions
# ---------------------------------------------------------------------------

class TestDiscoveryQueryExpanderProfile:
    def _make_expander(self) -> DiscoveryQueryExpander:
        return DiscoveryQueryExpander(llm_endpoint=None, max_queries=20)

    def test_internship_contract_adds_stage_informatique(self) -> None:
        profile = _make_profile(contract_types=["internship"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert "stage informatique" in result

    def test_web_development_domain_adds_web_query(self) -> None:
        profile = _make_profile(domains=["web_development"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert "développement web" in result

    def test_tech_skills_added_as_queries(self) -> None:
        profile = _make_profile(skills=["python", "golang"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert "python" in result
        assert "golang" in result

    def test_too_short_skill_not_added(self) -> None:
        profile = _make_profile(skills=["a"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert "a" not in result

    def test_too_long_skill_not_added(self) -> None:
        long_skill = "x" * 51
        profile = _make_profile(skills=[long_skill])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert long_skill not in result

    def test_noisy_skill_not_added_if_rejected(self) -> None:
        profile = _make_profile(skills=["numérique"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        assert "numérique" not in result

    def test_alternance_contract_already_in_baseline(self) -> None:
        profile = _make_profile(contract_types=["alternance"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        # "alternance informatique" is already in baseline — no duplicate added
        assert result.count("alternance informatique") == 1

    def test_networking_domain_uses_specific_term(self) -> None:
        profile = _make_profile(domains=["networking"])
        expander = self._make_expander()
        result = expander.expand(profile, source="france_travail", language="fr")
        # "réseau informatique" (specific) should appear, not bare "réseau"
        assert "réseau informatique" in result
        assert "réseau" not in result or any(
            q == "réseau informatique" for q in result
        )


# ---------------------------------------------------------------------------
# Tests: DiscoveryQueryExpander — LLM additive path
# ---------------------------------------------------------------------------

class TestDiscoveryQueryExpanderLLM:
    def _make_expander(self, max_queries: int = 20) -> DiscoveryQueryExpander:
        return DiscoveryQueryExpander(
            llm_endpoint="http://localhost:1234",
            llm_model="test-model",
            llm_timeout=5,
            max_queries=max_queries,
        )

    def test_llm_queries_appear_in_result(self) -> None:
        expander = self._make_expander()
        llm_unique = ["devops", "architecture logicielle"]
        resp = _llm_response(llm_unique)

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        for q in llm_unique:
            assert q in result, f"Expected LLM query '{q}' in result"

    def test_baseline_present_even_with_llm(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        for q in _FT_BASELINE:
            assert q in result

    def test_llm_duplicate_of_baseline_not_added_twice(self) -> None:
        expander = self._make_expander()
        # LLM returns a query already in the baseline
        resp = _llm_response(["informatique", "devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        assert result.count("informatique") == 1
        assert "devops" in result

    def test_llm_noisy_query_rejected(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["numérique", "devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        assert "numérique" not in result
        assert "devops" in result

    def test_llm_noisy_query_replaced(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["réseau", "devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        assert "réseau" not in result
        assert "réseau informatique" in result

    def test_max_queries_cap_is_respected(self) -> None:
        expander = self._make_expander(max_queries=3)
        resp = _llm_response(["devops", "cloud", "kubernetes", "docker"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        assert len(result) == 3

    def test_baseline_queries_take_priority_over_cap(self) -> None:
        expander = self._make_expander(max_queries=3)
        resp = _llm_response(["devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        # First 3 should all come from baseline
        assert len(result) == 3
        for q in result:
            assert q in _FT_BASELINE

    def test_llm_connection_error_leaves_baseline_intact(self) -> None:
        import requests as req_lib
        expander = self._make_expander()

        with patch("requests.post", side_effect=req_lib.exceptions.ConnectionError("refused")):
            result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")

        for q in _FT_BASELINE:
            assert q in result

    def test_llm_timeout_leaves_baseline_intact(self) -> None:
        import requests as req_lib
        expander = self._make_expander()

        with patch("requests.post", side_effect=req_lib.exceptions.Timeout("timed out")):
            result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")

        for q in _FT_BASELINE:
            assert q in result

    def test_malformed_llm_output_leaves_baseline_intact(self) -> None:
        expander = self._make_expander()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "choices": [{"message": {"content": "This is not JSON at all"}}]
        }

        with patch("requests.post", return_value=resp):
            result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")

        for q in _FT_BASELINE:
            assert q in result

    def test_llm_http_error_leaves_baseline_intact(self) -> None:
        import requests as req_lib
        expander = self._make_expander()
        resp = MagicMock()
        resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("500")

        with patch("requests.post", return_value=resp):
            result = expander.expand(_FULL_PROFILE, source="france_travail", language="fr")

        for q in _FT_BASELINE:
            assert q in result

    def test_llm_prompt_includes_domains(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["devops"])

        with patch("requests.post", return_value=resp) as mock_post:
            expander.expand(_FULL_PROFILE, source="france_travail", language="fr")

        payload = mock_post.call_args.kwargs["json"]
        # messages[0] is the /no_think system message; prompt is in messages[1]
        prompt_content = payload["messages"][1]["content"]
        assert "software_development" in prompt_content
        assert "qa_testing" in prompt_content

    def test_llm_prompt_specifies_language(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["softwareentwicklung"])

        with patch("requests.post", return_value=resp) as mock_post:
            expander.expand(_FULL_PROFILE, source="france_travail", language="de")

        payload = mock_post.call_args.kwargs["json"]
        # messages[0] is the /no_think system message; prompt is in messages[1]
        prompt_content = payload["messages"][1]["content"]
        assert "de" in prompt_content

    def test_result_has_no_duplicates_with_llm(self) -> None:
        expander = self._make_expander()
        resp = _llm_response(["informatique", "devops", "devops"])

        with patch("requests.post", return_value=resp):
            result = expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        lower_result = [q.lower() for q in result]
        assert len(lower_result) == len(set(lower_result))


# ---------------------------------------------------------------------------
# Tests: metadata stored in _last_expansion
# ---------------------------------------------------------------------------

class TestExpansionMetadata:
    def test_metadata_stored_after_expand(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")
        assert "france_travail" in expander._last_expansion

    def test_metadata_baseline_queries(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")
        meta = expander._last_expansion["france_travail"]
        assert isinstance(meta, DiscoveryExpansionResult)
        assert meta.baseline_queries == _FT_BASELINE

    def test_metadata_llm_not_available_when_no_endpoint(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")
        meta = expander._last_expansion["france_travail"]
        assert meta.llm_available is False
        assert meta.llm_queries == []

    def test_metadata_llm_available_when_llm_succeeds(self) -> None:
        expander = DiscoveryQueryExpander(
            llm_endpoint="http://localhost:1234", max_queries=20
        )
        resp = _llm_response(["devops"])

        with patch("requests.post", return_value=resp):
            expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        meta = expander._last_expansion["france_travail"]
        assert meta.llm_available is True
        assert "devops" in meta.llm_queries

    def test_metadata_rejected_queries_captured(self) -> None:
        expander = DiscoveryQueryExpander(
            llm_endpoint="http://localhost:1234", max_queries=20
        )
        resp = _llm_response(["numérique", "devops"])

        with patch("requests.post", return_value=resp):
            expander.expand(_EMPTY_PROFILE, source="france_travail", language="fr")

        meta = expander._last_expansion["france_travail"]
        assert "numérique" in meta.rejected_queries


# ---------------------------------------------------------------------------
# Tests: expand_all
# ---------------------------------------------------------------------------

class TestExpandAll:
    def test_expand_all_returns_france_travail_key(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        assert "france_travail" in result

    def test_expand_all_france_travail_is_non_empty(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        assert len(result["france_travail"]) > 0

    def test_expand_all_includes_baseline(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        for q in _FT_BASELINE:
            assert q in result["france_travail"]

    def test_expand_all_with_llm_includes_baseline_and_llm(self) -> None:
        expander = DiscoveryQueryExpander(
            llm_endpoint="http://localhost:1234",
            max_queries=20,
        )
        llm_unique = ["architecture microservices"]
        resp = _llm_response(llm_unique)

        with patch("requests.post", return_value=resp):
            result = expander.expand_all(_FULL_PROFILE)

        queries = result["france_travail"]
        for q in _FT_BASELINE:
            assert q in queries
        assert "architecture microservices" in queries

    def test_llm_called_once_per_source(self) -> None:
        # expand_all expands 4 sources (france_travail, apec, jobicy, remoteok),
        # each of which calls the LLM endpoint once.
        expander = DiscoveryQueryExpander(
            llm_endpoint="http://localhost:1234",
            llm_timeout=5,
        )
        resp = _llm_response(["informatique"])

        with patch("requests.post", return_value=resp) as mock_post:
            expander.expand_all(_FULL_PROFILE)

        assert mock_post.call_count == 4

    # --- New sources added by Phase 2 ---

    def test_expand_all_returns_apec_key(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        assert "apec" in result

    def test_expand_all_returns_jobicy_key(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        assert "jobicy" in result

    def test_expand_all_returns_remoteok_key(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_FULL_PROFILE)
        assert "remoteok" in result

    def test_expand_all_apec_baseline_non_empty(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        assert len(result["apec"]) > 0

    def test_expand_all_jobicy_baseline_non_empty(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        assert len(result["jobicy"]) > 0

    def test_expand_all_remoteok_baseline_non_empty(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        assert len(result["remoteok"]) > 0

    def test_expand_all_apec_includes_baseline_terms(self) -> None:
        _APEC_BASELINE = ["informatique", "développeur", "data", "devops"]
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        for q in _APEC_BASELINE:
            assert q in result["apec"]

    def test_expand_all_jobicy_includes_baseline_slugs(self) -> None:
        _JOBICY_BASELINE = ["engineering", "cybersecurity", "admin"]  # data-science removed (returns 0 jobs)
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        for slug in _JOBICY_BASELINE:
            assert slug in result["jobicy"]

    def test_expand_all_remoteok_includes_baseline_tags(self) -> None:
        _REMOTEOK_BASELINE = ["python", "javascript", "devops", "react"]
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        for tag in _REMOTEOK_BASELINE:
            assert tag in result["remoteok"]

    def test_expand_all_returns_all_four_keys(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None)
        result = expander.expand_all(_EMPTY_PROFILE)
        assert set(result.keys()) >= {"france_travail", "apec", "jobicy", "remoteok"}

    def test_expand_all_remoteok_adds_skill_tags_from_profile(self) -> None:
        profile = _make_profile(skills=["golang", "rust"])
        expander = DiscoveryQueryExpander(llm_endpoint=None, max_queries=20)
        result = expander.expand_all(profile)
        assert "golang" in result["remoteok"]
        assert "rust" in result["remoteok"]

    def test_expand_all_no_duplicates_in_any_source(self) -> None:
        expander = DiscoveryQueryExpander(llm_endpoint=None, max_queries=20)
        result = expander.expand_all(_FULL_PROFILE)
        for src, queries in result.items():
            lower = [q.lower() for q in queries]
            assert len(lower) == len(set(lower)), f"Duplicates in source '{src}'"


# ---------------------------------------------------------------------------
# Tests: generate_partition_vocabulary
# ---------------------------------------------------------------------------

class TestGeneratePartitionVocabulary:
    def _make_expander(self) -> DiscoveryQueryExpander:
        return DiscoveryQueryExpander(llm_endpoint=None)

    def test_returns_list_for_france_travail(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_FULL_PROFILE, "france_travail")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_returns_empty_for_unknown_source(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_FULL_PROFILE, "unknown_source")
        assert result == []

    def test_no_duplicates_in_result(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_FULL_PROFILE, "france_travail")
        assert len(result) == len(set(result))

    def test_all_terms_come_from_vocabulary(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_EMPTY_PROFILE, "france_travail")
        for term in result:
            assert term in _FT_PARTITION_VOCABULARY

    def test_all_vocabulary_terms_present_for_empty_profile(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_EMPTY_PROFILE, "france_travail")
        for term in _FT_PARTITION_VOCABULARY:
            assert term in result

    def test_profile_relevant_terms_appear_first(self) -> None:
        """Terms relevant to the user's domains should precede generic vocabulary."""
        profile = _make_profile(domains=["software_development"])
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(profile, "france_travail")
        # "développeur" is the first affinity term for software_development
        assert result[0] == "développeur"

    def test_data_domain_prioritises_data_term(self) -> None:
        profile = _make_profile(domains=["data"])
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(profile, "france_travail")
        assert result.index("data") < result.index("cloud")

    def test_cybersecurity_domain_prioritises_cybersecurite(self) -> None:
        profile = _make_profile(domains=["cybersecurity"])
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(profile, "france_travail")
        assert "cybersécurité" in result[:3]

    def test_empty_profile_returns_full_vocabulary_in_order(self) -> None:
        expander = self._make_expander()
        result = expander.generate_partition_vocabulary(_EMPTY_PROFILE, "france_travail")
        assert result == _FT_PARTITION_VOCABULARY


# ---------------------------------------------------------------------------
# Tests: DiscoveryQuerySet import (backward compatibility)
# ---------------------------------------------------------------------------

class TestDiscoveryQuerySet:
    def test_instantiation(self) -> None:
        qs = DiscoveryQuerySet(source="france_travail", language="fr")
        assert qs.source == "france_travail"
        assert qs.queries == []


# ---------------------------------------------------------------------------
# Tests: Phase LATER / I15 — adaptive (yield-history) reordering wiring
# ---------------------------------------------------------------------------

class TestAdaptiveReorderingWiring:
    def _make_expander(self) -> DiscoveryQueryExpander:
        return DiscoveryQueryExpander(llm_endpoint=None)

    def test_no_yield_history_reproduces_exact_pre_i15_order(self) -> None:
        expander = self._make_expander()
        with_history_param = expander.expand(_EMPTY_PROFILE, source="france_travail", yield_history=None)
        assert with_history_param == _FT_BASELINE

    def test_empty_yield_history_dict_reproduces_exact_pre_i15_order(self) -> None:
        expander = self._make_expander()
        result = expander.expand(_EMPTY_PROFILE, source="france_travail", yield_history={})
        assert result == _FT_BASELINE

    def test_high_yield_baseline_query_is_promoted_first(self) -> None:
        from src.discovery.adaptive import QueryYieldStats

        expander = self._make_expander()
        # "data" is last in the FT baseline; give it a huge measured yield.
        history = {"data": QueryYieldStats("data", 500, 5)}
        result = expander.expand(_EMPTY_PROFILE, source="france_travail", yield_history=history)
        assert result[0] == "data"
        assert set(result) == set(_FT_BASELINE)  # bounded: same query set

    def test_expand_all_threads_per_source_history_independently(self) -> None:
        from src.discovery.adaptive import QueryYieldStats

        expander = self._make_expander()
        history_by_source = {
            "france_travail": {"data": QueryYieldStats("data", 500, 5)},
        }
        result = expander.expand_all(_EMPTY_PROFILE, yield_history_by_source=history_by_source)
        assert result["france_travail"][0] == "data"
        # apec/jobicy/remoteok had no history entry -> unaffected, original order.
        assert result["apec"] == list(expander._static_fallback("apec"))

    def test_adaptive_promoted_is_recorded_in_expansion_metadata(self) -> None:
        from src.discovery.adaptive import QueryYieldStats

        expander = self._make_expander()
        history = {"data": QueryYieldStats("data", 500, 5)}
        expander.expand(_EMPTY_PROFILE, source="france_travail", yield_history=history)
        meta = expander._last_expansion["france_travail"]
        assert "data" in meta.adaptive_promoted

    def test_history_never_changes_which_queries_are_included(self) -> None:
        from src.discovery.adaptive import QueryYieldStats

        expander = self._make_expander()
        history = {"data": QueryYieldStats("data", 500, 5), "informatique": QueryYieldStats("informatique", 0, 5)}
        result = expander.expand(_FULL_PROFILE, source="france_travail", yield_history=history)
        no_history_result = expander.expand(_FULL_PROFILE, source="france_travail")
        assert set(result) == set(no_history_result)
        assert len(result) == len(no_history_result)
