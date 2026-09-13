# pdf-to-epub

一个用于 Codex / Claude Code 的 Skill：把 **扫描版 PDF 图书**（图片页、无文字层）转换成 EPUB3，方便在 Kindle / Kobo / Apple Books 上阅读。

## 适用场景

- PDF 是扫描件，复制不出文字
- 想要一份可重排、可调字号的电子书，而不是逐页图片
- 中文（简/繁）与英文混排的学术书、史料、专著

## 不适用场景

- PDF 本身有文字层 → 直接用 `pypdf` / `pdftotext` 提取即可，不需要 OCR
- 只想要 PDF 转图片、加水印、合并拆分 → 用 `pdf` skill
- 需要 100% 保留原版式（逐页像素级一致）→ EPUB 是重排格式，做不到；这种需求应该直接看 PDF

## 依赖

- macOS（依赖 Vision 框架做 OCR，仅限 Mac）
- `poppler`（提供 `pdftoppm`）：`brew install poppler`
- Xcode Command Line Tools（提供 `swiftc`）
- Python 3

## 安装

```bash
# Codex
cp -r . ~/.codex/skills/pdf-to-epub

# Claude Code
cp -r . ~/.claude/skills/pdf-to-epub
```

安装后重启会话，Skill 会被自动加载。也可以直接把 `SKILL.md` 的内容贴给任意支持读文件的 agent。

## 工作流程

```
扫描版 PDF
   │  pdftoppm -r 200
   ▼
每页 PNG ──► macOS Vision OCR ──► 每页 txt
                                    │
                                    ▼
                              make_epub.py
                          (去页眉/脚注、识别标题、
                           合并跨页段落、分章)
                                    │
                                    ▼
                                EPUB3
```

详细步骤、正则调整方式和排错表见 [SKILL.md](SKILL.md)。

## 目录结构

| 文件 | 说明 |
|------|------|
| `SKILL.md` | Skill 主文件：触发条件、完整流水线、排错指南 |
| `scripts/ocr_vision.swift` | macOS Vision 封装，中文（简/繁）+ 英文，accuracy 模式 |
| `scripts/make_epub.py` | OCR 文本 → EPUB3 组装脚本 |

## 已知限制

这是一条「纯文本 + 标题结构」路线，优先保证**读得下去**：

- 页眉、印刷页码会被丢弃
- 脚注会被识别并剔除（多种模式的启发式规则，偶有漏判/误判）
- 表格、影印插图**不会被还原**，会作为空白丢失
- OCR 存在少量错字，需要人工校对

如果需要保留插图、表格和脚注，需要在此基础上扩展（把图片区域按坐标裁切后嵌入 XHTML），本仓库不含这部分。

## License

未指定。
