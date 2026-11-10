import pytest

from app.model_json import read_model_json


def test_complete_items_survive_broken_wrapper_without_completing_partial_values():
    raw = '{"items":[{"item_id":"a","value":"{完整值}"},{"item_id":"b","value":"未完成'
    data, recovered = read_model_json(raw)
    assert recovered
    assert data == {"items": [{"item_id": "a", "value": "{完整值}"}]}


def test_complete_items_survive_extra_closing_brace():
    data, recovered = read_model_json('{"items":[{"item_id":"a","source_ids":["S1"]}}]')
    assert recovered
    assert data['items'][0]['source_ids'] == ['S1']


def test_broken_value_is_not_invented():
    with pytest.raises(ValueError):
        read_model_json('{"items":[{"value":"未完成')
