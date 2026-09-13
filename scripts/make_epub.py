#!/usr/bin/env python3
"""将 OCR 文本文件组装成 EPUB3。
用法：在脚本顶部设置 TXTDIR / OUT / TITLE / AUTHOR，然后运行。
"""
import os, zipfile, re, textwrap
from pathlib import Path
from html import escape as _he

# ── 用户配置区 ────────────────────────────────────────────────
TXTDIR = Path("/tmp/pdf2epub/texts")                           # OCR 文本目录
OUT    = Path("~/Downloads/向社会主义过渡.epub").expanduser()   # 输出 EPUB 路径
TITLE  = "向社會主義過渡——中國經濟與社會的轉型（1953-1955）"
AUTHOR = "林蘊暉"
# ─────────────────────────────────────────────────────────────

HEADER_RE = re.compile(r'^[\d]+$')           # 独立数字行（页码或脚注编号）
RUNHEAD_RE = re.compile(
    r'^(?:中華人民共和.史.*|[一—-]?向社.{1,8}過.{0,5})$'
)  # 书名页眉（整行匹配，含OCR变体，不误杀正文句子）
# 章标题式页眉：只在头区（前5行）过滤
CHAPTER_RUNHEAD_RE = re.compile(
    r'^[第布].{1,2}[章草早].{2,28}$'      # 奇数页：第X章 章名
    r'|^(?:[^\d一二三四五六七八九十])'      # 偶数页：不以序数/数字开头
    r'[一-鿿「」（）、—\-·]{6,22}$'        # 纯CJK短行（章名简称式页眉）
)
NOISE_RE = re.compile(r'^[§！!i\s]+$')       # 扫描噪点行

# 书目关键词：只有含这些词的行才可能是脚注
_BIB_KW = re.compile(
    r'出版社|'
    r'轉引自|'
    r'[載载][^，。]{0,10}[文室編編]|'   # 载…文/载…室（引用来源行）
    r'頁\d|'
    r'[（(]?[\d一二三四五六七八九十百]{4}年[）)]?[，、\d）]?|'
    r'第\d+[卷册期號]|'
    r'〉（|'                             # 〉（= 书名号+括号，脚注特征
    r'》[，。）\s]|'
    r'[:：]\s*[《〈]'
)
# 脚注行：数字开头+书目关键词，或黑点，或转引自
FOOTNOTE_LINE_RE = re.compile(
    r'^\d{1,3}\s+.{0,30}(?:出版社|頁\d|轉引自|》|：《)|'  # "41 毛澤東：《..." 含书目词
    r'（[\d一二三四五六七八九十百]{4}年|'  # （1953年...
    r'頁\d|'
    r'出版社|'
    r'轉引自|'
    r'^[•·]\s*.{4}|'                 # "• 薄一波：..." 黑点脚注
    r'《[^，。；：]{2,}'              # 《书名（不要求闭合》）
)

# 标题识别
CHAPTER_RE = re.compile(r'^第[一二三四五六七八九十百\d]+章')
SECTION_RE = re.compile(
    r'^[一二三四五六七八九十]{1,2}[、\s　]|'  # 一、最初設想... 或 一 最初設想
    r'^[一二三四五六七八九十]{1,2}(?=[^\d，。；：、])'  # 一最初設想... (直接跟汉字)
)
SUBSECTION_RE = re.compile(r'^\d+\s+[一-鿿]')  # 1 新民主主義...
TITLE_LINE_RE = re.compile(r'^[一-鿿（）「」—\-\d·•\s]{4,20}$')  # 短独立标题行

def is_footnote_line(line: str) -> bool:
    return bool(FOOTNOTE_LINE_RE.search(line))

def clean_text(txt: str) -> str:
    """去除页眉（页码+书名栏）和脚注区域，返回纯正文。"""
    lines = txt.splitlines()

    # 1. 去掉开头区域（前5行）中的页眉行和孤立页码
    #    这类页面常见格式：第一行是章标题页眉，第二行是页码，第三行才是正文
    #    用过滤而非截断，以免误删标题行
    head_zone = min(5, len(lines))
    cleaned_head = []
    for l in lines[:head_zone]:
        s = l.strip()
        if RUNHEAD_RE.match(s) or CHAPTER_RUNHEAD_RE.match(s) or NOISE_RE.match(s):
            continue  # 书名页眉 / 章标题式页眉 / 噪点 → 删除
        if HEADER_RE.match(s):
            continue  # 孤立页码 → 删除
        cleaned_head.append(l)
    lines = cleaned_head + lines[head_zone:]

    # 2. 去掉正文内部残留的书名页眉行和噪点行（不删章标题行，只删书名横栏）
    lines = [l for l in lines if not RUNHEAD_RE.match(l.strip()) and not NOISE_RE.match(l.strip())]

    # 脚注起始行特征：数字（含 "1.8" 形式）+ 书目内容
    FN_START_RE = re.compile(r'^\d+[.\s]\d*\s*[《\s]|^\d{1,3}\s+\S')

    def _is_inline_footnote(s: str) -> bool:
        """数字开头且含书目关键词 → 脚注；纯标题文字 → 不是。"""
        return bool(FN_START_RE.match(s) and _BIB_KW.search(s))

    # 3. 找脚注起始点：
    #    a) 独立数字行（脚注编号单独成行）
    #    b) 行以"数字+书目关键词"开头（内联脚注第一行）
    #    c) 行末含脚注编号且下一行是书目行
    #    d) 行以 • 或 · 开头（黑点脚注标记）
    #    e) 行以〈《「载 开头且含书目关键词（书名引用型脚注第一行）
    footnote_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i < 2:
            continue
        # 情况 a：独立数字行
        if HEADER_RE.match(stripped):
            footnote_start = i
            break
        # 情况 b：数字+书目关键词
        if _is_inline_footnote(stripped):
            footnote_start = i
            break
        # 情况 c：行末脚注编号，且下一行是书目行
        if re.search(r'\d{1,3}[a-zA-Z]?$', stripped):
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if is_footnote_line(next_line):
                lines[i] = re.sub(r'\s*\d{1,3}[a-zA-Z]?$', '', line)
                footnote_start = i + 1
                break
        # 情况 d：黑点脚注
        if re.match(r'^[•·]', stripped) and is_footnote_line(stripped):
            footnote_start = i
            break
        # 情况 e：以〈《开头的书名引用行（页面后40%才触发，避免误杀正文引用）
        if (i > len(lines) * 0.6
                and re.match(r'^[〈《「載载]', stripped)
                and _BIB_KW.search(stripped)):
            footnote_start = i
            break

    if footnote_start is not None:
        lines = lines[:footnote_start]

    return "\n".join(lines)

def classify_line(line: str) -> str:
    """返回 'h1' / 'h2' / 'h3' / 'p' 表示行的语义类型。"""
    s = line.strip()
    if CHAPTER_RE.match(s):
        return 'h1'
    if SECTION_RE.match(s) and not re.search(r'[，。]', s[2:]):
        return 'h2'
    if SUBSECTION_RE.match(s):
        # 数字开头：若含书目关键词或行过长（>28字，非正常小节标题）则是脚注
        if _BIB_KW.search(s) or len(s) > 28:
            return 'fn'
        return 'h3'
    # 短独立标题行（章副标题）：4-20字，纯CJK/符号，无句末标点，无年月日，非机构落款
    if (TITLE_LINE_RE.match(s)
            and not re.search(r'[，。；？！]', s)
            and not re.search(r'\d{4}年\d+月\d+日|\d{4}年\d+月', s)
            and not re.search(r'工作部$|委員會$|辦公室$|研究室$|出版社$', s)):
        return 'h2'
    return 'p'

def page_to_segments(txt: str) -> list:
    """将单页 OCR 文本清洗后转换为 (tag, text) 列表。"""
    txt = clean_text(txt)
    segments = []
    current_p = []

    for line in txt.splitlines():
        line_s = line.strip()
        if not line_s:
            if current_p:
                segments.append(('p', "".join(current_p)))
                current_p = []
            continue
        if NOISE_RE.match(line_s):
            continue
        tag = classify_line(line_s)
        if tag == 'fn':
            continue  # 脚注行，丢弃
        if tag != 'p':
            if current_p:
                segments.append(('p', "".join(current_p)))
                current_p = []
            segments.append((tag, line_s))
        else:
            current_p.append(line_s)

    if current_p:
        segments.append(('p', "".join(current_p)))

    return [(tag, text) for tag, text in segments if text.strip()]


def _para_is_incomplete(text: str) -> bool:
    """正文段落末尾不是句末标点，说明是跨页截断的段落。"""
    return bool(text) and text[-1] not in '。！？」』…'


def merge_section_segments(all_page_segs: list) -> list:
    """将多页 segments 合并，跨页截断的 <p> 首尾相接。"""
    merged = []
    for segs in all_page_segs:
        for tag, text in segs:
            if (tag == 'p' and merged and merged[-1][0] == 'p'
                    and _para_is_incomplete(merged[-1][1])):
                # 上一段末尾截断 → 拼接到同一段落
                merged[-1] = ('p', merged[-1][1] + text)
            else:
                merged.append([tag, text])
    return [(tag, text) for tag, text in merged]


def segments_to_body(segments: list) -> str:
    return "\n".join(f"<{tag}>{_he(text)}</{tag}>" for tag, text in segments)


def make_xhtml(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-Hans">
<head>
  <meta charset="utf-8"/>
  <title>{_he(title)}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
{body}
</body>
</html>"""


# 收集文本文件并排序
txt_files = sorted(TXTDIR.glob("*.txt"), key=lambda p: int(p.stem))

pages = []
for f in txt_files:
    txt = f.read_text(encoding="utf-8").strip()
    if txt:
        pages.append((int(f.stem), txt))

# 将各页 segments 预先计算出来
page_segments = [(pn, page_to_segments(txt)) for pn, txt in pages]

# 合并连续页：只在下一页以 h1/h2 开头时开启新 xhtml 文件
# 同时：若本页最后一个 segment 是 h1/h2，也开启新文件（章末自然断开）
sections = []   # list of (first_page_num, title_str, [segments...])
cur_page_segs = []
cur_first = None
cur_title = None

for i, (pn, segs) in enumerate(page_segments):
    if not segs:
        continue

    # 判断是否应该在此页开始新 section
    starts_new = False
    if cur_first is None:
        starts_new = True
    elif segs and segs[0][0] in ('h1', 'h2'):
        # 下一页以标题开头 → 自然节首
        starts_new = True
    elif cur_page_segs and cur_page_segs[-1] and cur_page_segs[-1][-1][0] in ('h1', 'h2'):
        # 上一页以标题结尾（章末/节末）→ 自然断开
        starts_new = True

    if starts_new and cur_first is not None:
        sections.append((cur_first, cur_title, merge_section_segments(cur_page_segs)))
        cur_page_segs = []

    if starts_new:
        cur_first = pn
        cur_title = segs[0][1] if segs[0][0] in ('h1', 'h2', 'h3') else f"第 {pn} 页"

    cur_page_segs.append(segs)

if cur_page_segs:
    sections.append((cur_first, cur_title, merge_section_segments(cur_page_segs)))

# 构建 EPUB zip
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:

    # mimetype（必须无压缩、第一个文件）
    z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
               compress_type=zipfile.ZIP_STORED)

    # container.xml
    z.writestr("META-INF/container.xml", textwrap.dedent("""\
        <?xml version="1.0"?>
        <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles>
            <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
          </rootfiles>
        </container>"""))

    # CSS
    z.writestr("OEBPS/styles/style.css", textwrap.dedent("""\
        body { font-family: serif; font-size: 1em; line-height: 1.8;
               margin: 1.5em; color: #1a1a1a; }
        p    { text-indent: 2em; margin: 0.4em 0; }
        h1   { font-size: 1.4em; font-weight: bold; text-align: center;
               margin: 1.2em 0 0.4em; text-indent: 0; }
        h2   { font-size: 1.15em; font-weight: bold; margin: 1em 0 0.3em;
               text-indent: 0; }
        h3   { font-size: 1em; font-weight: bold; margin: 0.8em 0 0.2em;
               text-indent: 0; }
        .page-num { text-align: center; color: #888; font-size: 0.8em;
                    margin-bottom: 1em; }"""))

    # 每个 section → 一个 xhtml 文件
    manifest_items = []
    spine_items = []
    for first_pn, title, segs in sections:
        mid = f"s{first_pn:04d}"
        fname = f"OEBPS/text/{mid}.xhtml"
        body = segments_to_body(segs)
        z.writestr(fname, make_xhtml(title, body))
        manifest_items.append(
            f'<item id="{mid}" href="text/{mid}.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{mid}"/>')

    # content.opf
    manifest_str = "\n    ".join(manifest_items)
    spine_str = "\n    ".join(spine_items)
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{TITLE}</dc:title>
    <dc:creator>{AUTHOR}</dc:creator>
    <dc:language>zh-Hans</dc:language>
    <dc:identifier id="uid">urn:isbn:prc-preview</dc:identifier>
  </metadata>
  <manifest>
    <item id="css" href="styles/style.css" media-type="text/css"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    {manifest_str}
  </manifest>
  <spine>
    {spine_str}
  </spine>
</package>"""
    z.writestr("OEBPS/content.opf", opf)

    # nav.xhtml — 每个 section 一条目录项
    nav_items = "\n".join(
        f'<li><a href="text/s{pn:04d}.xhtml">{_he(title)}</a></li>'
        for pn, title, _ in sections)
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-Hans">
<head><meta charset="utf-8"/><title>目录</title></head>
<body>
  <nav epub:type="toc">
    <h1>目录</h1>
    <ol>{nav_items}</ol>
  </nav>
</body>
</html>"""
    z.writestr("OEBPS/nav.xhtml", nav)

print(f"EPUB 已生成：{OUT}")
print(f"共 {len(pages)} 页，合并为 {len(sections)} 个章节文件")
