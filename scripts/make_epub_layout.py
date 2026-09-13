#!/usr/bin/env python3
"""Assemble an EPUB3 from geometry-aware OCR of a scanned book.

Input : json/NNN.jsonl  line text + normalized bbox (origin top-left)
        pages/NNN.png   rendered page images
        the source PDF  (for embedded images and page geometry)
Output: EPUB3 with cover, title page, CIP page, nav TOC, chapter/section
        structure from the printed 目录, inline facsimiles, cropped tables
        and per-page footnote blocks.

Usage:
    python3 make_epub_layout.py --config book.json

Everything except `pdf` and `out` has a default; see config.example.json.
"""
import difflib
import html
import io
import json
import os
import re
import statistics
import sys
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps, ImageStat

# ------------------------------------------------------------------ config
_PAGE_KEYS = {
    "total": None,               # page count; default: number of json/*.jsonl
    "cover": 1,                  # PDF page holding the cover artwork, 0 = none
    "cip": None,                 # PDF page holding the CIP block
    "blurb": None,               # PDF page holding the back-cover blurb
    "toc": [],                   # PDF pages of the printed 目录
    "table_toc": [],             # PDF pages of the printed 表目录
    "body_first": None,          # first PDF page of the body text
    "body_last": None,           # last PDF page of the body text
    "printed_page_offset": 0,    # pdf page = printed page + offset
}

# TOC entries that are really one-line-per-item lists, where paragraph
# merging must be disabled
DEFAULT_LIST_SECTIONS = ["参考文献", "图片来源", "附录", "索引", "书目"]


def _config_path():
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
    env = os.environ.get("PDF2EPUB_CONFIG")
    if env:
        return Path(env)
    for candidate in (Path("book.json"), Path(__file__).resolve().parent / "book.json"):
        if candidate.exists():
            return candidate
    return None


def load_config():
    path = _config_path()
    raw = json.loads(path.read_text(encoding="utf-8")) if path else {}
    cfg = {
        "pdf": None,
        "out": None,
        "work": ".",
        "scratch": "/tmp/pdf2epub_build",
        "metadata": {},
        "pages": {},
        "running_head": [],
        "corrections": None,
        "list_sections": None,
    }
    for key, value in raw.items():
        if key in ("pages", "metadata"):
            cfg[key].update(value or {})
        else:
            cfg[key] = value
    for key, default in _PAGE_KEYS.items():
        cfg["pages"].setdefault(key, default)
    if not cfg["pdf"] or not cfg["out"]:
        raise SystemExit(
            "config must set both \"pdf\" and \"out\"; "
            "pass it with --config book.json (see config.example.json)"
        )
    return cfg


CFG = load_config()
PDF = Path(CFG["pdf"]).expanduser()
OUT = Path(CFG["out"]).expanduser()
W = Path(CFG["work"]).expanduser()        # holds pages/ and json/
MO = Path(CFG["scratch"]).expanduser()    # scratch for extracted assets

META = CFG["metadata"]
TITLE = META.get("title") or PDF.stem
AUTHOR = META.get("author", "")
PUBLISHER = META.get("publisher", "")
PUBDATE = META.get("date", "")
ISBN = META.get("isbn", "")
LANGUAGE = META.get("language", "zh-Hans")
BOOK_ID = META.get("identifier") or (
    "urn:isbn:" + re.sub(r"\D", "", ISBN) if ISBN
    else "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, TITLE + "|" + AUTHOR))
)
MODIFIED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

_P = CFG["pages"]
_json_pages = sorted((W / "json").glob("*.jsonl")) if (W / "json").is_dir() else []
N_PAGES = _P["total"] or len(_json_pages)
BODY_LAST = _P["body_last"] or N_PAGES
BODY_FIRST = _P["body_first"] or 1
COVER_PAGE = _P["cover"] or 0
CIP_PAGE = _P["cip"] or 0                 # 0 = no CIP page
BLURB_PAGE = _P["blurb"] or 0             # 0 = no blurb page
TOC_PAGES = _P["toc"] or []
TABLE_TOC_PAGES = _P["table_toc"] or []
PAGE_OFFSET = _P["printed_page_offset"] or 0

if not _json_pages and not N_PAGES:
    raise SystemExit(f"no OCR json found in {W / 'json'}")

_RUNNING_HEADS = None


def running_heads():
    """Printed running heads to strip.

    Taken verbatim from `running_head` when the config lists any; otherwise
    auto-detected as the short strings that recur in the top band of many
    pages (the usual shape of a printed running head).
    """
    global _RUNNING_HEADS
    if _RUNNING_HEADS is None:
        terms = CFG["running_head"]
        if isinstance(terms, str):
            terms = [terms]
        terms = [t for t in terms if t]
        _RUNNING_HEADS = set(terms) if terms else detect_running_heads()
    return _RUNNING_HEADS


def detect_running_heads(min_pages=5):
    counts = Counter()
    for pno in range(BODY_FIRST, BODY_LAST + 1):
        seen = set()
        for row in load_rows(pno):
            if row["y"] > 0.14:
                continue
            core = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", row["text"])
            if 4 <= len(core) <= 24:
                seen.add(core)
        counts.update(seen)
    return {core for core, n in counts.items() if n >= min_pages}


# ------------------------------------------------------------------ text fixes
# OCR fixes are book specific, so there are no built-in ones
CORRECTIONS = [tuple(pair) for pair in (CFG["corrections"] or [])]

# an explicitly empty list here means "treat no section as a list", which is
# why this checks for None rather than for a falsy value
LIST_SECTIONS = (CFG["list_sections"] if CFG["list_sections"] is not None
                 else DEFAULT_LIST_SECTIONS)
LIST_SECTION_RE = re.compile("^(" + "|".join(map(re.escape, LIST_SECTIONS)) + ")")

# z-library / URL stamps burned into the scan
WATERMARK_RE = re.compile(r"z-?library|https?://|www\.", re.IGNORECASE)


def correct(text):
    for bad, good in CORRECTIONS:
        text = text.replace(bad, good)
    return text


def median(values, default=0.0):
    values = [v for v in values if v]
    return statistics.median(values) if values else default


def main_column(rows):
    """x-interval that contains the largest number of OCR lines."""
    if not rows:
        return 0.1, 0.9
    best = (0, 0.1, 0.9)
    for r in rows:
        a, b = r["x"], r["x"] + r["w"]
        for _ in range(2):
            inside = [s for s in rows if s["x"] >= a - 0.02 and s["x"] + s["w"] <= b + 0.02]
            if not inside:
                break
            a = min(s["x"] for s in inside)
            b = max(s["x"] + s["w"] for s in inside)
        if len(inside) > best[0]:
            best = (len(inside), a, b)
    return best[1], best[2]


_CORPUS = None


def corpus_stats():
    """Character/bigram statistics of the whole OCR text (for gibberish detection)."""
    global _CORPUS
    if _CORPUS is None:
        freq, bigrams = Counter(), Counter()
        for pno in range(1, N_PAGES + 1):
            path = W / "json" / f"{pno:03d}.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                text = "".join(c for c in json.loads(line)["text"]
                               if "\u4e00" <= c <= "\u9fff")
                freq.update(text)
                bigrams.update(text[i:i + 2] for i in range(len(text) - 1))
        _CORPUS = ({c for c, n in freq.items() if n >= 25}, bigrams)
    return _CORPUS


def _garbled(text):
    core, bigrams = corpus_stats()
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    if len(chars) < 8:
        return False
    rare = sum(1 for c in chars if c not in core) / len(chars)
    if rare < 0.3:
        return False
    pairs = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    plausible = sum(1 for b in pairs if bigrams[b] >= 4) / len(pairs)
    return plausible < 0.5


def drop_garbled_rows(rows):
    """Remove lines OCR'd from a facsimile image pasted into the page scan."""
    flags = [_garbled(r["text"]) for r in rows]
    if sum(flags) < 3:
        return rows
    return [r for r, bad in zip(rows, flags) if not bad]


# ------------------------------------------------------------------ OCR input
RUNHEAD_RE = re.compile(
    r"^(?:\d{1,3}|"
    r"(?:前言|目录|表目录|图目录|参考文献及资料要目|图片来源)\s*\d{0,3}|"
    r"\d{1,3}\s*[\u4e00-\u9fff]{2,22}|"
    r"第[一二三四五六七八九十]+章.{2,45}\d{1,3}\s*|"
    r"[\u4e00-\u9fff]{2,22}\s*\d{1,3})$"
)
JUNK_RE = re.compile(r"^[0-9①-⑳ⅠⅡⅢⅣⅤ*＊\s\-#—－~〜,，.。、;；:：'\"“”‘’()（）\[\]【】]+$")
NOTE_START_RE = re.compile(r"^[①-⑳⒈-⒛]|^\d{1,2}\s*(?:参见|见|同前|《|页\d)")


def load_rows(pno):
    path = W / "json" / f"{pno:03d}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["text"].strip()]


def merge_fragments(rows):
    """Group OCR fragments into printed lines (top-to-bottom, left-to-right)."""
    lines = []
    for r in sorted(rows, key=lambda r: r["y"] + r["h"] / 2):
        centre = r["y"] + r["h"] / 2
        if lines and abs(centre - lines[-1]["centre"]) <= 0.5 * min(r["h"], lines[-1]["h_last"]):
            line = lines[-1]
            line["rows"].append(r)
            line["centre"] = sum(x["y"] + x["h"] / 2 for x in line["rows"]) / len(line["rows"])
            line["h"] = max(line["h"], r["h"])
            line["h_last"] = r["h"]
        else:
            lines.append({"centre": centre, "h": r["h"], "h_last": r["h"], "rows": [r]})
    out = []
    for line in lines:
        parts = sorted(line["rows"], key=lambda r: r["x"])
        group = []

        def flush_group():
            if not group:
                return
            text = ""
            for i, r in enumerate(group):
                if text and r["x"] - (group[i - 1]["x"] + group[i - 1]["w"]) >= 0.03:
                    text += " "
                text += r["text"]
            x0 = min(r["x"] for r in group)
            x1 = max(r["x"] + r["w"] for r in group)
            y0 = min(r["y"] for r in group)
            y1 = max(r["y"] + r["h"] for r in group)
            out.append({"text": correct(text.strip()), "x": x0, "y": y0,
                        "w": x1 - x0, "h": y1 - y0})
            group.clear()

        for r in parts:
            if group and r["x"] - (group[-1]["x"] + group[-1]["w"]) > 0.05:
                flush_group()
            group.append(r)
        flush_group()
    return out


def is_running_head(row):
    if row["y"] > 0.135:
        return False
    t = row["text"].strip()
    if not t or len(t) > 40:
        return False
    if any(term in t for term in running_heads()):
        return True
    return bool(RUNHEAD_RE.match(t))


def is_junk(row, body_h):
    t = row["text"].strip()
    if not t:
        return True
    if len(t) <= 2 and JUNK_RE.match(t):
        return True
    if len(t) <= 3 and JUNK_RE.match(t) and row["h"] < body_h * 0.85:
        return True
    return False


def split_notes(rows, body_h):
    """Split the bottom footnote block off a page."""
    if len(rows) < 4:
        return rows, []
    med = median([rows[i]["y"] - rows[i - 1]["y"] for i in range(1, len(rows))], 0.024)
    cut = None
    for i in range(2, len(rows)):
        if rows[i]["y"] < 0.45:
            continue
        if rows[i]["y"] - rows[i - 1]["y"] < max(0.045, 1.9 * med):
            continue
        if not NOTE_START_RE.match(rows[i]["text"]):
            continue
        cut = i
    if cut is None:
        return rows, []
    return rows[:cut], rows[cut:]


def group_notes(rows):
    notes = []
    for r in rows:
        t = r["text"].strip()
        if not t:
            continue
        if notes and not NOTE_START_RE.match(t):
            notes[-1] += t
        else:
            notes.append(t)
    return notes


# ------------------------------------------------------------------ TOC parse
TOC_HEADING_WORDS = {"目录", "表目录", "图目录"}
ENTRY_START_RE = re.compile(
    r"^第[一二三四五六七八九十]+章|^\d{1,2}\s*[.．、]|^引言$|^结语$|^余论$|"
    r"^前言$|^参考文献|^图片来源|^附录|^后记"
)
LEVEL1_RE = re.compile(r"^第[一二三四五六七八九十]+章")


def parse_toc():
    """Ordered TOC entries (title, level) from the printed 目录 pages."""
    entries = []
    for pno in TOC_PAGES:
        rows = [r for r in merge_fragments(load_rows(pno)) if not is_running_head(r)]
        rows = [r for r in rows if r["text"].strip() not in TOC_HEADING_WORDS]
        rows = [r for r in rows if r["x"] < 0.7]
        for row in sorted(rows, key=lambda r: r["y"]):
            for part in split_toc_line(row["text"]):
                if entries and not ENTRY_START_RE.match(part):
                    entries[-1]["title"] += " " + part
                else:
                    level = 1 if (LEVEL1_RE.match(part)
                                  or part in {"前言", "参考文献及资料要目", "图片来源"}) else 2
                    entries.append({"title": part, "level": level})
    for e in entries:
        e["title"] = re.sub(r"\s+", " ", e["title"]).strip()
        e["title"] = re.sub(r"\s+\d{1,3}$", "", e["title"]).strip()
    return [e for e in entries if len(re.sub(r"[^\u4e00-\u9fff\w]", "", e["title"])) >= 2]


def split_toc_line(text):
    """Split one printed TOC line into entry strings (page numbers removed)."""
    t = re.sub(r"(?<=\s)\d{1,3}(?=\s|$)", " ", text)
    t = re.sub(r"[\s·.．⋯—－\-]*\d{1,3}\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []
    parts = re.split(r"(?<=[\s）)】》”\"])"
                     r"(?=第[一二三四五六七八九十]+章|引言(?=$|\s)|结语(?=$|\s)|"
                     r"余论(?=$|\s)|\d{1,2}\s*[.．、]\s*[^\d])", t)
    out = []
    for p in parts:
        p = re.sub(r"[\s—－·．.\-]+$", "", p).strip()
        p = re.sub(r"\s+\d{1,3}$", "", p).strip()
        if p:
            out.append(p)
    return out


def parse_table_toc():
    """[(table_label, printed_page)] from the printed 表目录."""
    tables = []
    for pno in TABLE_TOC_PAGES:
        rows = [r for r in merge_fragments(load_rows(pno)) if not is_running_head(r)]
        rows = [r for r in rows if r["text"].strip() not in TOC_HEADING_WORDS]
        titles = [r for r in rows if r["x"] < 0.7]
        numbers = [r for r in rows if r["x"] > 0.75 and re.fullmatch(r"\d{1,3}", r["text"].strip())]
        buf = []
        for row in sorted(titles, key=lambda r: r["y"]):
            buf.append(row)
            num = next((n for n in numbers if abs(n["y"] - row["y"]) < 0.016), None)
            if num:
                title = re.sub(r"\s+", "", " ".join(b["text"].strip() for b in buf))
                label = title.lstrip("表").split("：")[0].split(":")[0].strip()
                tables.append((label, int(num["text"].strip())))
                buf = []
    return tables


# ------------------------------------------------------------------ page model
class PageObj:
    def __init__(self, pno, doc):
        self.pno = pno
        rows = merge_fragments(load_rows(pno))
        self.images = []
        page = doc[pno - 1]
        page_area = abs(page.rect.width * page.rect.height)
        pw, ph = page.rect.width, page.rect.height
        for info in page.get_image_info(xrefs=True):
            x0, y0, x1, y1 = info["bbox"]
            if abs((x1 - x0) * (y1 - y0)) > 0.9 * page_area:
                continue
            self.images.append({
                "bbox": (x0 / pw, y0 / ph, x1 / pw, y1 / ph),
                "xref": info["xref"],
            })
        rows = [r for r in rows if not is_running_head(r)]
        self.body_h = median([r["h"] for r in rows if len(r["text"]) >= 10], 0.02)
        self.left, self.right = main_column(rows)
        self.col_w = max(0.2, self.right - self.left)
        keep = []
        for r in rows:
            cx, cy = r["x"] + r["w"] / 2, r["y"] + r["h"] / 2
            if any(x0 - 0.005 <= cx <= x1 + 0.005 and y0 - 0.005 <= cy <= y1 + 0.005
                   for x0, y0, x1, y1 in (im["bbox"] for im in self.images)):
                continue
            keep.append(r)
        rows = [r for r in keep if not is_junk(r, self.body_h)]
        rows = self._drop_off_column(rows)
        rows, self.side_crops = self._split_side_column(rows)
        rows = drop_garbled_rows(rows)
        rows, self.notes = split_notes(rows, self.body_h)
        self.rows = rows

    def _split_side_column(self, rows):
        """Detect a second text column (baked-in facsimile) and keep it as an image."""
        if len(rows) < 10:
            return rows, []
        # rows sharing a baseline but far apart horizontally => two columns
        pairs = []
        for i in range(len(rows)):
            ci = rows[i]["y"] + rows[i]["h"] / 2
            for j in range(i + 1, len(rows)):
                cj = rows[j]["y"] + rows[j]["h"] / 2
                if abs(ci - cj) > 0.012:
                    continue
                a = (rows[i]["x"], rows[i]["x"] + rows[i]["w"])
                b = (rows[j]["x"], rows[j]["x"] + rows[j]["w"])
                if a[0] > b[0]:
                    a, b = b, a
                if b[0] - a[1] > 0.05:
                    pairs.append((a, b))
        if len(pairs) < 4:
            return rows, []
        left_range = (min(p[0][0] for p in pairs), max(p[0][1] for p in pairs))
        right_range = (min(p[1][0] for p in pairs), max(p[1][1] for p in pairs))

        def contained(rng):
            return [r for r in rows
                    if r["x"] >= rng[0] - 0.03 and r["x"] + r["w"] <= rng[1] + 0.03]

        left_rows, right_rows = contained(left_range), contained(right_range)
        if min(len(left_rows), len(right_rows)) < 5:
            return rows, []
        side = left_rows if len(left_rows) < len(right_rows) else right_rows
        main = [r for r in rows if r not in side]
        x0 = min(r["x"] for r in side)
        x1 = max(r["x"] + r["w"] for r in side)
        y0 = min(r["y"] for r in side)
        y1 = max(r["y"] + r["h"] for r in side)
        if x1 - x0 < 0.08 or y1 - y0 < 0.06:
            return rows, []
        # the side content must sit beside the main column, not below it
        main_y = sorted(r["y"] for r in main)
        if not main_y or y1 < main_y[len(main_y) // 4]:
            return rows, []
        crop = {"page": self.pno, "kind": "table",
                "box": (max(0.0, x0 - 0.01), max(0.0, y0 - 0.01),
                        min(1.0, x1 + 0.01), min(1.0, y1 + 0.01)),
                "name": f"side-{self.pno:03d}.jpg"}
        return main, [crop]

    def _drop_off_column(self, rows):
        """Drop OCR lines that sit outside the body text column (photo edges)."""
        wide = [r for r in rows if r["w"] > 0.5 * self.col_w and len(r["text"]) >= 8]
        if len(wide) < 4:
            return rows
        lefts = sorted(r["x"] for r in wide)
        rights = sorted(r["x"] + r["w"] for r in wide)
        left = lefts[len(lefts) // 10]
        right = rights[-max(1, len(rights) // 10)]
        out = []
        for r in rows:
            centre = r["x"] + r["w"] / 2
            if centre < left - 0.06 or centre > right + 0.06:
                continue
            out.append(r)
        return out


class Doc:
    def __init__(self):
        self.blocks = []
        self.para = None
        self.pending_notes = []
        self.quote = None
        self.page = 0

    def _tag(self, block):
        block["page"] = self.page
        return block

    def flush(self):
        if self.para:
            self.blocks.append(self._tag({"kind": "p", "text": self.para}))
            self.para = None
        if self.quote:
            self.blocks.append(self._tag({"kind": "blockquote", "text": self.quote}))
            self.quote = None
        for note in self.pending_notes:
            self.blocks.append(self._tag({"kind": "note", "text": note}))
        self.pending_notes = []

    def heading(self, level, text):
        self.flush()
        self.blocks.append(self._tag({"kind": level, "text": text}))

    def figure(self, meta):
        self.flush()
        self.blocks.append(self._tag({"kind": "figure", "image": meta}))

    def add_notes(self, notes):
        self.pending_notes.extend(notes)

    def add_row(self, row, page, first_of_page):
        indent = row["x"] - page.left > 0.022
        if self.quote is not None:
            self.flush()
        if self.para is None and self.quote is None:
            self.para = row["text"]
        elif indent:
            self.flush()
            self.para = row["text"]
        else:
            self.para = (self.para or "") + row["text"]

    def add_quote_row(self, row):
        if self.para:
            self.flush()
        self.quote = (self.quote or "") + row["text"]


# ------------------------------------------------------------------ assembly
def locate_entries(pages, entries):
    """Find each TOC entry in the body, walking the pages in reading order."""
    located = []
    start = BODY_FIRST
    for entry in entries:
        target = re.sub(r"\s+", "", entry["title"])
        tlen = len(target)
        best = None
        for pno in range(start, BODY_LAST + 1):
            page = pages[pno]
            best_here = None
            for i, r in enumerate(page.rows):
                t = re.sub(r"\s+", "", r["text"])
                if not t or len(t) < 0.5 * tlen or len(t) > 2 * tlen + 8:
                    continue
                ratio = difflib.SequenceMatcher(None, t, target).ratio()
                if best_here is None or ratio > best_here[2]:
                    best_here = (pno, i, ratio)
            if best_here and (best is None or best_here[2] > best[2]):
                best = best_here
            if best_here and best_here[2] >= 0.88:
                best = best_here
                break
            if (best_here and best_here[2] >= 0.62 and page.rows
                    and all(r["h"] > 0.03 for r in page.rows)):
                best = best_here          # chapter title page
                break
        item = dict(entry)
        if best and best[2] >= 0.6:
            item.update(page=best[0], idx=best[1], ratio=round(best[2], 3))
            start = best[0]
        else:
            item.update(page=None, idx=None, ratio=round(best[2], 3) if best else 0.0)
        located.append(item)
    return located


def is_quote_row(row, page):
    indent = row["x"] - page.left > 0.022
    narrower = (row["x"] + row["w"]) < page.right - 0.02
    return indent and narrower and row["w"] < 0.93 * page.col_w


def build_document(pages, located, table_pages):
    by_page = {}
    for e in located:
        if e["page"]:
            by_page.setdefault(e["page"], []).append(e)

    doc = Doc()

    # sections that are lists (bibliography, photo credits): one line = one entry
    list_ranges = []
    starts = sorted(e["page"] for e in located
                    if e["page"] and LIST_SECTION_RE.match(e["title"]))
    for i, start in enumerate(starts):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else BODY_LAST
        list_ranges.append((start, end))

    # per page: heading rows (index -> entry) and whether it is a title page
    hit_cache, title_pages, first_traits = {}, set(), {}
    for pno, page in pages.items():
        hits = {}
        for e in by_page.get(pno, []):
            hits.setdefault(e["idx"], e)
        hit_cache[pno] = hits
        if hits and page.rows and all(r["h"] > 0.03 for r in page.rows):
            title_pages.add(pno)
        first_is_heading = pno in title_pages or (0 in hits)
        first_indent = bool(page.rows) and (page.rows[0]["x"] - page.left > 0.022)
        first_traits[pno] = (first_is_heading, first_indent)

    for pno in sorted(pages):
        if pno < BODY_FIRST:
            continue
        page = pages[pno]
        doc.page = pno
        rows = page.rows
        hits = hit_cache[pno]
        in_list_page = any(a <= pno <= z for a, z in list_ranges)
        entry_x = 0.0
        if in_list_page and rows:
            starts_x = sorted(r["x"] for r in rows)
            entry_x = max(starts_x, key=lambda x: sum(1 for y in starts_x if abs(y - x) < 0.01))
        if pno in title_pages:
            entry = next(iter(hit_cache[pno].values()))
            doc.flush()
            doc.heading("h1", entry["title"])
            rows = []

        if rows and (pno in table_pages or TABLE_CAP_RE.match(rows[0]["text"])):
            emit_table(doc, page, rows)
            continue

        quote_flags = [False] * len(rows)
        j = 0
        while j < len(rows):
            if is_quote_row(rows[j], page):
                k = j
                while k + 1 < len(rows) and is_quote_row(rows[k + 1], page):
                    k += 1
                if k > j:
                    for m in range(j, k + 1):
                        quote_flags[m] = True
                j = k + 1
            else:
                j += 1

        figures = {}
        for image in page.images:
            anchor = len(rows)
            y1 = image["bbox"][3]
            for i, r in enumerate(rows):
                if r["y"] > y1 - 0.01:
                    anchor = i
                    break
            figures.setdefault(anchor, []).append(image)
        for crop in page.side_crops:
            anchor = len(rows)
            y1 = crop["box"][3]
            for i, r in enumerate(rows):
                if r["y"] > y1:
                    anchor = i
                    break
            figures.setdefault(anchor, []).append(crop)

        for idx, row in enumerate(rows):
            for image in figures.get(idx, []):
                doc.figure(figure_meta(pno, image))
            if idx in hits:
                entry = hits[idx]
                doc.heading("h1" if entry["level"] == 1 else "h2", entry["title"])
                continue
            if in_list_page:
                if row["x"] > entry_x - 0.012:
                    doc.flush()
                    doc.para = row["text"]
                elif doc.para:
                    doc.para += row["text"]
                else:
                    doc.para = row["text"]
                continue
            if quote_flags[idx]:
                doc.add_quote_row(row)
                continue
            doc.add_row(row, page, first_of_page=(idx == 0))

        for image in figures.get(len(rows), []):
            doc.figure(figure_meta(pno, image))

        doc.add_notes(group_notes(page.notes))

        # decide whether the open paragraph continues on the next page
        if doc.para is not None:
            nxt = pages.get(pno + 1)
            if nxt is None:
                doc.flush()
            else:
                first_is_heading, first_indent = first_traits[pno + 1]
                if first_is_heading or first_indent or not nxt.rows:
                    doc.flush()
    doc.flush()
    return doc.blocks


TABLE_CAP_RE = re.compile(r"^(表\s*\d+\s*[-—]\s*\d+|续表\s*[:：]?)")


def table_span(rows, start, col_w):
    """Rows [start, end) that belong to a typeset table."""
    end = len(rows)
    for j in range(start + 1, len(rows)):
        t = rows[j]["text"]
        cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
        body_like = (len(t) >= 25 and cjk >= 15 and cjk / len(t) >= 0.72
                     and rows[j]["w"] > 0.5 * col_w)
        if body_like and not re.match(r"^(说明|注\s*[:：]|资料来源|备注)", t):
            end = j
            break
    return end


def figure_meta(pno, item):
    """Normalise an embedded image or a page-region crop into a figure record."""
    if "xref" in item:
        return {"page": pno, "image": item,
                "name": f"img-{pno:03d}-{item['xref']}.jpg", "kind": "image"}
    return dict(item)


def emit_table(doc, page, rows):
    """Keep a typeset table as a cropped image plus its caption/label text."""
    cap = next((i for i, r in enumerate(rows) if TABLE_CAP_RE.match(r["text"])), None)
    if cap is None:
        for idx, row in enumerate(rows):
            doc.add_row(row, page, first_of_page=(idx == 0))
        doc.add_notes(group_notes(page.notes))
        return
    start = cap + 1
    end = table_span(rows, start, page.col_w)
    table_rows = rows[start:end]
    for idx, row in enumerate(rows[:cap]):
        doc.add_row(row, page, first_of_page=(idx == 0))
    doc.para = rows[cap]["text"]
    doc.flush()
    if table_rows:
        x0 = min(r["x"] for r in table_rows) - 0.02
        x1 = max(r["x"] + r["w"] for r in table_rows) + 0.02
        y0 = rows[cap]["y"] - 0.008
        y1 = max(r["y"] + r["h"] for r in table_rows) + 0.012
        doc.figure({"page": page.pno, "box": (x0, y0, x1, y1),
                    "name": f"table-{page.pno:03d}.jpg", "kind": "table"})
    for idx, row in enumerate(rows[end:]):
        doc.add_row(row, page, first_of_page=(idx == 0))
    doc.add_notes(group_notes(page.notes))


# ------------------------------------------------------------------ assets
def export_assets(pages, blocks, doc_pdf):
    MO.mkdir(parents=True, exist_ok=True)
    (MO / "images").mkdir(exist_ok=True)
    assets = {}

    def save_extracted(xref, name):
        if name in assets:
            return
        info = doc_pdf.extract_image(xref)
        path = MO / "images" / name
        path.write_bytes(info["image"])
        if info["ext"] not in ("jpg", "jpeg"):
            img = Image.open(path)
            jpg = path.with_suffix(".jpg")
            img.convert("RGB").save(jpg, "JPEG", quality=88)
            path.unlink()
            name = jpg.name
        assets[name] = f"images/{name}"

    for block in blocks:
        if block["kind"] != "figure":
            continue
        meta = block["image"]
        if meta.get("kind") == "image":
            save_extracted(meta["image"]["xref"], meta["name"])
        else:
            name = meta["name"]
            if name in assets:
                continue
            src = Image.open(W / "pages" / f"page-{meta['page']:03d}.png")
            if ImageStat.Stat(src.convert("L")).mean[0] < 128:
                src = ImageOps.invert(src.convert("RGB"))
            w, h = src.size
            x0, y0, x1, y1 = meta["box"]
            crop = src.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
            if crop.width > 2400:
                ratio = 2400 / crop.width
                crop = crop.resize((2400, int(crop.height * ratio)), Image.LANCZOS)
            crop = crop.convert("RGB")
            path = MO / "images" / name
            crop.save(path, "JPEG", quality=88)
            assets[name] = f"images/{name}"
    return assets


def export_cover(doc_pdf):
    """Cover artwork: the largest embedded image on the cover page, else the
    rendered page image (some books put the whole cover in as one scan)."""
    if not COVER_PAGE:
        return None
    MJO = MO / "images"
    MJO.mkdir(parents=True, exist_ok=True)
    page = doc_pdf[COVER_PAGE - 1]
    infos = [i for i in page.get_image_info(xrefs=True)]
    if infos:
        infos.sort(key=lambda i: -(i["width"] * i["height"]))
        data = doc_pdf.extract_image(infos[0]["xref"])
        img = Image.open(io.BytesIO(data["image"])).convert("RGB")
    else:
        src = W / "pages" / f"page-{COVER_PAGE:03d}.png"
        if not src.exists():
            return None
        img = Image.open(src).convert("RGB")
    if img.width > 1400:
        img = img.resize((1400, int(img.height * 1400 / img.width)), Image.LANCZOS)
    path = MJO / "cover.jpg"
    img.save(path, "JPEG", quality=90)
    return "images/cover.jpg"


# ------------------------------------------------------------------ epub write
CSS = """\
@charset "utf-8";
body { font-family: "Songti SC", "Noto Serif CJK SC", "Source Han Serif SC", serif;
       line-height: 1.8; color: #1a1a1a; margin: 0 5%; text-align: justify; }
h1 { font-size: 1.5em; font-weight: bold; text-align: center;
     margin: 1.6em 0 1.2em; line-height: 1.5; }
h2 { font-size: 1.15em; font-weight: bold; margin: 1.4em 0 0.7em; }
p  { margin: 0.35em 0; text-indent: 2em; }
blockquote { margin: 0.8em 1.5em; font-size: 0.95em; }
blockquote p { text-indent: 2em; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; max-height: 92vh; }
.table-figure img { border: 1px solid #ddd; }
div.notes { margin-top: 1.6em; padding-top: 0.6em; border-top: 1px solid #999;
            font-size: 0.82em; line-height: 1.6; }
p.note { text-indent: 0; margin: 0.15em 0; text-align: left; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-height: 100vh; max-width: 100%; }
.title-page { text-align: center; margin-top: 18%; }
.title-page h1 { font-size: 1.9em; margin-bottom: 1.6em; }
.title-page .author { font-size: 1.2em; margin: 2em 0; }
.title-page .publisher { font-size: 1.05em; margin-top: 4em; }
.cip { font-size: 0.9em; line-height: 1.9; }
nav ol { list-style: none; padding-left: 0.8em; }
nav li { margin: 0.3em 0; }
"""


def xhtml(title, body, css_path="../styles/style.css"):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-Hans" lang="zh-Hans">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="{css_path}"/>
</head>
<body>
{body}
</body>
</html>
"""


def esc(text):
    return html.escape(text, quote=False)


def render_blocks(blocks, assets):
    """Render a block list to XHTML; returns (body, headings)."""
    parts, headings = [], []
    open_notes = False
    for block in blocks:
        kind = block["kind"]
        if kind != "note" and open_notes:
            parts.append("</div>")
            open_notes = False
        if kind == "h1":
            hid = f"h{len(headings)}"
            headings.append((1, block["text"], hid))
            parts.append(f'<h1 id="{hid}">{esc(block["text"])}</h1>')
        elif kind == "h2":
            hid = f"h{len(headings)}"
            headings.append((2, block["text"], hid))
            parts.append(f'<h2 id="{hid}">{esc(block["text"])}</h2>')
        elif kind == "p":
            parts.append(f"<p>{esc(block['text'])}</p>")
        elif kind == "blockquote":
            parts.append(f"<blockquote><p>{esc(block['text'])}</p></blockquote>")
        elif kind == "figure":
            src = assets[block["image"]["name"]]
            cls = ' class="table-figure"' if block["image"].get("kind") == "table" else ""
            parts.append(f'<figure{cls}><img src="../{src}" alt=""/></figure>')
        elif kind == "note":
            if not open_notes:
                parts.append('<div class="notes">')
                open_notes = True
            parts.append(f'<p class="note">{esc(block["text"])}</p>')
    if open_notes:
        parts.append("</div>")
    return "\n".join(parts), headings


def split_into_docs(blocks):
    """Split the block list into spine documents at h1/h2 boundaries."""
    if not any(b["kind"] in ("h1", "h2") for b in blocks):
        # no usable 目录: fall back to fixed-size chunks so a long book does
        # not end up as one enormous XHTML file
        return [blocks[i:i + 24] for i in range(0, len(blocks), 24)]
    docs = []
    current = []
    for block in blocks:
        if block["kind"] in ("h1", "h2") and current:
            docs.append(current)
            current = []
        current.append(block)
    if current:
        docs.append(current)
    return docs


def main():
    doc_pdf = pymupdf.open(PDF)
    pages = {p: PageObj(p, doc_pdf) for p in range(1, BODY_LAST + 1)}
    toc_entries = parse_toc()
    table_toc = parse_table_toc()
    table_pages = {}
    for label, printed in table_toc:
        table_pages.setdefault(printed + PAGE_OFFSET, label)

    located = locate_entries(pages, toc_entries)
    unmatched = [e for e in located if not e["page"]]
    blocks = build_document(pages, located, table_pages)

    # front matter handled separately: CIP block and back-cover blurb
    cip_text = ""
    if CIP_PAGE in pages:
        cip_text = "\n".join(f"<p>{esc(r['text'])}</p>" for r in pages[CIP_PAGE].rows)
    blurb_text = ""
    if BLURB_PAGE in pages:
        blurb_text = "".join(r["text"] for r in pages[BLURB_PAGE].rows
                             if len(r["text"]) > 12
                             and not WATERMARK_RE.search(r["text"]))

    body_docs = split_into_docs(blocks)
    assets = export_assets(pages, blocks, doc_pdf)
    cover = export_cover(doc_pdf)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""")
        z.writestr("OEBPS/styles/style.css", CSS)

        # static front matter pages
        front_docs = []
        if cover:
            front_docs.append(("cover", "封面",
                               f'<div class="cover"><img src="../{cover}" alt="封面"/></div>'))
        title_body = ["<div class=\"title-page\">", f"<h1>{esc(TITLE)}</h1>"]
        if AUTHOR:
            title_body.append(f'<p class="author">{esc(AUTHOR)} 著</p>')
        for line in (PUBLISHER, PUBDATE):
            if line:
                title_body.append(f'<p class="publisher">{esc(line)}</p>')
        title_body.append("</div>")
        front_docs.append(("titlepage", "书名页", "".join(title_body)))
        if cip_text or blurb_text:
            cip_body = ['<div class="cip">']
            if blurb_text:
                cip_body.append(f"<h2>内容简介</h2><p>{esc(blurb_text)}</p>")
            if cip_text:
                cip_body.append("<h2>图书在版编目（CIP）数据</h2>")
                cip_body.append(cip_text)
            if ISBN:
                cip_body.append(f"<p>ISBN {esc(ISBN)}</p>")
            cip_body.append("</div>")
            front_docs.append(("cip", "版权信息", "".join(cip_body)))
        for fid, ftitle, fbody in front_docs:
            z.writestr(f"OEBPS/text/{fid}.xhtml", xhtml(ftitle, fbody))

        # body documents
        manifest = ['<item id="css" href="styles/style.css" media-type="text/css"/>',
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
        spine, nav_items = [], []
        if cover:
            manifest.append('<item id="cover-img" href="' + cover
                            + '" media-type="image/jpeg" properties="cover-image"/>')
        for fid, ftitle, _ in front_docs:
            manifest.append(f'<item id="{fid}" href="text/{fid}.xhtml"'
                            ' media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{fid}"/>')
            nav_items.append((ftitle, f"text/{fid}.xhtml", 1))

        for i, doc_blocks in enumerate(body_docs, start=1):
            body, headings = render_blocks(doc_blocks, assets)
            name = f"text/p{i:04d}.xhtml"
            first_title = headings[0][1] if headings else ""
            z.writestr("OEBPS/" + name, xhtml(first_title or TITLE, body))
            manifest.append(f'<item id="p{i:04d}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{i:04d}"/>')
            for level, title, hid in headings:
                nav_items.append((title, f"{name}#{hid}", level))

        for name, src in sorted(assets.items()):
            manifest.append(f'<item id="{re.sub(r"[^A-Za-z0-9]", "_", name)}" '
                            f'href="{src}" media-type="image/jpeg"/>')
            z.write(MO / src, "OEBPS/" + src)
        if cover:
            z.write(MO / cover, "OEBPS/" + cover)

        # nav
        ol = []
        i = 0
        while i < len(nav_items):
            title, href, level = nav_items[i]
            if level == 1:
                children = []
                j = i + 1
                while j < len(nav_items) and nav_items[j][2] == 2:
                    t, h, _ = nav_items[j]
                    children.append(f'<li><a href="{h}">{esc(t)}</a></li>')
                    j += 1
                sub = f"<ol>{''.join(children)}</ol>" if children else ""
                ol.append(f'<li><a href="{href}">{esc(title)}</a>{sub}</li>')
                i = j
            else:
                ol.append(f'<li><a href="{href}">{esc(title)}</a></li>')
                i += 1
        nav = xhtml("目录",
                    '<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">'
                    "<h1>目录</h1><ol>" + "".join(ol) + "</ol></nav>",
                    css_path="styles/style.css")
        z.writestr("OEBPS/nav.xhtml", nav)

        dc = [f"<dc:title>{esc(TITLE)}</dc:title>"]
        if AUTHOR:
            dc.append(f"<dc:creator>{esc(AUTHOR)}</dc:creator>")
        if PUBLISHER:
            dc.append(f"<dc:publisher>{esc(PUBLISHER)}</dc:publisher>")
        if PUBDATE:
            dc.append(f"<dc:date>{esc(PUBDATE)}</dc:date>")
        dc.append(f'<dc:identifier id="uid">{esc(BOOK_ID)}</dc:identifier>')
        dc.append(f"<dc:language>{esc(LANGUAGE)}</dc:language>")
        dc.append(f'<meta property="dcterms:modified">{MODIFIED}</meta>')
        opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {chr(10).join("    " + d for d in dc)}
  </metadata>
  <manifest>
    {chr(10).join("    " + m for m in manifest)}
  </manifest>
  <spine>
    {chr(10).join("    " + s for s in spine)}
  </spine>
</package>
"""
        z.writestr("OEBPS/content.opf", opf)

    print(f"EPUB written: {OUT}  ({OUT.stat().st_size/1e6:.1f} MB)")
    print(f"sections: {len(body_docs)}  images: {len(assets)}  toc entries: {len(toc_entries)}")
    if unmatched:
        print(f"unmatched toc entries: {len(unmatched)}")
        for e in unmatched[:20]:
            print("   ", e)


if __name__ == "__main__":
    main()
