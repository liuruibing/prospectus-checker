"""Read document heading numbers without depending on spaces after the number."""
from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Heading:
    number: str
    depth: int
    title: str
    style: str


def parse_heading(text: str) -> Heading | None:
    text = text.strip()
    if re.match(r'</?(?:table|tr|td|th|details|span)\b', text, re.I):
        return None
    value = unicodedata.normalize('NFKC', re.sub(r'</?u>|[*_`]', '', text, flags=re.I))
    marker = re.match(r'^(#{1,6})\s+', value)
    value = value[marker.end():].strip() if marker else value
    if re.search(r'(?:\.{3,}|…{2,})\s*\d+\s*$', value):
        return None
    patterns = [
        ('section', r'^(§\s*\d+(?:\.\d+)*)\s*(\S.*)', 1),
        ('arabic', r'^(\d+(?:\.\d+)+)(?![\d.])(?:[、.]?\s*)([\u3400-\u9fffA-Za-z《【(].*)', None),
        ('arabic', r'^(\d+[、.])\s*([\u3400-\u9fffA-Za-z《【(].*)', 1),
        ('arabic', r'^(\d{1,3})\s+([\u3400-\u9fffA-Za-z《【(].*)', 1),
        ('chinese', r'^(第[一二三四五六七八九十百]+[章节部分]+)\s*(\S.*)', 1),
        ('chinese', r'^([一二三四五六七八九十百]+、)\s*(\S.*)', 1),
        ('parenthesized', r'^(\([一二三四五六七八九十百]+\))\s*(\S.*)', 2),
    ]
    for style, pattern, depth in patterns:
        match = re.match(pattern, value)
        if not match:
            continue
        number, title = match.groups()
        if style == 'arabic' and (len(number.split('.')[0]) >= 4 or re.fullmatch(r'[%‰]|[万亿千百]?(?:元|股|份|年|月|日|倍|次|人)', title.strip())):
            return None
        # Numbered sentences and footnotes are body content, unless explicitly
        # marked as headings. They must not cut off a containing chapter.
        if not marker and (title.endswith(('。', '；', ';')) or len(title)>140):
            return None
        return Heading(number, depth or len(number.split('.')), title, style)
    if marker:
        return Heading('', len(marker.group(1)), value, 'markdown')
    return None
