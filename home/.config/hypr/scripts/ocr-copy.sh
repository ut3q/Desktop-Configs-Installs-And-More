#!/bin/sh
# Select a region, OCR it, put the text on the clipboard.
# Bound to Super+Shift+T. Uses grim+slurp rather than Caelestia's picker
# because that picker only knows two outcomes: save a PNG, or copy a PNG.
set -eu

command -v tesseract >/dev/null 2>&1 || {
    notify-send -a screenshot-ocr -u critical "OCR unavailable" \
        "tesseract is not installed:  sudo pacman -S tesseract tesseract-data-eng"
    exit 1
}

geom=$(slurp -d) || exit 0          # empty on Esc: cancelled, not an error
img=$(mktemp --suffix=.png) || exit 1
txt=$(mktemp) || exit 1
trap 'rm -f "$img" "$txt" "$txt.txt"' EXIT

grim -g "$geom" "$img" || {
    notify-send -a screenshot-ocr -u critical "OCR failed" "could not capture the region"
    exit 1
}

# Upscale and grayscale before OCR: tesseract is markedly more accurate on
# small on-screen text when it is not working at native UI scale.
if command -v magick >/dev/null 2>&1; then
    magick "$img" -colorspace Gray -resize 300% -sharpen 0x1 "$img" 2>/dev/null || true
fi

tesseract "$img" "$txt" --psm 6 2>/dev/null || {
    notify-send -a screenshot-ocr -u critical "OCR failed" "tesseract could not read that region"
    exit 1
}

# Trim trailing blank lines tesseract likes to add, keep internal layout.
text=$(sed -e 's/[[:space:]]*$//' "$txt.txt" | awk 'BEGIN{b=0} {if($0=="")b++; else{while(b--)print "";b=0;print}}')

if [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ]; then
    notify-send -a screenshot-ocr "No text found" "Nothing readable in that region"
    exit 0
fi

printf '%s' "$text" | wl-copy
chars=$(printf '%s' "$text" | wc -m)
lines=$(printf '%s\n' "$text" | wc -l)
notify-send -a screenshot-ocr "Text copied" "$chars characters, $lines line(s)"
