"""Use role-labelled source IDs on the model wire; retain native IDs in results."""
import json
import re
from app.model_json import read_model_json


def _map_sources(value, mapping, key=""):
    if isinstance(value, dict):
        return {mapping.get(name, name) if key == "sources" else name: _map_sources(child, mapping, name)
                for name, child in value.items()}
    if isinstance(value, list):
        return [_map_sources(child, mapping, key) for child in value]
    if isinstance(value, str) and (key == "source_id" or key.endswith("source_ids")):
        return mapping.get(value, value)
    return value


class SourceText(str):
    def __new__(cls, text, metrics):
        result = super().__new__(cls, text)
        result.metrics = metrics
        return result


class SourceAliasProvider:
    def __init__(self, provider, catalogues, trace=None, *, request_limit=64_000):
        self.provider = provider
        self.model_name = getattr(provider, "model_name", None)
        self.trace = trace
        self.request_limit = request_limit
        self.aliases = {}
        self.calls = 0
        counts = {}
        records = []
        for role, catalogue in catalogues.items():
            prefix = {"product_summary": "S", "periodic_report": "R", "annual_report": "R", "prospectus": "P"}.get(role, "U")
            for page in catalogue["pages"]:
                for source in page["sources"]:
                    source_id = source["source_id"]
                    if source_id in self.aliases:
                        continue
                    counts[prefix] = counts.get(prefix, 0)+1
                    alias = prefix+str(counts[prefix])
                    self.aliases[source_id] = alias
                    records.append({"display_id": alias, "source_id": source_id, "role": role, "page": page.get("page", source.get("page"))})
        self.native_ids = {alias: native for native, alias in self.aliases.items()}
        if trace:
            trace({"kind": "artifact", "path": "comparison/source_aliases.json", "content": records})

    async def generate_text(self, system_prompt, user_prompt):
        prefix, separator, payload = user_prompt.rpartition("\n")
        try:
            encoded = _map_sources(json.loads(payload), self.aliases)
        except (ValueError, TypeError):
            return await self.provider.generate_text(system_prompt, user_prompt)
        wire_prompt = prefix+separator+json.dumps(encoded, ensure_ascii=False)
        wire_system = system_prompt+"\n来源编号S属于产品资料概要，R属于主要定期报告，P属于补充招书。两份文档文字相同也必须分别引用本侧编号；不得将R填入summary_source_ids。共享sources字典的键就是引用编号。"
        if len(wire_system)+len(wire_prompt) > self.request_limit:
            raise ValueError("MODEL_CONTEXT_EXCEEDS_BUDGET")
        self.calls += 1
        path = f"comparison/model_wire/{self.calls}"
        if self.trace:
            self.trace({"kind": "artifact", "path": path+"/request.json", "content": {"system": wire_system, "prompt": wire_prompt}})
        response = await self.provider.generate_text(wire_system, wire_prompt)
        if self.trace:
            self.trace({"kind": "artifact", "path": path+"/response.txt", "content": str(response)})
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response).strip(), flags=re.I)
        try:
            parsed, recovered = read_model_json(raw)
            decoded = _map_sources(parsed, self.native_ids)
            if recovered and self.trace:
                self.trace({"kind": "event", "event": "model_json_items_recovered", "details": {
                    "path": path, "complete_items": len(parsed.get("items", [])),
                    "message": "仅保留完整 JSON 条目；未返回或损坏条目仍待核对",
                }})
            text = json.dumps(decoded, ensure_ascii=False)
        except (ValueError, TypeError):
            text = str(response)
        metrics = dict(getattr(response, "metrics", {}) or {})
        metrics.update(wire_prompt_characters=len(wire_prompt), wire_system_characters=len(wire_system),
                       wire_response_characters=len(str(response)), source_alias_version=1,
                       source_alias_trace_path=path if self.trace else None)
        return SourceText(text, metrics)
