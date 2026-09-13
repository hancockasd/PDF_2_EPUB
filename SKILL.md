---
name: pdf-to-epub
description: Use this skill whenever the user wants to convert a PDF book (especially a scanned/image-based PDF) into EPUB format for reading on an e-reader. Triggers on phrases like "把PDF转成epub"、"pdf转电子书"、"扫描版PDF转换"、"convert PDF to EPUB", or any request to make a PDF readable on Kindle/Kobo/Apple Books. Use this skill even if the user just says "帮我转换这本书" if a PDF is mentioned nearby.
---

# PDF → EPUB 转换

本 Skill 覆盖完整的流水线：扫描版 PDF（图片页）→ macOS Vision OCR → EPUB3。

## 快速判断：是否需要 OCR？

```bash
# 检查 PDF 是否有文字层（有输出 = 有文本层，可直接用 pypdf 提取）
pdftotext -f 1 -l 3 "$PDF" /tmp/test_extract.txt && wc -c /tmp/test_extract.txt
```

- **输出 > 200 字节**：有文本层，直接用 pypdf 提取，跳过 OCR 步骤
- **输出 ≈ 0**：扫描版，走下面的完整流水线

---

## 完整流水线（扫描版 PDF）

### 第一步：准备工作目录

```bash
PDF="/path/to/book.pdf"          # 输入 PDF
WORKDIR="/tmp/pdf2epub"
mkdir -p "$WORKDIR/pages" "$WORKDIR/texts"
```

### 第二步：PDF → PNG（每页）

```bash
# 200dpi 足够 OCR 精度，文件不会太大
pdftoppm -r 200 -png "$PDF" "$WORKDIR/pages/page"
# 产出：pages/page-001.png, page-002.png, ...
```

检查页数：`ls $WORKDIR/pages/*.png | wc -l`

### 第三步：编译 OCR 二进制（只需一次）

```bash
swiftc /Users/I572226/.claude/skills/pdf-to-epub/scripts/ocr_vision.swift \
    -o "$WORKDIR/ocr_vision"
```

这是 macOS Vision 框架的封装，支持简体/繁体中文 + 英文，accuracy 模式。  
**编译后的二进制可在同一台 Mac 上重复使用**——如果 `$WORKDIR/ocr_vision` 已存在就跳过。

### 第四步：并行 OCR

```bash
# 生成 OCR 脚本（8 并行，适合 M 系列芯片）
ls "$WORKDIR/pages"/*.png | sort | \
  awk -v wd="$WORKDIR" '{
    n = $0; gsub(".*/page-", "", n); gsub(".png", "", n);
    print wd "/ocr_vision " $0 " > " wd "/texts/" n ".txt"
  }' | xargs -P 8 -I{} bash -c "{}"
```

完成后检查：`ls $WORKDIR/texts/ | wc -l`（应等于 PNG 数量）

### 第五步：组装 EPUB

编辑 `scripts/make_epub.py` 顶部的四个变量，然后运行：

```python
TXTDIR = Path("/tmp/pdf2epub/texts")   # OCR 文本目录
OUT    = Path("~/Downloads/书名.epub") # 输出路径
TITLE  = "书名"
AUTHOR = "作者"
```

```bash
python3 /Users/I572226/.claude/skills/pdf-to-epub/scripts/make_epub.py
```

---

## make_epub.py 的处理逻辑

脚本会自动完成以下处理，**无需手动调整**（对典型中文学术书）：

### 页眉/噪点去除
- 开头若干行中的孤立数字（页码）、书名横栏、`§！` 等扫描噪点行

### 脚注识别与删除（四种模式）
| 模式 | 示例 | 检测方式 |
|------|------|---------|
| 独立数字行 | `36\n毛澤東...` | 行是纯数字，且下面接书目内容 |
| 内联脚注首行 | `41 毛澤東：《...》` | `^\d{1,3}\s+` + 书目关键词 |
| 行末脚注编号 | `分工包乾41\n毛澤東...` | 行尾 `\d+[a-zA-Z]?` + 下一行是书目 |
| 黑点标记 | `• 薄一波：《...》` | `^[•·]` + 书目关键词 |

书目关键词包含：`出版社`、`頁\d`、`轉引自`、`（19xx年`、`《书名` 等。

### 标题识别
| 级别 | 示例 | 规则 |
|------|------|------|
| `<h1>` | `第一章` | `^第[一-十百\d]+章$` |
| `<h2>` | `一最初設想：...` | 开头一个中文数字 + 非数字非句末标点 |
| `<h2>` | `向社會主義過渡的總路線` | 4-20字短行，纯CJK，无句末标点 |
| `<h3>` | `1 新民主主義的建國網領` | `^\d+\s+[CJK]` |

### 跨页段落合并
同一节内，若某页最后一段末尾不是句末标点（`。！？」』…`），则与下一页开头段落拼接为同一 `<p>`，消除 PDF 分页造成的读者视觉断裂。

### 章节文件划分
- 新 `<h1>/<h2>` 开头 → 开启新 xhtml 文件（EPUB 的一个"章"）
- 段落在同一章节内流动，不按 PDF 页码分割

---

## 针对不同书籍的调整

如果书中脚注或页眉格式与默认规则不符，修改 `make_epub.py` 顶部的正则即可：

```python
RUNHEAD_RE   # 书名页眉匹配（当前：r'中華人民共和.史'）
FOOTNOTE_LINE_RE  # 脚注行特征（可添加该书特有的出版社名等）
```

先跑10页测试（`TXTDIR` 只指向少量 txt 文件），确认效果后再全量运行。

---

## 典型问题排查

| 症状 | 原因 | 处理 |
|------|------|------|
| 脚注仍残留 | 脚注格式未覆盖 | 看该页的 `.txt` 文件，在 `FOOTNOTE_LINE_RE` 加规则 |
| 正文被误删 | 脚注误判触发过早 | 降低 `i >= 2` 的阈值，或缩窄某条正则 |
| 短正文行变成标题 | `TITLE_LINE_RE` 误匹配 | 给 `classify_line` 加排除条件 |
| OCR 漏字/乱码 | 图像分辨率不足 | 把 `pdftoppm -r 200` 改为 `-r 300` |
| 中文繁简混排 | Vision 默认 zh-Hans | 在 swift 中加 `"zh-Hant"` 到语言列表 |

---

## 分阶段运行建议

1. **先转 10 页测试**：把 `TXTDIR` 指向只含几个 txt 的目录，生成预览 EPUB，在阅读器中核对
2. **确认效果后全量**：对全书跑 OCR（大书可能需要数小时），再运行 `make_epub.py`
3. **保留 texts/ 目录**：OCR 结果文本是中间产物，出现问题可直接修改 txt 重新组装，不用重跑 OCR
