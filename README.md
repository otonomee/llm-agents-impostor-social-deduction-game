# LLM Agents: Impostor Social Deduction Game

Ten LLM agents play a hidden-impostor social deduction game (inspired by Among Us). Crewmates share a secret word; the
impostor only knows its category and has to bluff, kill, and steer votes onto innocents. A Python game master runs
everything: roles, rooms, words, message routing, validation, scoring. Every model call is stateless, so a player
only ever knows what the engine tells it.

## How a round works
1. Rooms are assigned privately. Crewmates get the secret word; the impostor gets only the category.
2. The impostor eliminates any one player, anywhere in the map. Everyone learns who died, not where.
3. Sealed room claims (room name + roommates), revealed all at once. The impostor may lie.
4. Clues, one at a time, each player hearing the ones before. The impostor never goes first. A neutral judge
   rejects crew clues that give the word away on their own.
5. Open floor: every living player reacts to new messages at once, tick after tick. Anyone whose suspicion changes
   must say so out loud; anyone addressed directly must answer. Closes when the floor goes quiet, time runs out,
   or the message cap is hit.
6. Private suspicion reports and votes. Most votes is ejected; ties and SKIP eject nobody.

The crew wins by ejecting the impostor. The impostor wins by surviving the round 4 vote.

## Stakes
Ten persistent agents (Vex, Juno, Orin, ...) form a league. Each keeps a career record and a private journal it
writes after every game. Scoring is individual: crewmates lose points for voting out innocents, getting ejected while
innocent, or saying the secret word out loud (which leaks it to the impostor), and gain points for ejecting the
impostor or being first to call it out. The impostor scores for every round survived and every innocent ejected.

After each game, the agent with the lowest average (3+ games played) is retired for real: its record and journal are
deleted, its last words go into `data/graveyard.json`, its name is never reused, and a new agent takes the seat.
Every agent sees the standings and who is closest to retirement. `--no-retire` turns this off.

## Setup (OpenCode)
    pip install requests
    cd players
    opencode serve --port 4096      # run FROM players/ so it loads the no-tools player agent
    # second terminal, from the repo root:
    python backend.py check         # must print PASS: players cannot read files

Choose a model with `OPENCODE_MODEL=provider/model-id` (list them with `opencode models`) or set `"model"` on the
player agent in `players/opencode.json`. Temperature lives in that file too.

## Run
    python game.py --backend opencode --games 1
    python game.py --backend opencode --games 5 --reveal     # spectator mode
    python game.py --backend mock --games 3                  # free test run with random players

Other backends: `anthropic` (needs `ANTHROPIC_API_KEY`) and `lobster`
(`LOBSTER_SERVE_URL`, `LOBSTER_PROVIDER`, `MODEL`).

## Watching
The live view shows seats, kills, room claims, clues, the discussion as chat, a heat bar of who is suspected, the
vote, then scores, standings, and retirements.

- `--reveal` shows the impostor (marked `*`), true rooms, kill locations, and who lied about their room.
- `--verbose` adds rejected replies, fallbacks, and private vote reasons.
- `--no-color` for plain output.

## Options
| flag | default | what it does |
|---|---|---|
| `--games` | 1 | games to play back to back |
| `--round-seconds` | 240 | discussion time limit per round |
| `--max-messages` | 40 | discussion message cap per round |
| `--say-words` | 25 | max words per discussion message |
| `--clue-words` | 5 | max words per clue |
| `--no-judge` | off | skip the too-obvious clue check (fewer calls) |
| `--no-retire` | off | never retire agents |
| `--workers` | 10 | parallel model calls; lower it if your provider throttles |
| `--temperature` | 1.0 | used by the anthropic/lobster backends; OpenCode reads it from players/opencode.json |

## Results
    python report.py        # win rates, crew accuracy vs random chance, tells vs red herrings
    python report.py 3      # full transcript of game 3

Files in `data/`:
- `games/game-N.json`: full log (rooms, words, claims, clues, statements, suspicion timeline, votes, scores)
- `games/game-N-incomplete.json`: whatever was saved when a game was stopped or crashed
- `roster.json`: the current league, with records and journals
- `graveyard.json`: retired agents and their last words
- `history.jsonl`: one line per finished game
- `discernment.json`: behaviors the crew cited and how often each pointed at the real impostor
- `used_words.json`: secret words already used

`words.json` holds the categories and words. Bigger categories make clues harder to read.
