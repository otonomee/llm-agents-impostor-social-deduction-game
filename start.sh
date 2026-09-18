#!/usr/bin/env bash
# One command for a game run:
#   1. OpenCode on :4096 in the background, started from players/ so it loads the no-tools player agent
#   2. the tool-blocking check
#   3. the game
# OpenCode stops when the game ends or you press Ctrl+C.
#
#   ./start.sh                    # one game on DeepSeek
#   ./start.sh --games 5          # anything after start.sh is passed to game.py
#   ./start.sh --reveal           # spectator mode
#   MODEL=openrouter/deepseek/deepseek-v3.2 ./start.sh      # any other OpenCode model

MODEL="${MODEL:-openrouter/deepseek/deepseek-v4-flash-0731}"
cd "$(dirname "$0")"
mkdir -p data

echo "== OpenCode =="
lsof -ti tcp:4096 | xargs kill -9 2>/dev/null
(cd players && exec opencode serve --port 4096) > data/opencode.log 2>&1 &
OPENCODE_PID=$!
cleanup() {
  kill "$OPENCODE_PID" 2>/dev/null
  lsof -ti tcp:4096 | xargs kill -9 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:4096/global/health > /dev/null && break
  sleep 1
done
curl -sf http://127.0.0.1:4096/global/health > /dev/null || { echo "OpenCode did not start; see data/opencode.log"; exit 1; }
echo "OpenCode is up (log: data/opencode.log)"

export OPENCODE_MODEL="$MODEL"
echo "== Game ==  model: $MODEL"
check=$(python backend.py check)
echo "$check"
echo "$check" | grep -q "^PASS" || { echo "tool check failed; not starting the game"; exit 1; }
python game.py --backend opencode --workers 4 "$@"
