#!/bin/sh
# Starts KOReader from NickelMenu. When KOReader hands back to the Kobo interface, books that arrived
# meanwhile (Syncthing, Kobo Bridge, Find a book) are imported into My Books automatically via NickelDBus.
# Usage: launch.sh [claude]   ("claude" opens straight on the Claude home screen)

MARK=/tmp/askclaude-books-mark
LOG=/tmp/askclaude-launch.log

touch "$MARK"
[ "$1" = "claude" ] && touch /tmp/askclaude-home

/mnt/onboard/.adds/koreader/koreader.sh

# koreader.sh has started the Kobo interface again in the background. Only import if a book changed.
new_books=$(find /mnt/onboard/Books -type f -newer "$MARK" \( -name '*.epub' -o -name '*.pdf' -o -name '*.cbz' \
    -o -name '*.cbr' -o -name '*.mobi' -o -name '*.txt' -o -name '*.html' -o -name '*.rtf' \) 2>/dev/null | wc -l)
rm -f "$MARK"
echo "$(date) KOReader closed, $new_books new or updated book(s)" >> "$LOG"
[ "$new_books" -gt 0 ] || exit 0
command -v qndb >/dev/null 2>&1 || { echo "qndb missing, skipping import" >> "$LOG"; exit 0; }

# Wait (up to 90 s) until the Kobo interface and NickelDBus answer, then let it settle.
i=0
until qndb -m ndbVersion >/dev/null 2>&1; do
    i=$((i + 1))
    [ $i -ge 90 ] && { echo "NickelDBus never answered" >> "$LOG"; exit 0; }
    sleep 1
done
sleep 5

if qndb -t 120000 -s pfmDoneProcessing -m pfmRescanBooksFull >> "$LOG" 2>&1; then
    qndb -m mwcToast 3000 "New books added to My Books" >/dev/null 2>&1
fi
echo "$(date) import done" >> "$LOG"
