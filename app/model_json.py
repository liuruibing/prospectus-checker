"""Read model JSON; salvage only complete items without inventing field values."""
import json
import re


def read_model_json(text: str) -> tuple[dict, bool]:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = clean.find("{"), clean.rfind("}")
    try:
        result = json.loads(clean[start:end + 1])
    except json.JSONDecodeError as error:
        # Some model gateways append surplus closing braces after a complete
        # root object. Recover only that unambiguous case: never ignore a
        # second JSON value or unfinished business fields.
        if start >= 0:
            try:
                result, cursor = json.JSONDecoder().raw_decode(clean[start:])
                tail = clean[start + cursor:]
                if isinstance(result, dict) and tail.strip() and not tail.strip(" \t\r\n}"):
                    return result, True
            except json.JSONDecodeError:
                pass
        match = re.match(r'\{\s*"items"\s*:\s*\[', clean[start:]) if start >= 0 else None
        if not match:
            raise error
        cursor = start + match.end()
        decoder, items = json.JSONDecoder(), []
        while cursor < len(clean):
            while cursor < len(clean) and clean[cursor].isspace():
                cursor += 1
            try:
                item, cursor = decoder.raw_decode(clean, cursor)
            except json.JSONDecodeError:
                break
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not item["item_id"].strip():
                break
            items.append(item)
            while cursor < len(clean) and clean[cursor].isspace():
                cursor += 1
            if cursor >= len(clean) or clean[cursor] != ",":
                break
            cursor += 1
        if not items:
            raise error
        return {"items": items}, True
    if not isinstance(result, dict):
        raise ValueError("AI_INVALID_JSON: expected an object")
    return result, False
