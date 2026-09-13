---
name: pdf-to-epub
description: Use this skill whenever the user wants to convert a PDF book (especially a scanned/image-based PDF) into EPUB format for reading on an e-reader. Triggers on phrases like "把PDF转成epub"、"pdf转电子书"、"扫描版PDF转换"、"convert PDF to EPUB", or any request to make a PDF readable on Kindle/Kobo/Apple Books. Use this skill even if the user just says "帮我转换这本书" if a PDF is mentioned nearby.
---

# PDF → EPUB 转换

把一本 PDF 图书转成 EPUB3。有两条路线，**先选路线再动手**：

| | 路线 A：轻量 | 路线 B：保版 |
|---|---|---|
| 脚本 | `scripts/make_epub.py` | `scripts/make_epub_layout.py` |
| 输入 | 每页一段 OCR 纯文本 | 每行文本 + 坐标（JSON） |
| 产出 | 标题 + 正文段落 | 章/节层级、首行缩进、跨页段落合并、脚注分区、内嵌插图、表格裁图、封面、书名页、版权页 |
| 适合 | 小说、纯文字书、不在乎图表 | 学术书、史料、有插图/表格/脚注的书 |
| 成本 | 渲染 + OCR + 一条命令 | 多一个 `book.json`，通常要调 1–2 轮 |

用户说"保持原排版"“别丢图表”时走路线 B。用户只想读文字时路线 A 就够了。

---

## 第 0 步：这个 PDF 到底需不需要 OCR？

```bash
pdftotext -f 1 -l 3 "$PDF" /tmp/probe.txt && wc -c < /tmp/probe.txt
```

- **输出 > 200 字节**：PDF 有文字层 → 直接用 `pypdf` / `pdftotext` 提取并自己拼 XHTML，两条路线都不需要，OCR 只会更差
- **输出 ≈ 0**：扫描版 → 继续往下

顺便记下页数（`pdfinfo` 或 `ls pages/*.png | wc -l`）和页尺寸，后面配置要用。

---

## 路线 B：保版

### 依赖

- **macOS**（OCR 走系统 Vision 框架，Linux 上要换成 tesseract/PaddleOCR）
- `poppler`（`pdftoppm`）：`brew install poppler`
- Xcode Command Line Tools（`swiftc`）
- Python 3 + `pymupdf`、`Pillow`

### 第 1 步：渲染 + OCR

```bash
~/.codex/skills/pdf-to-epub/scripts/prepare.sh book.pdf /path/to/work 200 8
#                                              ↑PDF      ↑工作目录   ↑dpi ↑并行度
```

产出：

```
/path/to/work/pages/page-001.png    每页一张灰度图
/path/to/work/json/001.jsonl        每行一个 {"text","x","y","w","h","conf"}
```

坐标是 0–1 归一化的，**原点在左上角**，这是后面所有排版判断的基础。200 dpi 对中文正文是精度/体积的平衡点（300 dpi 更慢更大，识别率提升有限）。

### 第 2 步：写 `book.json`

复制 `config.example.json` 改。唯一必须正确填的是**页码**——算法靠它区分封面、目录、正文：

```json
"pages": {
  "total": 530,          // 总页数
  "cover": 1,            // 封面（放封面图，不参与正文）
  "blurb": 2,            // 内容简介页，可省
  "cip": 4,              // 版权/CIP 页，可省
  "toc": [5, 6, 7, 8],   // 印刷版目录页（决定全书的章/节结构）
  "table_toc": [9, 10],  // 印刷版表目录，可省
  "body_first": 11,
  "body_last": 528,      // 有重复扫描页时用它截断
  "printed_page_offset": 10   // PDF 页 = 印刷页 + 10，表目录定位要用
}
```

怎么找这些页码：先渲染前 20 页看一眼（`pdftoppm -r 80 -png -f 1 -l 20 book.pdf /tmp/peek/p`），或者直接看 JSON 里 top 0.14 区域出现的行——页眉/目录标题很显眼。

`toc` 页是最关键的一项：**没有它，全书会退化成一整块无章节文本**（脚本会退化成每 24 段切一个文件，能读但没导航）。多给一页没关系，少了会丢章节。

### 第 3 步：生成 EPUB

```bash
python3 ~/.codex/skills/pdf-to-epub/scripts/make_epub_layout.py --config book.json
```

也可以把配置写到 `book.json` 放在当前目录，或设 `PDF2EPUB_CONFIG=/path/book.json`，然后省掉 `--config`。

### 第 4 步：校验

```bash
python3 ~/.codex/skills/pdf-to-epub/scripts/verify_epub.py --config book.json
```

看四个数：

| 输出 | 期望 | 说明 |
|---|---|---|
| `structure problems` | `none` | 容器结构、manifest/spine、所有 href/src 引用 |
| `TOC entries located` | 接近 `n/n` | 没命中的目录条目 = 丢的章节标题 |
| `pages with missing 20-char chunks` | 0 或个位数 | 正文有没有被误删 |
| `epub text chars` vs `source chars` | 略大于源 | 小很多说明段落被吞了 |

### 配置字段

| 字段 | 默认 | 作用 |
|---|---|---|
| `pdf` / `out` | 必填 | 输入 PDF、输出 EPUB |
| `work` | `.` | 含 `pages/`、`json/` 的工作目录 |
| `scratch` | `/tmp/pdf2epub_build` | 裁图缓存，可删 |
| `metadata.*` | 从文件名猜 | 书名、作者、出版社、日期、ISBN、语言 |
| `running_head` | 自动识别 | 页眉文字。留空=自动找"在多页顶部反复出现的短字符串"；给的字符串按**子串**匹配 |
| `corrections` | 空 | OCR 系统性错字的替换表 `[["士改","土改"], ...]`，跑完看几页再加 |
| `list_sections` | 参考文献/图片来源/附录/索引/书目 | 这些章节是"一行一条"，不做段落合并 |

### 算法在做什么

想调参之前先理解这几步，它们决定了输出长相：

1. **页眉/页码**：y < 0.135 的行，命中页眉词或形如 `第一章…42` 的整行，删掉
2. **正文列**：找包含最多 OCR 行的 x 区间作为正文列，落在列外的碎片（照片边缘）丢弃
3. **段落**：`x` 比正文左边距缩进 > 0.022 的行视为新段开头（中文首行缩进两字）；否则并到上一行
4. **跨页合并**：本页最后一段没以 `。！？」』` 结尾，且下一页开头不是标题/缩进行 → 拼成同一段
5. **脚注**：底部出现"行距 ≥ 1.9 倍常规"且行首是 `①②…` 或 `12 参见/见/《` 的块 → 整块移入页末脚注区
6. **章/节**：拿印刷目录的条目在正文里按阅读顺序模糊匹配（`difflib` ≥ 0.6），命中行升级为 `<h1>`/`<h2>`，并据此切分 XHTML 文件
7. **表格**：表目录指到的页，或**页面首行是 `表N-M`/`续表`** 的页 → 整块裁成图片，图注保留为文字
8. **插图**：PDF 里内嵌的图片按 xref 原样抽出；影印插页/多栏版面按坐标从页面图裁切；反相（负片）页裁切前先翻转回来

### 排错

| 症状 | 原因 | 处理 |
|---|---|---|
| 章标题没出现在导航里 | 目录页页码填错，或目录是竖排/图片 | 检查 `pages.toc`；`verify_epub.py` 会列出未命中的条目 |
| 正文缺了一大段 | 被当成页眉删了 | 看 `running_head` 是否命中正文；填具体页眉词而不是留空 |
| 段落被切得很碎 | 缩进阈值不匹配（书籍左边距不同） | 调 `Doc.add_row` 里的 `0.022` |
| 脚注混在正文里 | 脚注块间距不够大 | 调 `split_notes` 里的 `max(0.045, 1.9 * med)` |
| 表格变成乱码文字 | 表格页没被识别 | 在 `table_toc` 里补页码 |
| 插图丢失 | 图是全页扫描（被当作整页图跳过） | 正常行为：全页图不重复插入，封面除外 |
| 图片是黑底白字 | 该页是负片扫描 | 已自动处理；若判断失败，改 `export_assets` 里的 `mean < 128` |
| OCR 漏字错字 | 分辨率或字号 | 换 300 dpi 重跑 `prepare.sh`（先删 `pages/`） |

### 分阶段做法（重要）

1. 先 `prepare.sh` 跑通 → 确认 `json/` 页数对得上
2. 写 `book.json` → 跑 `make_epub_layout.py` → 在阅读器里翻**目录页、正文首页、有表/图的页、最后一章**
3. 有问题只改配置重跑（30 秒级），不要重跑 OCR
4. OCR 结果 `json/` 是中间产物，**留着**：改错字、改参数都靠它

---

## 路线 A：轻量

不需要坐标，也不需要 `book.json`。适合纯文字书，或者只想快速拿到可读文本。

```bash
PDF="/path/to/book.pdf"
WORKDIR="/tmp/pdf2epub"
mkdir -p "$WORKDIR/pages" "$WORKDIR/texts"

# 1. 渲染
pdftoppm -r 200 -png "$PDF" "$WORKDIR/pages/page"

# 2. 编译 OCR 助手（同一台 Mac 只需一次）
swiftc ~/.codex/skills/pdf-to-epub/scripts/ocr_vision.swift -o "$WORKDIR/ocr_vision"

# 3. 并行 OCR 成纯文本
ls "$WORKDIR/pages"/*.png | sort | \
  awk -v wd="$WORKDIR" '{
    n = $0; gsub(".*/page-", "", n); gsub(".png", "", n);
    print wd "/ocr_vision " $0 " > " wd "/texts/" n ".txt"
  }' | xargs -P 8 -I{} bash -c "{}"

# 4. 组装：编辑 make_epub.py 顶部的 TXTDIR / OUT / TITLE / AUTHOR 后运行
python3 ~/.codex/skills/pdf-to-epub/scripts/make_epub.py
```

`make_epub.py` 会自动做这些事（对典型中文学术书）：

**页眉/噪点去除** —— 开头若干行中的孤立数字（页码）、书名横栏、`§！` 等扫描噪点行

**脚注识别与删除**（四种模式）

| 模式 | 示例 | 检测方式 |
|------|------|---------|
| 独立数字行 | `36\n毛澤東...` | 行是纯数字，且下面接书目内容 |
| 内联脚注首行 | `41 毛澤東：《...》` | `^\d{1,3}\s+` + 书目关键词 |
| 行末脚注编号 | `分工包乾41\n毛澤東...` | 行尾 `\d+[a-zA-Z]?` + 下一行是书目 |
| 黑点标记 | `• 薄一波：《...》` | `^[•·]` + 书目关键词 |

书目关键词包含：`出版社`、`頁\d`、`轉引自`、`（19xx年`、`《书名` 等。

**标题识别**

| 级别 | 示例 | 规则 |
|------|------|------|
| `<h1>` | `第一章` | `^第[一-十百\d]+章$` |
| `<h2>` | `一最初設想：...` | 开头一个中文数字 + 非数字非句末标点 |
| `<h2>` | `向社會主義過渡的總路線` | 4-20字短行，纯CJK，无句末标点 |
| `<h3>` | `1 新民主主義的建國網領` | `^\d+\s+[CJK]` |

**跨页段落合并** —— 同一节内，若某页最后一段末尾不是句末标点（`。！？」』…`），则与下一页开头段落拼接为同一 `<p>`，消除 PDF 分页造成的读者视觉断裂。

**章节文件划分** —— 新 `<h1>/<h2>` 开头 → 开启新 xhtml 文件；段落在同一章节内流动，不按 PDF 页码分割。

### 路线 A 的调整

脚注或页眉格式与默认规则不符时，改 `make_epub.py` 顶部的正则：

```python
RUNHEAD_RE        # 书名页眉匹配
FOOTNOTE_LINE_RE  # 脚注行特征（可加该书特有的出版社名）
```

先跑 10 页测试（`TXTDIR` 只指向少量 txt），确认效果后再全量。

---

## 通用注意

- **扫描件的质量决定上限**：OCR 出来的错字（士/土、己/已、儿/几）是系统性的，集中在古籍、影印本。跑完抽几页对照原书，把高频错字写进 `corrections`
- **EPUB 是重排格式**：做不到和 PDF 逐页像素一致。要像素级保真就别转 EPUB
- **页眉、页码、书眉线**一律丢弃——它们在重排后没有意义
- **版权**：转换只应针对用户自己拥有/有权处理的书

## 已验证的参数

一本 530 页、纯扫描、带 27 个排版表格和大量影印插页的中文学术书（2009 年，江西人民出版社）：

| 环节 | 实测 |
|---|---|
| 渲染 200 dpi 灰度 | 530 页，1.1 GB |
| Vision OCR（8 并行，M 系列） | ~80 秒 |
| 组装 | 30 秒级 |
| 成品 | 59 MB、82 个正文文件、233 张图、80 条导航 |

可以拿这几个数当量级参考来估算自己的书。
