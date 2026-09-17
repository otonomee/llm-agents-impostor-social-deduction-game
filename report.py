"""Read game results.

  python report.py            # summary across all games
  python report.py 3          # readable transcript of game 3
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"


def transcript(n):
    g = json.loads((DATA / "games" / f"game-{n}.json").read_text())
    print(f"GAME {n}   impostor: {g['impostor']}   winner: {g['winner']}")
    for r in g["rounds"]:
        print(f"\n--- ROUND {r['round']} ---")
        print(f"rooms: {r['rooms']}")
        print(f"category: {r['category']}   secret word: {r['word']}")
        print(f"eliminated: {r['eliminated']} (location hidden from players)")
        if r.get("clue_order"):
            print(f"clue order: {' -> '.join(r['clue_order'])}")
        print("claims:")
        for c, d in r.get("claims", {}).items():
            tag = "  <-- IMPOSTOR" if c == g["impostor"] else ""
            print(f"  {c:7} room {d['room']:10} with {', '.join(map(str, d['roommates'])):20} clue: {d.get('clue', '')}{tag}")
        disc = r.get("discussion", {})
        print(f"discussion: {disc.get('messages', 0)} messages over {disc.get('ticks', 0)} ticks, ended by {disc.get('ended_by')}")
        for s in r.get("statements", []):
            to = f" -> {s['to']}" if s["to"] != "all" else ""
            print(f"  [t{s['tick']}] {s['player']}{to}: {s['text']}")
        print("private reports (crew):")
        for rep in r.get("reports", []):
            mark = "RIGHT" if rep["correct"] else "wrong"
            print(f"  {rep['player']:7} suspects {str(rep['suspect']):7} {rep['confidence']:>3}%  {mark}  | {rep['reason']}")
        print(f"votes: {r.get('votes')}   ejected: {r.get('ejected')}")
    fb = Counter(f["step"] for f in g["fallbacks"])
    print(f"\nfallbacks (engine had to substitute a reply): {dict(fb) or 'none'}")


def summary():
    games = sorted((p for p in DATA.glob("games/game-*.json") if "incomplete" not in p.stem),
                   key=lambda p: int(p.stem.split("-")[1]))
    if not games:
        print("no games yet"); return
    wins = Counter()
    hits, total, expected = defaultdict(int), defaultdict(int), defaultdict(float)
    fallbacks = 0
    for path in games:
        g = json.loads(path.read_text())
        wins[g["winner"]] += 1
        fallbacks += len(g["fallbacks"])
        for r in g["rounds"]:
            for rep in r.get("reports", []):
                total[r["round"]] += 1
                hits[r["round"]] += rep["correct"]
                expected[r["round"]] += rep["baseline"]
    n = len(games)
    print(f"games: {n}   crew wins: {wins['crew']}   impostor wins: {wins['impostor']}")
    print(f"avg fallbacks per game: {fallbacks / n:.1f}")
    print("crew accuracy by round (random guessing would score 'expected'):")
    for rnd in sorted(total):
        print(f"  round {rnd}: {hits[rnd]}/{total[rnd]} = {hits[rnd] / total[rnd]:.0%}   expected {expected[rnd] / total[rnd]:.0%}")
    lessons = DATA / "discernment.json"
    if lessons.exists():
        print("\nbehaviors crew relied on:")
        for l in json.loads(lessons.read_text()):
            used = l["correct"] + l["incorrect"]
            verdict = "TELL" if l["correct"] > l.get("expected", 0) else "red herring"
            print(f"  {l['behavior']:30} right {l['correct']}/{used}   chance ~{l.get('expected', 0):.1f}   {verdict}")


if __name__ == "__main__":
    transcript(sys.argv[1]) if len(sys.argv) > 1 else summary()
