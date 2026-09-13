#!/usr/bin/env bash
# Render a scanned PDF to per-page PNGs and OCR every page to
# geometry-aware JSON (line text + normalized bbox).
#
# Usage: prepare.sh <book.pdf> <workdir> [dpi] [jobs]
#
# Produces:
#   <workdir>/pages/page-NNN.png     one image per page
#   <workdir>/json/NNN.jsonl         one {"text","x","y","w","h","conf"} per line
#
# Re-running is cheap: existing pages are kept, so this is also the way to
# re-render at a different dpi (delete <workdir>/pages first).
set -euo pipefail

PDF=${1:?usage: prepare.sh <book.pdf> <workdir> [dpi] [jobs]}
WORK=${2:?usage: prepare.sh <book.pdf> <workdir> [dpi] [jobs]}
DPI=${3:-200}
JOBS=${4:-8}
HERE=$(cd "$(dirname "$0")" && pwd)

command -v pdftoppm >/dev/null || {
    echo "pdftoppm not found - install poppler (brew install poppler)" >&2
    exit 1
}
command -v swiftc >/dev/null || {
    echo "swiftc not found - install the Xcode command line tools" >&2
    exit 1
}

mkdir -p "$WORK/pages" "$WORK/json" "$WORK/pages_raw"

# macOS Vision needs an uncompressed render; 200 dpi is the accuracy/size
# sweet spot for CJK body text
echo "rendering $PDF at ${DPI} dpi ..."
pdftoppm -r "$DPI" -gray -png "$PDF" "$WORK/pages_raw/page"
for f in "$WORK"/pages_raw/*.png; do
    n=${f##*-}
    n=${n%.png}
    mv "$f" "$WORK/pages/page-$(printf '%03d' "$((10#$n))").png"
done
rmdir "$WORK/pages_raw"
echo "pages: $(ls "$WORK"/pages/*.png | wc -l | tr -d ' ')"

if [ ! -x "$WORK/ocr_vision_json" ]; then
    echo "compiling the Vision OCR helper ..."
    swiftc "$HERE/ocr_vision_json.swift" -o "$WORK/ocr_vision_json"
fi

echo "ocr (${JOBS} workers) ..."
export WORK
export OCR="$WORK/ocr_vision_json"
ls "$WORK"/pages/*.png | sort | xargs -P "$JOBS" -n1 sh -c '
        p="$1"
        n=$(basename "$p" .png)
        n=${n#page-}
        "$OCR" "$p" > "$WORK/json/$n.jsonl"
    ' _
echo "ocr done: $(ls "$WORK"/json/*.jsonl | wc -l | tr -d ' ')"
