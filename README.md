# pdf-to-epub

把一本（通常是扫描版的）PDF 图书转成 EPUB3。给 Codex / Claude Code 用的 Skill。

```bash
cp -r . ~/.codex/skills/pdf-to-epub      # Codex
cp -r . ~/.claude/skills/pdf-to-epub     # Claude Code
```

重启会话后生效。也可以直接把 `SKILL.md` 丢给任意能读文件的 agent。

## 两条路线

| | 轻量（`make_epub.py`） | 保版（`make_epub_layout.py`） |
|---|---|---|
| 输入 | 每页一段 OCR 纯文本 | 每行文本 + 归一化坐标（JSON） |
| 产出 | 标题 + 正文 | 章/节层级、首行缩进、跨页段落合并、脚注分区、内嵌插图、表格裁图、封面、书名页、版权页 |
| 适合 | 小说、纯文字书 | 学术书、史料、有图表脚注的书 |

保版路线的完整流程：

```
扫描版 PDF
   │  scripts/prepare.sh        渲染 200 dpi 灰度 + macOS Vision OCR
   ▼
pages/NNN.png  +  json/NNN.jsonl（每行文字 + bbox）
   │  make_epub_layout.py --config book.json
   │     目录 → 章/节层级      缩进 → 段落       行距 → 脚注分区
   │     xref → 内嵌插图       坐标 → 影印插页/表格裁图
   ▼
EPUB3  ──  verify_epub.py  ──  结构 / 引用 / 文本覆盖率检查
```

## 依赖

macOS（OCR 走系统 Vision 框架，Linux 需换 OCR 后端）、`poppler`（`pdftoppm`）、Xcode Command Line Tools（`swiftc`）、Python 3 + `pymupdf` + `Pillow`。

```bash
brew install poppler
pip3 install pymupdf pillow
```

## 仓库结构

| 文件 | 说明 |
|---|---|
| `SKILL.md` | Skill 主文件：路线选择、完整步骤、配置字段、排错表 |
| `config.example.json` | 保版路线的配置样例（复制成 `book.json` 改） |
| `scripts/prepare.sh` | 渲染 + 编译 OCR 助手 + 并行 OCR，一条命令 |
| `scripts/ocr_vision_json.swift` | macOS Vision OCR，输出每行文字 + bbox |
| `scripts/ocr_vision.swift` | 同上，但只输出纯文本（轻量路线用） |
| `scripts/make_epub_layout.py` | 保版路线：几何感知的 EPUB3 组装 |
| `scripts/make_epub.py` | 轻量路线：纯文本 + 标题规则的 EPUB3 组装 |
| `scripts/verify_epub.py` | 校验成品：容器结构、所有 href/src、OCR 文本覆盖率 |

## 实测（路线 B）

一本 530 页、纯扫描、带 27 个排版表格和大量影印插页的中文学术书：

| 环节 | 实测 |
|---|---|
| 渲染 200 dpi 灰度 | 530 页，约 1.1 GB |
| Vision OCR（8 并行，M 系列） | 约 80 秒 |
| 组装 | 30 秒级 |
| 成品 | 59 MB、82 个正文文件、233 张图、80 条导航 |

## 已知限制

EPUB 是重排格式，做不到和 PDF 逐页像素一致。

- 页眉、印刷页码一律丢弃
- 排版表格裁成图片保留，不可复制、不可重排
- 影印插页同样以裁图保留，正文里的原图内容无法转文字
- 多栏排版、竖排、古籍需要另调参数
- OCR 有约 1% 的错字（尤其小字号脚注和影印件），建议对照原书抽查
- 没有印刷版目录（或没配 `pages.toc`）时，全书会退化成无章节的结构

## 授权

仅用于处理你本人拥有或有权转换的文件。
