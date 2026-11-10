import pytest

from app.headings import parse_heading


@pytest.mark.parametrize('text,depth', [
    ('8.2.1境内股票投资组合',3),
    ('８．２．１ 境内股票投资组合',3),
    ('### 8.2.1境内股票投资组合',3),
    ('**12.3.4** 章节标题',3),
    ('7.4.7.18其他费用',4),
    ('第十三部分 基金费用',1),
    ('（二）基金运作相关费用',2),
    ('3 报告期末股票投资明细',1),
    ('１２ 基金费用与税收',1),
])
def test_heading_depth_comes_from_complete_number_not_spacing(text,depth):
    assert parse_heading(text).depth==depth


@pytest.mark.parametrize('text', [
    '2019-09-06','510100','2026年1月26日','2季度，全球经济情况',
    '0.15%','4.72万元','100 元','510100 基金代码','<tr><td>8.2.1标题</td></tr>',
    '2.证券从业的含义遵从行业协会相关规定。',
    '8.2 行业分类……………………56',
])
def test_values_toc_entries_and_numbered_notes_are_not_headings(text):
    assert parse_heading(text) is None
