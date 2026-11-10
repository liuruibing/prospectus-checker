import asyncio
import json

import pytest

from app.source_aliases import SourceAliasProvider


S = "a"*24
R = "b"*24


def catalogues():
    return {role: {"pages": [{"page": 1, "sources": [{"source_id": source_id}]}]}
            for role, source_id in (("product_summary", S), ("periodic_report", R))}


def test_role_aliases_round_trip_citations_without_rewriting_document_text():
    events = []

    class Provider:
        async def generate_text(self, system, prompt):
            assert "S属于产品资料概要" in system
            data = json.loads(prompt.split("\n", 1)[1])
            assert set(data["context_pool"]["sources"]) == {"S1", "R1"}
            assert data["context_pool"]["sources"]["S1"]["raw_text"] == R
            assert data["items"][0]["allowed_summary_source_ids"] == ["S1"]
            assert data["items"][0]["verified_primary_reference"]["source_ids"] == ["R1"]
            return '{"items":[{"summary_source_ids":["S1"],"references":[{"source_ids":["R1"]}],"value":"S1"}]}'

    provider = SourceAliasProvider(Provider(), catalogues(), events.append)
    payload = {"items": [{"allowed_summary_source_ids": [S], "verified_primary_reference": {"source_ids": [R]}}],
               "context_pool": {"sources": {S: {"raw_text": R}, R: {"raw_text": "同样的正文"}}}}
    response = asyncio.run(provider.generate_text("system", "data\n"+json.dumps(payload)))
    item = json.loads(response)["items"][0]
    assert item["summary_source_ids"] == [S]
    assert item["references"][0]["source_ids"] == [R]
    assert item["value"] == "S1"
    assert events[0]["path"] == "comparison/source_aliases.json"
    assert any(event["path"].endswith("/response.txt") and '"R1"' in event["content"] for event in events)


def test_invalid_json_is_preserved_for_existing_failure_handling():
    class Provider:
        async def generate_text(self, system, prompt):
            return '{"items":[{"source_ids":["S1"]}'

    response = asyncio.run(SourceAliasProvider(Provider(), catalogues()).generate_text("system", "data\n{}"))
    assert response == '{"items":[{"source_ids":["S1"]}'


def test_encoded_request_still_obeys_context_budget():
    class Provider:
        async def generate_text(self, system, prompt):
            raise AssertionError("Oversized requests must not reach the model")

    with pytest.raises(ValueError, match="MODEL_CONTEXT_EXCEEDS_BUDGET"):
        asyncio.run(SourceAliasProvider(Provider(), catalogues(), request_limit=100).generate_text("system", "data\n{}"))
