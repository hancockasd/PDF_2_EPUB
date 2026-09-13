#!/usr/bin/env python3
"""Validate an EPUB produced by make_epub_layout.py.

Checks the container structure, every manifest/spine/nav/img reference, and
how much of the OCR source text actually made it into the book.

Usage:
    python3 verify_epub.py --config book.json
"""
import collections
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_epub_layout as b  # noqa: E402

NS = {"opf": "http://www.idpf.org/2007/opf"}


def check_structure(path):
    problems = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if names[0] != "mimetype":
            problems.append("mimetype is not the first entry")
        if z.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype is compressed")
        if z.read("mimetype") != b"application/epub+zip":
            problems.append("bad mimetype content")
        for name in names:
            if name.endswith((".xhtml", ".opf", ".xml")):
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as exc:
                    problems.append(f"{name}: XML error {exc}")
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
        manifest = {i.get("id"): i.get("href")
                    for i in opf.iter("{http://www.idpf.org/2007/opf}item")}
        spine = [i.get("idref")
                 for i in opf.iter("{http://www.idpf.org/2007/opf}itemref")]
        for sid in spine:
            if sid not in manifest:
                problems.append(f"spine idref {sid} missing from manifest")
            elif f"OEBPS/{manifest[sid]}" not in names:
                problems.append(f"spine file {manifest[sid]} missing from zip")
        for href in manifest.values():
            if f"OEBPS/{href}" not in names:
                problems.append(f"manifest file {href} missing from zip")
        # every href/src in the XHTML, resolved against its own directory
        for name in names:
            if not name.endswith(".xhtml"):
                continue
            text = z.read(name).decode("utf-8")
            for attr in ("href", "src"):
                for m in re.finditer(rf'{attr}="([^"]+)"', text):
                    ref = m.group(1)
                    if ref.startswith(("http", "mailto", "data:")):
                        continue
                    target = ref.partition("#")[0]
                    if not target:
                        continue
                    resolved = str((Path(name).parent / target).as_posix())
                    resolved = _normpath(resolved)
                    if resolved not in names:
                        problems.append(f"{name}: broken {attr} {ref}")
    return problems, names


def _normpath(path):
    parts = []
    for part in path.split("/"):
        if part == ".." and parts:
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def main():
    path = b.OUT
    if not path.exists():
        raise SystemExit(f"no EPUB at {path}")
    problems, names = check_structure(path)
    print(f"EPUB        : {path} ({path.stat().st_size / 1e6:.1f} MB)")
    print(f"zip entries : {len(names)}")
    print("structure problems:", problems if problems else "none")

    doc_pdf = pymupdf.open(b.PDF)
    pages = {p: b.PageObj(p, doc_pdf) for p in range(1, b.BODY_LAST + 1)}
    located = b.locate_entries(pages, b.parse_toc())
    table_pages = {}
    for label, printed in b.parse_table_toc():
        table_pages.setdefault(printed + b.PAGE_OFFSET, label)
    blocks = b.build_document(pages, located, table_pages)

    def norm(s):
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)

    with zipfile.ZipFile(path) as z:
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
        manifest = {i.get("id"): i.get("href")
                    for i in opf.iter("{http://www.idpf.org/2007/opf}item")}
        spine_files = [manifest[i.get("idref")]
                       for i in opf.iter("{http://www.idpf.org/2007/opf}itemref")]
        epub_text = norm("".join(
            re.sub(r"<[^>]+>", "", z.read("OEBPS/" + f).decode("utf-8"))
            for f in spine_files))

    missing, total_src = [], 0
    for pno, page in pages.items():
        src = norm("".join(r["text"] for r in page.rows))
        total_src += len(src)
        chunks = [src[i:i + 20] for i in range(0, max(0, len(src) - 20), 20)]
        if not chunks:
            continue
        found = sum(1 for c in chunks if c in epub_text)
        if found < len(chunks):
            missing.append((pno, len(chunks) - found, len(chunks)))
    print(f"source chars: {total_src}   epub text chars: {len(epub_text)}")
    print(f"pages with missing 20-char chunks: {len(missing)}")
    for row in missing[:15]:
        print("   page %d: %d/%d chunks missing" % row)

    print("blocks:", dict(collections.Counter(blk["kind"] for blk in blocks)))
    unmatched = [e["title"] for e in located if not e["page"]]
    print(f"TOC entries located: {len(located) - len(unmatched)}/{len(located)}")
    for title in unmatched[:10]:
        print("   unmatched:", title)


if __name__ == "__main__":
    main()
