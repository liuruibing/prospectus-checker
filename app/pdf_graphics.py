"""Normalize PDF drawing primitives and repeated raster fragments into rules."""
from statistics import median

import pymupdf


def normalize_rules(page, font_size):
    scale = max(font_size, 1)
    images = page.get_image_info()
    candidates = []

    def add(box, image=None):
        box = pymupdf.Rect(box)
        if box.is_empty:
            return
        if min(box.width, box.height) > scale * .18:
            return
        horizontal = box.width >= box.height
        lo, hi = (box.x0, box.x1) if horizontal else (box.y0, box.y1)
        center = (box.y0 + box.y1) / 2 if horizontal else (box.x0 + box.x1) / 2
        candidates.append((horizontal, center, lo, hi, image))
        if image is not None and max(box.width,box.height) <= min(box.width,box.height)*1.2:
            # Raster joins can be square. Test both directions and retain them
            # only when the surrounding repeated placements establish a rule.
            candidates.append((not horizontal,
                               (box.x0+box.x1)/2 if horizontal else (box.y0+box.y1)/2,
                               box.y0 if horizontal else box.x0,
                               box.y1 if horizontal else box.x1, image))

    for drawing in page.get_drawings():
        for item in drawing['items']:
            if item[0] == 're':
                add(item[1])
            elif item[0] == 'l':
                a, b = item[1:3]
                if abs(a.y-b.y) <= scale*.08:
                    add((min(a.x,b.x), a.y-.01, max(a.x,b.x), a.y+.01))
                elif abs(a.x-b.x) <= scale*.08:
                    add((a.x-.01, min(a.y,b.y), a.x+.01, max(a.y,b.y)))
    for index, image in enumerate(images):
        box = pymupdf.Rect(image['bbox'])
        # A small standalone icon remains an image. Only repeated, collinear
        # placements forming a long rule can later be classified as fragments.
        if max(box.width, box.height) <= scale*1.5:
            add(box, index)

    groups = []
    for horizontal, center, lo, hi, image in sorted(candidates, key=lambda c: c[:4]):
        group = next((g for g in reversed(groups)
                      if g['horizontal'] == horizontal and abs(g['center']-center) <= scale*.12), None)
        if group is None:
            group = {'horizontal': horizontal, 'center': center, 'parts': []}
            groups.append(group)
        group['parts'].append((lo, hi, image))

    rules = []
    for group in groups:
        pieces = sorted(group['parts'], key=lambda p: p[:2])
        gap = min(scale*.6, median(b-a for a,b,_ in pieces)*2)
        runs = []
        for a,b,index in pieces:
            if runs and a <= runs[-1]['end'] + gap:
                runs[-1]['end'] = max(runs[-1]['end'], b)
                runs[-1]['parts'].append((a,b,index))
            else:
                runs.append({'start': a, 'end': b, 'parts': [(a,b,index)]})
        for run in runs:
            length = run['end']-run['start']
            raster = [i for _,_,i in run['parts'] if i is not None]
            native = any(i is None for _,_,i in run['parts'])
            coverage = sum(b-a for a,b,_ in run['parts']) / max(length, 1)
            if length < scale*3 or (raster and not native and (len(raster)<3 or coverage<.6)):
                continue
            a,b,c = run['start'], run['end'], group['center']
            line = [[a,c],[b,c]] if group['horizontal'] else [[c,a],[c,b]]
            rules.append({'line': line, 'image_indices': raster,
                          'method': 'raster_run' if raster else 'vector_rule'})
    return images, rules
