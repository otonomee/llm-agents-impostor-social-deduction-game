# Impostor

A social deduction game for LLM agents. Python is the game master: it assigns roles,
rooms, and secret words, controls every message, and scores how well the crew
discerns the impostor. Each player call is stateless, so players only ever see
what the engine gives them.

## Setup (OpenCode)
    pip install requests
    cd players
    opencode serve --port 4096      # run it FROM the players folder so it loads the no-tools player agent
    # in another terminal, from the app folder:
    python backend.py check         # confirms players cannot read files; must print PASS

Pick a model with OPENCODE_MODEL=provider/model-id (see `opencode models`), or add
"model": "provider/model-id" to the player agent in players/opencode.json.
Temperature is set in players/opencode.json, not on the command line.

## Run
    python game.py --backend mock --games 3          # free test run, random players
    python game.py --backend opencode --games 1      # needs `opencode serve` running in players/
    python game.py --backend lobster --games 1       # needs `lobster serve` running
    python game.py --backend anthropic --games 1     # needs ANTHROPIC_API_KEY

Lobster env vars: LOBSTER_SERVE_URL (default http://127.0.0.1:4096), LOBSTER_PROVIDER, MODEL.

## Output (data/)
- games/game-N.json   full log: rooms, words, claims, statements, reports, votes
- history.jsonl       one line per game: winner, per-round accuracy vs random baseline
- discernment.json    behaviors crew cited, how often each pointed at the real impostor
- used_words.json     words already used

Nothing is written to disk until a game ends.

## Tuning
- --temperature: default 1.0. At 0, crewmates tend to act identically.
- --round-seconds: discussion time limit per round (default 240). Checked between ticks.
- --max-messages: safety cap on statements per round (default 80).
- --workers: parallel calls per tick, claims, and votes. Lower it if the server struggles.

## Discussion rules
The floor opens after claims are revealed. Each tick, every living player sees all new
messages and reacts at the same time. A player must speak if their suspect changed or
their confidence moved 20+ points since they last spoke, and must answer if someone
addressed them directly. Replies don't create a new obligation, so exchanges can't
ping-pong forever. The floor closes when a tick passes with nobody speaking, time runs
out, or the message cap is hit. Every tick's declared suspicions are logged in
suspicion_timeline.
- --clue-words: max words per clue (default 5).
- --no-judge: skip the too-obvious clue check. Saves one call per clue attempt.
- words.json: add categories and words. Bigger categories make clues harder to read.

## Round structure
1. Rooms assigned privately; crew get the secret word, the impostor only the category.
2. Impostor eliminates someone in its room. Everyone learns who died, not where.
3. Sealed room claims, revealed together.
4. Clues one at a time in random order (impostor never first), each player hearing earlier clues.
   A neutral judge sees the category and one crew clue; if its top guess is the word, the clue is rejected.
5. Open floor discussion (ticks), then private suspicion reports and votes.
