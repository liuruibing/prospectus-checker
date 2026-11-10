"""Build continuous Markdown from PDF geometry and verify every source character."""
from collections import Counter
from copy import deepcopy
import hashlib
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pymupdf
from app.pdf_graphics import normalize_rules
from app.headings import parse_heading

PIPELINE_VERSION = "pdf-continuous-md-v5"


def edges(values, tolerance=2):
    result = []
    for value in sorted(values):
        if not result or value - result[-1] > tolerance:
            result.append(value)
    return result


def vertical_rules(page):
    rules = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "re":
                rect = item[1]
                if rect.width < 2 and rect.height > 3:
                    rules.append(((rect.x0+rect.x1)/2, rect.y0, rect.y1))
            elif item[0] == "l":
                a, b = item[1:3]
                if abs(a.x-b.x) < 1 and abs(a.y-b.y) > 3:
                    rules.append(((a.x+b.x)/2, min(a.y,b.y), max(a.y,b.y)))
    return rules


def extend_open_top(cells, rules):
    """Recover a clipped first row when every column border extends above the detected grid."""
    xs = edges([v for c in cells for v in (c[0], c[2])])
    top = min(c[1] for c in cells)
    starts = []
    for x in xs:
        candidates = [lo for vx, lo, hi in rules if abs(vx-x) <= 2 and lo < top-3 and hi >= top-2]
        if not candidates:
            return cells, False
        starts.append(max(candidates))
    if max(starts)-min(starts) > 2 or top-max(starts) > 60:
        return cells, False
    start = max(starts)
    return [(xs[i],start,xs[i+1],top) for i in range(len(xs)-1)]+cells, True


def horizontal_tables(page, atoms, lines, existing, normalized_rules=()):
    """Recover ruled forms with implicit columns; partial horizontal rules retain row spans."""
    rules = []
    for rule in normalized_rules:
        (x0,y0),(x1,y1) = rule['line']
        if abs(y0-y1)<1 and x1-x0>30:
            rules.append((x0,x1,(y0+y1)/2))
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == 're':
                rect = item[1]
                if rect.height < 2 and rect.width > 30:
                    rules.append((rect.x0,rect.x1,(rect.y0+rect.y1)/2))
            if item[0] == "l":
                a, b = item[1:3]
                if abs(a.y-b.y) < 1 and abs(a.x-b.x) > 30:
                    rules.append((min(a.x,b.x), max(a.x,b.x), (a.y+b.y)/2))
    groups = {}
    for x0, x1, y in rules:
        if x1-x0 > page.cropbox.width*.5:
            key = next((k for k in groups if abs(k[0]-x0)<3 and abs(k[1]-x1)<3), (x0,x1))
            groups.setdefault(key, []).append(y)
    result = []
    for (left, right), values in groups.items():
        ys = edges(values)
        runs, run = [], []
        for top, bottom in zip(ys, ys[1:]):
            is_title = any(top < (line['bbox'][1]+line['bbox'][3])/2 < bottom and HEADING.match(compact("".join(atoms[i]["c"] for i in line["ids"])))
                           for line in lines)
            if is_title:
                if len(run)>1:
                    runs.append(run)
                run = []
            else:
                run = run+[bottom] if run else [top,bottom]
        if len(run)>1:
            runs.append(run)
        for row_bounds in runs:
            top,bottom = row_bounds[0],row_bounds[-1]
            box = pymupdf.Rect(left,top,right,bottom)
            if any((box & pymupdf.Rect(t.bbox)).get_area() > min(box.get_area(),pymupdf.Rect(t.bbox).get_area())*.1 for t in existing+result):
                continue
            points = [a for a in atoms if not a["c"].isspace()
                      and left <= (a["box"][0]+a["box"][2])/2 < right
                      and top <= (a["box"][1]+a["box"][3])/2 < bottom]
            baselines = []
            for a in sorted(points, key=lambda a: (a["box"][1],a["box"][0])):
                cy = (a["box"][1]+a["box"][3])/2
                group = next((g for g in reversed(baselines[-3:]) if abs(g[0]-cy)<2),None)
                if group is None:
                    baselines.append([cy,[a]])
                else:
                    group[1].append(a)
            chunks = []
            for _, chars in baselines:
                chunk = []
                for a in sorted(chars,key=lambda a:a["box"][0]):
                    if chunk and a["box"][0]-chunk[-1]["box"][2] > max(5,(a["box"][3]-a["box"][1])*.8):
                        chunks.append(chunk)
                        chunk = []
                    chunk.append(a)
                if chunk:
                    chunks.append(chunk)
            starts = []
            for chunk in chunks:
                x = chunk[0]["box"][0]
                y = chunk[0]["box"][1]
                band = next((i for i in range(len(row_bounds)-1) if row_bounds[i]<=y<row_bounds[i+1]),None)
                group = next((g for g in starts if abs(g[0]-x)<3),None)
                if group is None:
                    starts.append([x,{band}])
                else:
                    group[1].add(band)
            repeated = sorted(x for x,bands in starts if len(bands)>=2)
            if len(row_bounds)==2 and 2<=len(chunks)<=8 and not any(re.search(r"[。；]", "".join(a["c"] for a in c)) for c in chunks):
                repeated = sorted(c[0]["box"][0] for c in chunks)
            vertical = [(a[0],min(a[1],b[1]),max(a[1],b[1])) for r in normalized_rules
                        for a,b in [r['line']] if abs(a[0]-b[0])<1]
            enclosed = all(any(abs(x-edge)<2 and lo<=top+2 and hi>=bottom-2 for x,lo,hi in vertical)
                           for edge in (left,right))
            if len(row_bounds)==2 and len(repeated)<2:
                # Wrapped prose in one cell must not erase a short label in
                # another cell. Repeated line starts identify its text column.
                counts = Counter(round(c[0]['box'][0]) for c in chunks)
                if any(n>=2 for n in counts.values()) and len(starts)>=2:
                    repeated = sorted(x for x,_ in starts)
            if len(repeated)<2 and not enclosed:
                continue
            inner_rules = [(x0,x1,y) for x0,x1,y in rules if top-1<=y<=bottom+1 and left-2<=x0<x1<=right+2]
            xs = edges([left,right]+[x for x0,x1,_ in inner_rules for x in (x0,x1)])
            occupied = sorted((c[0]["box"][0],c[-1]["box"][2]) for c in chunks)
            union = []
            for a,b in occupied:
                if union and a <= union[-1][1]:
                    union[-1][1] = max(union[-1][1],b)
                else:
                    union.append([a,b])
            gutters = [(a[1],b[0]) for a,b in zip(union,union[1:]) if b[0]-a[1]>=5]
            for x,bands in sorted(starts):
                minimum = 1 if len(row_bounds)==2 else 2 if len(xs)==2 else max(3,(len(row_bounds)-1)//2+1)
                if len(bands)<minimum or x <= left+15 or any(abs(x-old)<12 for old in xs):
                    continue
                previous = max(v for v in xs if v<x)
                choices = [(a,b) for a,b in gutters if previous+3<a<b<=x+1]
                if choices:
                    a,b = max(choices,key=lambda gap:gap[1]-gap[0])
                    xs.append((a+b)/2)
            xs.sort()
            all_ys = edges([top,bottom]+[y for _,_,y in inner_rules])
            cells = []
            for x0,x1 in zip(xs,xs[1:]):
                cy = edges([top,bottom]+[y for a,b,y in inner_rules if a-1 <= (x0+x1)/2 <= b+1])
                cells.extend((x0,a,x1,b) for a,b in zip(cy,cy[1:]))
            # Text that crosses an implicit boundary belongs to a horizontally merged cell.
            changed = True
            while changed:
                changed = False
                for i,a in enumerate(cells):
                    b = next((b for b in cells if abs(a[2]-b[0])<1 and a[1:2]==b[1:2] and a[3]==b[3]),None)
                    if b and any(chunk[0]["box"][0] < a[2]-2 < chunk[-1]["box"][2]-4
                                 and a[1] <= (chunk[0]["box"][1]+chunk[0]["box"][3])/2 < a[3] for chunk in chunks):
                        cells.remove(a)
                        cells.remove(b)
                        cells.append((a[0],a[1],b[2],a[3]))
                        changed = True
                        break
            result.append(SimpleNamespace(cells=cells,bbox=tuple(box),method="horizontal_rules"))
    return result


def extract_layout(path):
    segments, pages, characters, atom_count = {}, [], {}, 0
    source = pymupdf.open(stream=path, filetype="pdf") if isinstance(path, bytes) else pymupdf.open(path)
    with source as pdf:
        for number, page in enumerate(pdf, 1):
            atoms, lines = [], []
            for block in page.get_text("rawdict", sort=True)["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    ids = []
                    for span in line["spans"]:
                        for char in span["chars"]:
                            ids.append(len(atoms))
                            atoms.append({"id": f"p{number}-c{len(atoms)}", "c": char["c"], "box": char["bbox"]})
                    lines.append({"ids": ids, "bbox": list(line["bbox"]),
                                  "font_size": max(s["size"] for s in line["spans"])})
            characters.update({a["id"]: {"text": a["c"], "bbox": list(a["box"]), "page": number} for a in atoms})
            assigned = set()

            def segment(ids, bbox):
                key = f"p{number}-s{len(segments)+1}"
                segments[key] = {"page": number, "text": "".join(atoms[i]["c"] for i in ids),
                                 "atoms": [atoms[i]["id"] for i in ids], "bbox": list(bbox)}
                assigned.update(ids)
                return key

            tables = []
            rules = vertical_rules(page)
            font_size = sorted(line['font_size'] for line in lines)[len(lines)//2] if lines else 12
            images, normalized_rules = normalize_rules(page, font_size)
            # 根据实际网格恢复 rowspan/colspan；不使用 MD 列数决定表的身份。
            # Filled text backgrounds are not table borders (PyMuPDF lines_strict).
            detected_tables = list(page.find_tables(strategy="lines_strict").tables)
            detected_tables.extend(horizontal_tables(page,atoms,lines,detected_tables,normalized_rules))
            for index, detected in enumerate(sorted(detected_tables,key=lambda t:(t.bbox[1],t.bbox[0]))):
                cells = sorted(set(c for c in detected.cells if c), key=lambda c: (c[1], c[0]))
                cells, open_top = extend_open_top(cells, rules)
                xs = edges([v for c in cells for v in (c[0], c[2])])
                ys = edges([v for c in cells for v in (c[1], c[3])])
                rows = [[] for _ in ys[:-1]]
                for cell in cells:
                    x0, y0, x1, y1 = cell
                    ids = [i for line in lines for i in sorted(line["ids"], key=lambda i: atoms[i]["box"][0])
                           if i not in assigned and x0 <= (atoms[i]["box"][0]+atoms[i]["box"][2])/2 < x1
                           and y0 <= (atoms[i]["box"][1]+atoms[i]["box"][3])/2 < y1]
                    r0, r1 = [min(range(len(ys)), key=lambda i: abs(ys[i]-v)) for v in (y0, y1)]
                    c0, c1 = [min(range(len(xs)), key=lambda i: abs(xs[i]-v)) for v in (x0, x1)]
                    rows[r0].append({"col": c0, "colspan": c1-c0, "rowspan": r1-r0,
                                     "bbox": list(cell), "segments": [segment(ids, cell)]})
                bbox = [min(c[0] for c in cells),min(c[1] for c in cells),max(c[2] for c in cells),max(c[3] for c in cells)]
                tables.append({"kind": "table", "page": number, "pages": [number], "bbox": bbox,
                               "id": f"p{number}-t{index + 1}",
                               "open_top": open_top,
                               "method": getattr(detected,"method","lines_strict"),
                               "fragments": [{"page": number, "bbox": bbox, "open_top": open_top}],
                               "xs": xs, "rows": rows})
            text = []
            for line in lines:
                remaining = [i for i in line["ids"] if i not in assigned]
                if remaining:
                    text.append({"kind": "text", "page": number, "pages": [number], "bbox": line["bbox"],
                                 "fragments": [{"page": number, "bbox": line["bbox"]}],
                                 "font_size": line["font_size"], "segments": [segment(remaining, line["bbox"])]})
            if assigned != set(range(len(atoms))):
                raise ValueError(f"第 {number} 页有未归属字符")
            atom_count += len(atoms)
            consumed_images = set()
            unresolved_rules = []
            for rule in normalized_rules:
                indices = rule['image_indices']
                if not indices:
                    continue
                (x0,y0),(x1,y1) = rule['line']
                box = pymupdf.Rect(x0-1,y0-1,x1+1,y1+1)
                if any((box & (pymupdf.Rect(t['bbox'])+(-2,-2,2,2))).get_area() >= box.get_area()*.9 for t in tables):
                    consumed_images.update(indices)
                else:
                    unresolved_rules.append(rule)
            pages.append({"page": number, "width": page.cropbox.width, "height": page.cropbox.height,
                          "rotation_matrix": list(page.rotation_matrix),
                          "tables": tables, "blocks": sorted(tables+text, key=lambda b: (b["bbox"][1], b["bbox"][0])),
                          "images": [list(image['bbox']) for i,image in enumerate(images) if i not in consumed_images],
                          "raster_objects": [{'bbox': list(i['bbox']), 'width': i['width'], 'height': i['height']} for i in images],
                          "graphic_rules": normalized_rules,
                          "consumed_image_indices": sorted(consumed_images),
                          "unresolved_graphic_rules": unresolved_rules,
                          "text_sha256": hashlib.sha256("".join(a["c"] for a in atoms).encode()).hexdigest()})
    counts = Counter(i for s in segments.values() for i in s["atoms"])
    if len(counts) != atom_count or any(n != 1 for n in counts.values()):
        raise ValueError("字符归属不守恒")
    return {"characters": characters, "segments": segments, "pages": pages, "atom_count": atom_count}


def text_of(item, segments):
    return "".join(segments[s]["text"] for s in item["segments"])


def compact(text):
    return "".join(text.split())


def compatible_grids(a, b):
    # 合并表头只会保留部分纵线，所以允许一套网格是另一套的子集。
    if abs(a["xs"][0]-b["xs"][0]) > 4 or abs(a["xs"][-1]-b["xs"][-1]) > 4:
        return False
    if a.get("method") == b.get("method") == "horizontal_rules" and len(a["xs"]) == len(b["xs"]):
        return all(abs(x-y) <= (a["xs"][-1]-a["xs"][0])*.04 for x,y in zip(a["xs"],b["xs"]))
    small, large = sorted((a["xs"], b["xs"]), key=len)
    return all(any(abs(x-y) <= 4 for y in large) for x in small)


def table_heading(table, segments):
    cells = [c for c in table["rows"][0] if compact(text_of(c, segments))]
    # Numbered financial statement rows remain data when other columns contain values.
    return (len(cells) == 1 and cells[0]["colspan"] == len(table["xs"])-1
            and bool(HEADING.match(compact(text_of(cells[0], segments)))))


def row_continuation(left, right, segments, open_top=False):
    if [(c["col"], c["colspan"]) for c in left] != [(c["col"], c["colspan"]) for c in right]:
        return False
    if any(c["rowspan"] != 1 for c in left+right):
        return False
    a = [compact(text_of(c, segments)) for c in left]
    b = [compact(text_of(c, segments)) for c in right]
    occupied = [i for i, value in enumerate(b) if value]
    if not occupied or not all(a[i] for i in occupied):
        return False
    if open_top and occupied == [0] and not re.fullmatch(r"[-−]?\d[\d,.]*", b[0]):
        return True
    # 精确数字/日期残片信号：不能把下一行正常金额接到上一行末尾。
    tails = [i for i in occupied if re.fullmatch(r"[-−]?\d[\d,]*\.\d?", a[i]) and re.fullmatch(r"\d{1,2}", b[i])]
    date_tail = any(re.fullmatch(r"\d{4}-\d{1,2}-", a[i]) and re.fullmatch(r"\d{1,2}", b[i]) for i in occupied)
    numeric_tail = date_tail or len(tails) >= 2 or bool(tails and b[0] and not re.search(r"\d", b[0]))
    if numeric_tail:
        if b[0].isdigit() and not date_tail:
            return False
        return all(not re.fullmatch(r"[-−]?\d[\d,.]*%?", b[i]) or re.fullmatch(r"\d{1,2}", b[i]) for i in occupied)
    # 首列行名的括号说明被分页截断，其他列在续页为空；不凭文字长度猜测。
    if occupied == [0]:
        opens, closes = "（([【", "）)]】"
        if any(a[0].count(o) > a[0].count(c) and b[0].count(c) > b[0].count(o)
               for o, c in zip(opens, closes)) and not re.search(r"[。！？；]$", a[0]):
            return True
    if b[0]:
        return False
    # 表头拆字或长文本单元格续写，不能把缺失行名的数值新行当作续行。
    if any(re.fullmatch(r"[-−]?\d[\d,.]*%?|[-—]", b[i]) for i in occupied):
        return False
    return all(not re.search(r"[。！？；]$", a[i]) and
               (len(a[i])+len(b[i]) >= 20 or re.search(r"阶段|收益|净值|标准|基准|比例|期限", "".join(a)))
               for i in occupied)


def merge_table(a, b, segments):
    joined = deepcopy(a)
    if a.get("method") == b.get("method") == "horizontal_rules" and len(a["xs"]) == len(b["xs"]):
        grid = [(x+y)/2 for x,y in zip(a["xs"],b["xs"])]
    else:
        grid = edges(a["xs"]+b["xs"], tolerance=4)
    def remap(rows):
        copied = deepcopy(rows)
        for row in copied:
            for cell in row:
                lo, hi = [min(range(len(grid)), key=lambda i: abs(grid[i]-x)) for x in (cell["bbox"][0], cell["bbox"][2])]
                cell.update(col=lo, colspan=hi-lo)
        return copied
    first, second = remap(a["rows"]), remap(b["rows"])
    action = "continued_table"
    if first and second and row_continuation(first[-1], second[0], segments, b.get("open_top", False)):
        for old, new in zip(first[-1], second.pop(0)):
            old["segments"].extend(new["segments"])
        action = "split_row"
    joined.update(xs=grid, rows=first+second, pages=list(dict.fromkeys(a["pages"]+b["pages"])), bbox=b["bbox"],
                  fragments=a.get("fragments", [])+b.get("fragments", []))
    return joined, action


HEADING = re.compile(r"^(?:§\s*\d+|\d+(?:\.\d+){1,5}\s*[^\d.,%]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十\d]+[）)]|注[：:])")


def recover_borderless_row(previous, body, segments, characters):
    """Recover a uniquely aligned first continuation row without relying on its business label."""
    first = body[0]
    if first["kind"] != "text" or HEADING.match(compact(text_of(first, segments))):
        return None
    size = max(first["font_size"], 6)
    prefix = []
    for block in body:
        if block["kind"] != "text" or block["bbox"][1] > first["bbox"][1] + size * .6:
            break
        if block["bbox"][3] > first["bbox"][3] + size or re.match(r"注[：:]", text_of(block, segments).strip()):
            return None
        prefix.append(block)
    xs = previous["xs"]
    groups = [[] for _ in xs[:-1]]
    old_keys = [key for block in prefix for key in block["segments"]]
    for key in old_keys:
        for atom in segments[key]["atoms"]:
            char = characters[atom]
            x = (char["bbox"][0] + char["bbox"][2]) / 2
            hits = [i for i in range(len(xs)-1) if xs[i] <= x < xs[i+1]]
            if len(hits) != 1:
                return None
            groups[hits[0]].append(atom)
    texts = ["".join(characters[a]["text"] for a in group).strip() for group in groups]
    numeric = re.compile(r"(?:[-−]?\d[\d,]*(?:\.\d+)?%?|[-—–])$")
    # A row needs a nonnumeric label and at least two numeric/dash columns. No inferred values.
    if not any(t and not numeric.fullmatch(compact(t)) for t in texts):
        return None
    if sum(bool(numeric.fullmatch(compact(t))) for t in texts) < 2:
        return None
    for key in old_keys:
        del segments[key]
    cells = []
    page = first["page"]
    y0, y1 = min(b["bbox"][1] for b in prefix), max(b["bbox"][3] for b in prefix)
    for col, atoms in enumerate(groups):
        key = f"p{page}-recovered-{atoms[0]}" if atoms else f"p{page}-empty-{col}-{y0}"
        box = [xs[col], y0, xs[col+1], y1]
        segments[key] = {"page": page, "text": "".join(characters[a]["text"] for a in atoms), "atoms": atoms, "bbox": box}
        cells.append({"col": col, "rowspan": 1, "colspan": 1, "bbox": box, "segments": [key]})
    block = {"id": f"p{page}-recovered-row", "kind": "table", "page": page, "pages": [page],
             "bbox": [xs[0], y0, xs[-1], y1], "xs": xs[:], "rows": [cells],
             "fragments": [{"page": page, "bbox": [xs[0], y0, xs[-1], y1], "method": "inherited_columns"}]}
    body[:len(prefix)] = [block]
    return block


def assemble(layout):
    segments = deepcopy(layout["segments"])
    pages = deepcopy(layout["pages"])
    marginalia, blocks, events = [], [], []
    counts = Counter(compact(text_of(b, segments)) for p in pages for b in p["blocks"]
                     if b["kind"] == "text" and b["bbox"][3] < p["height"]*.075)
    for page in pages:
        body = []
        visual = [{"kind": "image", "page": page["page"], "pages": [page["page"]], "bbox": bbox,
                   "asset": f'assets/p{page["page"]:04d}-image{i+1}.png'} for i, bbox in enumerate(page["images"])]
        for block in sorted(page["blocks"]+visual, key=lambda b: (b["bbox"][1], b["bbox"][0])):
            if block["kind"] == "text":
                text = compact(text_of(block, segments))
                header = block["bbox"][3] < page["height"]*.075 and counts[text] >= 3
                centered = abs((block["bbox"][0]+block["bbox"][2])/2-page.get("width", 595)/2) < 80
                footer = centered and block["bbox"][1] > page["height"]*.90 and bool(re.fullmatch(r"\d+|\d+/\d+|第?\d+页(?:共\d+页)?", text))
                if not text or header or footer:
                    marginalia.append(block)
                    continue
            body.append(block)
        if not body:
            events.append({"page": page["page"], "kind": "empty_body", "merged": False})
            continue
        if blocks:
            left, right = blocks[-1], body[0]
            event = {"page": page["page"], "previous_page": page["page"]-1, "merged": False}
            adjacent = left["pages"][-1] == page["page"]-1
            near_edge = left["bbox"][3] > pages[page["page"]-2]["height"]*.78 and right["bbox"][1] < page["height"]*.22
            if adjacent and near_edge and left["kind"] == "table" and right["kind"] == "text":
                recovered = recover_borderless_row(left, body, segments, layout.get("characters", {}))
                if recovered:
                    right = recovered
                    event["recovered_from_text"] = True
                else:
                    leading = " ".join(text_of(b, segments) for b in body
                                       if b["kind"] == "text" and b["bbox"][1] <= right["bbox"][1]+12)
                    if not HEADING.match(compact(leading)) and not re.match(r"\s*注[：:]", leading):
                        if len(re.findall(r"(?<!\w)(?:\d[\d,.]*|[-—])", leading)) >= 2:
                            event["uncertain_row"] = True
            if adjacent and near_edge and left["kind"] == right["kind"] == "table":
                if compatible_grids(left, right) and not table_heading(right, segments):
                    merged, action = merge_table(left, right, segments)
                    event.update(kind=action, merged=True, columns_before=[len(left["xs"])-1, len(right["xs"])-1],
                                 columns_after=len(merged["xs"])-1)
                    blocks[-1] = merged
                    body.pop(0)
                else:
                    event.update(kind="separate_tables", reason="新标题或网格位置不相容，保留全部内容")
            elif adjacent and near_edge and left["kind"] == right["kind"] == "text":
                a, b = compact(text_of(left, segments)), compact(text_of(right, segments))
                aligned = abs(left["bbox"][0]-right["bbox"][0]) < max(right["font_size"], 4)
                short_title = (HEADING.match(a) or re.match(r"\d+[、)）]", a)) and left["bbox"][2]-left["bbox"][0] < page.get("width", 595)*.65
                if a and b and aligned and not short_title and not re.search(r"[。！？；：:]$", a) and not HEADING.match(b) and not re.match(r"注[：:]|[①②③④⑤⑥]|[（(]\d|\d+[、)）]", b):
                    left["segments"].extend(right["segments"])
                    left["pages"].extend(right["pages"])
                    left["fragments"] = left.get("fragments", [])+right.get("fragments", [])
                    left["bbox"] = right["bbox"]
                    body.pop(0)
                    event.update(kind="continued_text", merged=True)
                else:
                    event.update(kind="text_boundary")
            else:
                event.update(kind="block_boundary")
            events.append(event)
        blocks.extend(body)
    return {"segments": segments, "blocks": blocks, "marginalia": marginalia,
            "atom_count": layout["atom_count"], "events": events}


def render_markdown(document):
    segments = document["segments"]
    def display(text):
        value = compact(text)
        # 仅对整个片段明确是一个小数/日期的情况收拢排版空白，不合并两个整数。
        if re.fullmatch(r"[-−]?\d[\d,]*\.\d+%?|\d{4}-\d{1,2}-\d{1,2}", value):
            return value
        return text.strip()
    def literal(text):
        value = escape(display(text))
        # 行内 HTML 仍会解析 Markdown；公式乘号等原文符号须作为字符实体保留。
        for char in "\\`*_[]~":
            value = value.replace(char, f"&#{ord(char)};")
        return value
    def spans(item):
        keys = item["segments"]
        text = "".join(segments[s]["text"] for s in keys)
        separator = "" if re.fullmatch(r"[-−]?\d[\d,]*\.\d+%?|\d{4}-\d{1,2}-\d{1,2}", compact(text)) else "<br>"
        return separator.join(f'<span data-source="{s}" data-page="{segments[s]["page"]}">{literal(segments[s]["text"])}</span>' for s in keys)
    result = []
    for block in document["blocks"]:
        result.append(f'<!-- source-pages: {",".join(map(str, block["pages"]))} -->')
        if block["kind"] == "image":
            result.append(f'![原图：物理页 {block["page"]}]({block["asset"]})')
        elif block["kind"] == "table":
            result.append('<table>')
            for row in block["rows"]:
                result.append('<tr>' + ''.join(f'<td colspan="{c["colspan"]}" rowspan="{c["rowspan"]}">{spans(c)}</td>'
                                              for c in row) + '</tr>')
            result.append('</table>')
        else:
            text = text_of(block, segments).strip()
            heading = parse_heading(text)
            level = min(6,heading.depth) if heading else 0
            result.append(("#"*level+" " if level and len(text) < 140 else "")+spans(block))
        result.append('')
    if document["marginalia"]:
        result.append('<details data-role="page-marginalia"><summary>原页页眉与页脚（保留出处，不作为正文章节）</summary>')
        for block in document["marginalia"]:
            result.append(f'<p>物理页 {block["page"]}：{spans(block)}</p>')
        result.append('</details>')
    return "\n".join(result)


def verify_markdown(markdown, document):
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.active = [], None
        def handle_starttag(self, tag, attrs):
            if tag == "span":
                self.active = [dict(attrs).get("data-source"), ""]
        def handle_data(self, data):
            if self.active is not None:
                self.active[1] += data
        def handle_endtag(self, tag):
            if tag == "span" and self.active is not None:
                self.parts.append(self.active)
                self.active = None
    reader = Reader()
    reader.feed(markdown)
    counts = Counter(k for k, _ in reader.parts)
    expected = document["segments"]
    missing = sorted(set(expected)-set(counts))
    extra = sorted(set(counts)-set(expected))
    duplicate = sorted(k for k, n in counts.items() if n != 1)
    changed = [k for k, text in reader.parts if k in expected and compact(text) != compact(expected[k]["text"])]
    atoms = Counter(i for key, _ in reader.parts if key in expected for i in expected[key]["atoms"])
    return {"ok": not (missing or extra or duplicate or changed) and len(atoms) == document["atom_count"]
                  and all(n == 1 for n in atoms.values()),
            "missing": missing, "extra": extra, "duplicate": duplicate, "changed": changed,
            "source_characters": document["atom_count"], "output_characters": len(atoms)}


def verify_structure(document, characters):
    issues = []
    for key, segment in document["segments"].items():
        original = "".join(characters[a]["text"] for a in segment["atoms"])
        if original != segment["text"]:
            issues.append(f"segment_text_changed:{key}")
    for block in document["blocks"]:
        if block["kind"] != "table":
            continue
        occupied = set()
        for r, row in enumerate(block["rows"]):
            for cell in row:
                for rr in range(r, r+cell["rowspan"]):
                    for cc in range(cell["col"], cell["col"]+cell["colspan"]):
                        if rr >= len(block["rows"]) or cc >= len(block["xs"])-1 or (rr, cc) in occupied:
                            issues.append(f"invalid_grid:{block['id']}:{r}:{cc}")
                        occupied.add((rr, cc))
    return issues


def convert_pdf_bytes(content: bytes, include_assets: bool = True) -> dict:
    """Create a verifiable continuous Markdown before any model request."""
    layout = extract_layout(content)
    assets, warnings = {}, []
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        for page in layout["pages"]:
            native = pdf[page["page"]-1]
            if page['unresolved_graphic_rules']:
                warnings.append({'page': page['page'], 'code': 'UNRESOLVED_RASTER_RULES',
                                 'reason': '重复图片片段形成的线条未能关联到表格，保留原图并要求复核',
                                 'count': len(page['unresolved_graphic_rules'])})
            # Preserve non-table vector figures as image evidence, alongside raster images.
            table_boxes = [pymupdf.Rect(t["bbox"])+(-2, -2, 2, 2) for t in page["tables"]]
            for box in native.cluster_drawings():
                if box.width < 12 or box.height < 12:
                    continue
                if any((box & t).get_area() >= box.get_area()*.98 for t in table_boxes):
                    continue
                if not any((box & pymupdf.Rect(b)).get_area() >= box.get_area()*.98 for b in page["images"]):
                    page["images"].append(list(box))
            text = "".join(s["text"] for s in layout["segments"].values() if s["page"] == page["page"])
            if not compact(text):
                warnings.append({"page": page["page"], "code": "NO_TEXT_LAYER", "reason": "整页原图保留，无法证明图像文字已转换，需OCR或人工复核"})
                page["images"] = [list(native.cropbox)]
            for i, bbox in enumerate(page["images"], 1):
                name = f"assets/p{page['page']:04d}-image{i}.png"
                if include_assets:
                    assets[name] = native.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), clip=pymupdf.Rect(bbox), alpha=False).tobytes("png")
    document = assemble(layout)
    for event in document["events"]:
        if event.get("kind") == "separate_tables" or event.get("uncertain_row"):
            warnings.append({"page": event["page"], "code": "UNCERTAIN_TABLE_CONTINUATION",
                             "reason": event.get("reason", "续页首行可能属于前表，但列归属不唯一；原文已完整保留")})
    markdown = render_markdown(document)
    verification = verify_markdown(markdown, document)
    verification["structure_issues"] = verify_structure(document, layout["characters"])
    verification["ok"] = verification["ok"] and not verification["structure_issues"]
    if not verification["ok"]:
        raise ValueError("PDF_MD_CONSERVATION_FAILED: "+str(verification)[:1500])
    # The reading view shares the exact blocks; only provenance span wrappers are removed.
    reader = re.sub(r'</?span\b[^>]*>', '', markdown)
    return {"version": PIPELINE_VERSION, "pdf_sha256": hashlib.sha256(content).hexdigest(),
            "status": "REQUIRES_REVIEW" if warnings else "READY", "warnings": warnings,
            "markdown": markdown, "reader_markdown": reader, "verification": verification, "assets": assets,
            "source_map": {**document, "pdf_sha256": hashlib.sha256(content).hexdigest(), "characters": layout["characters"],
                           "original_segments": layout["segments"],
                           "page_fragments": [{k:p[k] for k in ('page','tables','graphic_rules','raster_objects','consumed_image_indices','unresolved_graphic_rules')} for p in layout["pages"]],
                           "pages": [{k: p[k] for k in ("page", "width", "height", "rotation_matrix", "text_sha256")} for p in layout["pages"]]}}


def write_conversion(package: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "document.md").write_text(package["markdown"], encoding="utf-8")
    (output / "reader.md").write_text(package["reader_markdown"], encoding="utf-8")
    for name in ("source_map", "verification"):
        (output / (name.replace('_', '-')+".json")).write_text(json.dumps(package[name], ensure_ascii=False), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps({k: package[k] for k in
        ("version", "pdf_sha256", "status", "warnings")}, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, data in package["assets"].items():
        target = output/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    preview = render_preview(package["source_map"])
    if not verify_markdown(preview, package["source_map"])["ok"]:
        raise ValueError("PDF_MD_PREVIEW_CONTENT_CHANGED")
    (output/"preview.html").write_text(preview, encoding="utf-8")


def render_preview(document):
    sections = []
    for block in document["blocks"]:
        html = render_markdown({**document, "blocks": [block], "marginalia": []})
        if block["kind"] == "table":
            xs = block["xs"]
            columns = "<colgroup>"+"".join(f'<col style="width:{(b-a)/(xs[-1]-xs[0])*100:.3f}%">' for a,b in zip(xs,xs[1:]))+"</colgroup>"
            html = html.replace("<table>", "<table>"+columns)
        elif block["kind"] == "image":
            html = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', lambda m: '<img alt="'+escape(m[1])+'" src="'+escape(m[2])+'">', html)
        elif re.search(r'^#+ ', html, flags=re.M):
            html = re.sub(r'^#+ (.*)$', r'<h3>\1</h3>', html, flags=re.M)
        else:
            html = '<p>'+html+'</p>'
        sections.append(html)
    sections.append(render_markdown({**document, "blocks": []}))
    return ('<!doctype html><meta charset="utf-8"><style>body{font:14px/1.7 sans-serif;max-width:1100px;margin:32px auto;padding:0 20px}'
            'table{border-collapse:collapse;table-layout:fixed;width:100%;margin:16px 0}td{border:1px solid #aaa;padding:6px;vertical-align:top;overflow-wrap:anywhere}'
            'p{margin:8px 0}h3{font-size:16px;margin:20px 0 12px}img{max-width:100%}</style>'+'\n'.join(sections))
