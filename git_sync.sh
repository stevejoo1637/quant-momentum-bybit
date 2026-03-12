#!/bin/bash
cd /root/bybit_strategy
while true; do
    inotifywait -e modify,create,delete,move -r /root/bybit_strategy --exclude '(\.git|state\.json|\.log|\.pyc|__pycache__)' 2>/dev/null
    sleep 2
    git add -A
    STAGED=$(git status --porcelain | grep -c '^[MADRC]' || true)
    if [ "$STAGED" = "0" ]; then continue; fi
    git commit -m "auto: $(date '+%Y-%m-%d %H:%M:%S')"
    git push origin main
done
