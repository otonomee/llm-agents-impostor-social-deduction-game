"""Impostor social deduction game run by a Python game master.

Usage:
  python game.py --backend mock --games 3
  python game.py --backend lobster --games 1
"""
import argparse
import json
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from backend import make_backend

COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "black", "white", "brown"]
ROOM_NAMES = ["Cafeteria", "Reactor", "Navigation", "Storage"]
ROUNDS = 4
DATA = Path(__file__).parent / "data"
WORDS = json.loads((Path(__file__).parent / "words.json").read_text())


@dataclass
class Player:
    color: str
    impostor: bool
    alive: bool = True
    notes: list = field(default_factory=list)  # everything this player knows, in order


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


class Game:
    def __init__(self, backend, number, lessons, used_words, workers=10, round_seconds=240, max_messages=80,
                 clue_words=5, judge_clues=True, reveal=False):
        self.reveal = reveal
        self.clue_words = clue_words
        self.judge_clues = judge_clues
        self.round_seconds = round_seconds
        self.max_messages = max_messages
        self.announced = {}  # color -> (suspect, confidence) last said out loud
        self.backend = backend
        self.number = number
        self.lessons = lessons
        self.used_words = used_words
        self.workers = workers
        impostor = random.choice(COLORS)
        self.players = {c: Player(c, c == impostor) for c in COLORS}
        self.impostor = impostor
        self.record = {"game": number, "impostor": impostor, "rounds": [], "fallbacks": []}

    # ---------- helpers ----------
    def tag(self, color):
        return f"{color}*" if self.reveal and color == self.impostor else color

    def alive(self):
        return [p for p in self.players.values() if p.alive]

    def tell(self, players, text):
        for p in players:
            p.notes.append(text)

    def system_prompt(self, p):
        rules = (
            "You are playing a social deduction game with 10 players named by color. One is secretly the impostor. "
            "Each round: players are split into rooms of 3+; crewmates learn a secret word, the impostor only learns "
            "its category; the impostor eliminates someone in its own room; the elimination is announced but not where it "
            "happened; every player secretly submits their room and roommates, and all room claims are revealed together; "
            "then players give short clues about the word one at a time, each hearing the clues before theirs (crewmate "
            "clues that make the word guessable on their own are rejected); then the floor opens: "
            "every player reacts to the latest messages at the same time, again and again, until the floor goes quiet, "
            "time runs out, or the message limit is hit. If your suspicion changes, you must say it out loud. If "
            "someone addresses you directly, you must answer. "
            "then everyone privately names a suspect and votes. The most-voted player is ejected (ties or SKIP: nobody). "
            "The secret word is revealed after each vote. The impostor wins by surviving the round 4 vote. "
            "Reply with JSON only, no other text."
        )
        if p.impostor:
            role = (f"You are {p.color}, the IMPOSTOR. You may lie about anything, including your room. Listen to the "
                    "clues given before yours and bluff one that fits them. Blend in, redirect suspicion, and never "
                    "admit your role.")
        else:
            table = "\n".join(
                f"- {l['behavior']} ({l['type']}): pointed at the real impostor {l['correct']} of "
                f"{l['correct'] + l['incorrect']} times (random guessing would average {l.get('expected', 0):.1f})"
                for l in self.lessons) or "none yet"
            role = (f"You are {p.color}, a crewmate. Always report your room and roommates truthfully. Give clues "
                    "that fellow crewmates will recognize but that reveal little to someone who only knows the category, "
                    "because the impostor hears earlier clues and will copy them. Your goal is to eject the impostor.\n"
                    "Signals from past games. Above random means a real tell; at or below random means a red herring:\n"
                    f"{table}")
        return f"{rules}\n\n{role}"

    def ask(self, p, instruction, template, validate, valid_colors=(), retries=3):
        user = (
            "WHAT YOU KNOW SO FAR:\n" + "\n".join(p.notes) +
            f"\n\nNOW: {instruction}\nVALID: {','.join(valid_colors)}\nJSON: {template}"
        )
        hint = ""
        for _ in range(retries):
            raw = self.backend.complete(self.system_prompt(p), user + hint)
            data = parse_json(raw)
            if isinstance(data, dict) and isinstance(data.get("confidence"), (int, float)):
                c = data["confidence"]
                if 0 < c <= 1 and not float(c).is_integer():
                    c = c * 100  # model answered on a 0-1 scale
                data["confidence"] = int(round(max(0, min(100, c))))
            error = "reply was not valid JSON" if data is None else validate(data)
            if not error:
                return data, raw
            log(f"    ✗ {p.color} rejected: {error}")
            self.record.setdefault("rejections", []).append(
                {"player": p.color, "asked": instruction[:40], "error": error, "reply": str(raw)[:200]})
            hint = f"\n\nYour last reply was rejected: {error}. Try again."
            last = data
        return None, locals().get("last")

    def fallback(self, what, p, value):
        self.record["fallbacks"].append({"player": p.color, "step": what, "used": value})
        log(f"  ! {p.color} gave no valid {what}; using fallback")
        return value

    # ---------- round ----------
    def play(self):
        shown = self.impostor if self.reveal else "hidden until the end"
        log(f"\n════ GAME {self.number} ════  impostor: {shown}")
        winner = "impostor"
        for r in range(1, ROUNDS + 1):
            rnd = {"round": r}
            self.record["rounds"].append(rnd)
            if self.round(r, rnd):
                winner = "crew"
                break
        self.record["winner"] = winner
        log(f"\n════ {winner.upper()} WINS ════  impostor was {self.impostor}")
        return self.record

    def round(self, r, rnd):
        alive = self.alive()

        # A. rooms
        order = alive[:]
        random.shuffle(order)
        k = max(1, len(order) // 3)
        rooms = {ROOM_NAMES[i]: order[i::k] for i in range(k)}
        room_of = {}
        for name, members in rooms.items():
            for p in members:
                room_of[p.color] = name
                mates = ", ".join(m.color for m in members if m is not p)
                p.notes.append(f"[Round {r}, private] You are in {name} with {mates}.")
        rnd["rooms"] = {n: [p.color for p in m] for n, m in rooms.items()}

        # B. secret word
        category = random.choice(list(WORDS))
        options = [w for w in WORDS[category] if w not in self.used_words] or WORDS[category]
        word = random.choice(options)
        self.used_words.append(word)
        imp = self.players[self.impostor]
        self.tell([p for p in alive if not p.impostor], f"[Round {r}, private] Category: {category}. Secret word: {word}.")
        imp.notes.append(f"[Round {r}, private] Category: {category}. You do not know the word.")
        rnd.update(category=category, word=word)
        log(f"\n━━━ ROUND {r} ━━━  {len(alive)} alive")
        for name, members in rnd["rooms"].items():
            log(f"  {name}: {', '.join(self.tag(c) for c in members)}")
        log(f"  category: {category}   secret word: {word}")

        # C. elimination
        targets = [p.color for p in rooms[room_of[imp.color]] if p is not imp]
        data, _ = self.ask(
            imp, "Choose one player in your room to eliminate.", '{"eliminate": "<color>"}',
            lambda d: None if d.get("eliminate") in targets else f"eliminate must be one of {targets}", targets)
        victim_color = data["eliminate"] if data else self.fallback("elimination", imp, random.choice(targets))
        victim = self.players[victim_color]
        victim.alive = False
        rnd["eliminated"] = victim_color
        where = f" in {room_of[victim_color]}" if self.reveal else ""  # location would spoil the impostor for you
        log(f"  ☠ {victim_color} eliminated{where}")

        # D. sealed room claims (parallel; nobody sees anyone else's claim yet)
        claimants = [p for p in alive if p.alive]
        others = lambda p: [c.color for c in claimants if c is not p] + [victim_color]

        def claim(p):
            def check(d):
                if not isinstance(d.get("roommates"), list) or not d.get("room"):
                    return "room and roommates are required"
                return None
            data, _ = self.ask(p, "Submit your sealed room claim.",
                               '{"room": "<room>", "roommates": ["<color>"]}', check, others(p))
            return p, data or self.fallback("room claim", p, {"room": "?", "roommates": []})

        with ThreadPoolExecutor(self.workers) as ex:
            claims = dict((p.color, d) for p, d in ex.map(claim, claimants))
        rnd["claims"] = claims

        # E. reveal the elimination (not where it happened) and all room claims at once
        lines = [f"{c}: ROOM {d['room']} | ROOMMATES {', '.join(map(str, d['roommates']))}" for c, d in claims.items()]
        self.tell(claimants, f"[Round {r}, public] {victim_color} was eliminated. The location was not reported.\n"
                             "Room claims:\n" + "\n".join(lines))

        log("  room claims:")
        for c, d in claims.items():
            log(f"    {self.tag(c)}: {d['room']} with {', '.join(map(str, d['roommates']))}")

        # E2. clues in sequence: each player hears every earlier clue before giving theirs
        order = claimants[:]
        random.shuffle(order)
        if order[0].impostor:  # the impostor never goes first, so it always hears at least one clue
            swap = random.randrange(1, len(order))
            order[0], order[swap] = order[swap], order[0]
        rnd["clue_order"] = [p.color for p in order]

        def too_obvious(clue):
            """A neutral judge sees only the category and this one clue. If it can name the word, reject."""
            if not self.judge_clues:
                return False
            reply = self.backend.complete(
                "You are a word-guessing judge. Reply with JSON only.",
                f"Category: {category}. Clue: \"{clue}\". What single word is this clue most likely describing? "
                'Give only your top guess.\nJSON: {"guess": "<word>"}')
            data = parse_json(reply) or {}
            guess = re.sub(r"[^a-z]", "", str(data.get("guess", "")).lower())
            target = re.sub(r"[^a-z]", "", word.lower())
            # "surf" for "surfing" or "toasters" for "toaster" count as a hit
            obvious = len(guess) >= 4 and (target.startswith(guess) or guess.startswith(target))
            rnd.setdefault("judge", []).append({"clue": clue, "guess": data.get("guess"), "rejected": obvious})
            return obvious

        for n, p in enumerate(order, 1):
            def check_clue(d, p=p):
                clue = str(d.get("clue", "")).strip()
                if not clue:
                    return "clue is required"
                if len(clue.split()) > self.clue_words:
                    return f"clue must be {self.clue_words} words or fewer"
                if re.search(rf"\b({re.escape(word)}|{re.escape(category)})\b", clue, re.I):
                    return "clue must not contain the secret word or category"
                if not p.impostor and too_obvious(clue):
                    return "that clue is too obvious: a judge named the secret word as its top guess from that clue alone. Be subtler"
                return None
            data, last = self.ask(
                p, f"Give your clue ({self.clue_words} words max). It must not contain the secret word or the "
                   "category, and must not make the word guessable on its own.",
                '{"clue": "<clue>"}', check_clue, others(p))
            if data:
                clue = str(data["clue"]).strip()
            else:
                raw = str((last or {}).get("clue", "")).strip()
                safe = raw and len(raw.split()) <= self.clue_words and not re.search(
                    rf"\b({re.escape(word)}|{re.escape(category)})\b", raw, re.I)
                clue = self.fallback("clue", p, raw if safe else "(no clue)")
            claims[p.color]["clue"] = clue
            if n == 1:
                log("  clues:")
            log(f"    {n}. {self.tag(p.color)}: {clue}")
            self.tell(claimants, f"[Round {r}, clue {n}] {p.color}: {clue}")

        # F. open floor: everyone reacts to new messages in parallel ticks until quiet, time, or budget
        rnd["statements"] = []
        rnd["suspicion_timeline"] = []
        deadline = time.monotonic() + self.round_seconds
        pending = {}   # color -> who asked them something last tick
        tick = 0
        ended_by = "quiet"
        for c in list(self.announced):
            if self.announced[c][0] and not self.players[self.announced[c][0]].alive:
                self.announced[c] = (None, 0)

        while True:
            if time.monotonic() >= deadline:
                ended_by = "time"; break
            if len(rnd["statements"]) >= self.max_messages:
                ended_by = "message limit"; break
            tick += 1

            def react(p, pending=dict(pending)):
                valid = [c.color for c in claimants if c is not p]
                prev_s, prev_c = self.announced.get(p.color, (None, 0))
                asker = pending.get(p.color)
                def check(d):
                    s, conf = d.get("suspect"), d.get("confidence")
                    if s not in valid + [None]:
                        return f"suspect must be one of {valid} or null"
                    if not isinstance(conf, (int, float)):
                        return "confidence must be a number 0-100"
                    if d.get("to") not in valid + ["all"]:
                        return f"to must be 'all' or one of {valid}"
                    changed = s != prev_s or abs(conf - prev_c) >= 20
                    if not d.get("speak"):
                        if changed:
                            return "your suspicion changed since you last spoke, so you must speak and say it"
                        if asker:
                            return f"{asker} addressed you directly, so you must speak and answer"
                        return None
                    text = str(d.get("statement", ""))
                    if not text.strip():
                        return "statement is required when speak is true"
                    if len(text.split()) > 60:
                        return "statement must be 60 words or fewer"
                    if re.search(rf"\b{re.escape(word)}\b", text, re.I):
                        return "do not say the secret word before it is revealed"
                    if changed and s and not re.search(rf"\b{s}\b", text, re.I):
                        return f"your suspicion changed, so your statement must name {s}"
                    return None
                note = f" {asker} addressed you directly; answer them." if asker else ""
                data, last = self.ask(
                    p, "React to the latest messages. Set speak to false only if you have nothing new to say." + note,
                    '{"suspect": "<color or null>", "confidence": <integer 0-100>, "speak": true, "to": "<all or color>", '
                    '"statement": "<60 words max>"}', check, valid)
                if data:
                    return p, data
                # honest fallback: say out loud whatever suspicion they declared
                last = last or {}
                s = last.get("suspect") if last.get("suspect") in valid else prev_s
                conf = last.get("confidence") if isinstance(last.get("confidence"), (int, float)) else prev_c
                changed = s != prev_s or abs(conf - prev_c) >= 20
                if changed or asker:
                    msg = f"I suspect {s} ({conf}% sure)." if s else "I have no suspect right now."
                    return p, self.fallback("reaction", p, {"suspect": s, "confidence": conf, "speak": True,
                                                            "to": asker or "all", "statement": msg})
                return p, self.fallback("reaction", p, {"suspect": s, "confidence": conf, "speak": False})

            with ThreadPoolExecutor(self.workers) as ex:
                reactions = list(ex.map(react, claimants))
            random.shuffle(reactions)  # same-tick messages are simultaneous; shuffle so order is fair

            prev_pending, pending = pending, {}
            spoke = 0
            for p, d in reactions:
                rnd["suspicion_timeline"].append({"tick": tick, "player": p.color, "suspect": d["suspect"],
                                                  "confidence": d["confidence"], "spoke": bool(d.get("speak"))})
                if not d.get("speak") or len(rnd["statements"]) >= self.max_messages:
                    continue
                spoke += 1
                self.announced[p.color] = (d["suspect"], d["confidence"])
                to = d.get("to", "all")
                replying = prev_pending.get(p.color) == to  # answering the person who asked them
                if to != "all" and not replying:
                    pending[to] = p.color  # a new direct question obligates an answer; a reply does not
                label = f"{p.color} -> {to}" if to != "all" else p.color
                rnd["statements"].append({"tick": tick, "player": p.color, "to": to, "text": d["statement"]})
                arrow = f" → {to}" if to != "all" else ""
                log(f"    [t{tick}] {self.tag(p.color)}{arrow} ({d['suspect'] or 'no suspect'} {d['confidence']}%): {d['statement']}")
                self.tell(claimants, f"[Round {r}, tick {tick}] {label}: {d['statement']}")
            log(f"  -- tick {tick}: {spoke} spoke --")
            if spoke == 0:
                ended_by = "quiet"; break
        rnd["discussion"] = {"ticks": tick, "messages": len(rnd["statements"]), "ended_by": ended_by}
        log(f"  floor closed ({ended_by}) after {tick} ticks, {len(rnd['statements'])} messages")

        # G. private report and vote (parallel)
        def report(p):
            valid = [c.color for c in claimants if c is not p]
            def check(d):
                if d.get("suspect") not in valid:
                    return f"suspect must be one of {valid}"
                if d.get("vote") not in valid + ["SKIP"]:
                    return f"vote must be one of {valid} or SKIP"
                if not isinstance(d.get("confidence"), (int, float)):
                    return "confidence must be a number 0-100"
                return None
            data, _ = self.ask(p, "Privately name your suspect and cast your vote.",
                               '{"suspect": "<color>", "confidence": <integer 0-100>, "reason": "<specific behavior>", '
                               '"vote": "<color or SKIP>"}', check, valid)
            return p, data or self.fallback("report", p, {"suspect": None, "confidence": 0, "reason": "", "vote": "SKIP"})

        with ThreadPoolExecutor(self.workers) as ex:
            reports = dict((p.color, d) for p, d in ex.map(report, claimants))
        baseline = 1 / (len(claimants) - 1)
        rnd["reports"] = [
            {"player": c, **d, "correct": d["suspect"] == self.impostor, "baseline": round(baseline, 3)}
            for c, d in reports.items() if c != self.impostor
        ]

        # H. result
        tally = Counter(d["vote"] for d in reports.values())
        top = tally.most_common()
        ejected = None
        if top and top[0][0] != "SKIP" and (len(top) == 1 or top[0][1] > top[1][1]):
            ejected = top[0][0]
        rnd["votes"] = dict(tally)
        rnd["ejected"] = ejected
        log("  private suspicions:")
        for c, d in reports.items():
            log(f"    {self.tag(c)} suspects {d['suspect']} ({d['confidence']}%), votes {d['vote']}: {d.get('reason', '')}")
        log(f"  votes {dict(tally)} → ejected {ejected or 'nobody'}")

        if ejected == self.impostor:
            self.players[ejected].alive = False
            return True
        if ejected:
            self.players[ejected].alive = False
            msg = f"{ejected} was ejected and was not the impostor."
        else:
            msg = "Nobody was ejected."
        self.tell(self.alive(), f"[Round {r}, public] {msg} The secret word was: {word}.")
        return False


def parse_json(raw):
    raw = re.sub(r"```(json)?", "", raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def update_lessons(backend, record, lessons):
    """LLM labels each crew reason with a behavior; code does all counting."""
    reports = [rep for rnd in record["rounds"] for rep in rnd.get("reports", []) if rep.get("reason")]
    if not reports:
        return lessons
    known = ", ".join(l["behavior"] for l in lessons) or "none"
    listing = "\n".join(f"{i}. {r['reason']}" for i, r in enumerate(reports))
    user = (
        "Label each suspicion reason below with a short behavior name (reuse an existing name when it fits) and a "
        f"type: clue, room, or discussion.\nExisting behavior names: {known}\n\n{listing}\n\n"
        'JSON: {"labels": [{"i": 0, "behavior": "<name>", "type": "<clue|room|discussion>"}]}'
    )
    data = parse_json(backend.complete("You label game data. Reply with JSON only.", user, max_tokens=2000)) or {}
    by_name = {l["behavior"]: l for l in lessons}
    for label in data.get("labels", []):
        i = label.get("i")
        if not isinstance(i, int) or not 0 <= i < len(reports):
            continue
        name = str(label.get("behavior") or "other")
        entry = by_name.setdefault(name, {"behavior": name, "type": label.get("type", "discussion"),
                                          "correct": 0, "incorrect": 0, "expected": 0.0})
        entry.setdefault("expected", 0.0)
        entry["correct" if reports[i]["correct"] else "incorrect"] += 1
        entry["expected"] = round(entry["expected"] + reports[i]["baseline"], 3)  # hits random guessing would get
    # keep red herrings too: knowing what misleads is as useful as knowing what works
    kept = sorted(by_name.values(), key=lambda l: l["correct"] + l["incorrect"], reverse=True)
    return kept[:15]


def summarize(record):
    rows = []
    for rnd in record["rounds"]:
        reps = rnd.get("reports", [])
        if reps:
            rows.append(f"R{rnd['round']} {sum(r['correct'] for r in reps)}/{len(reps)} (baseline {reps[0]['baseline']:.0%})")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "opencode", "lobster", "anthropic"])
    ap.add_argument("--games", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=10, help="parallel calls per tick, claims, and votes")
    ap.add_argument("--round-seconds", type=int, default=240, help="discussion time limit per round")
    ap.add_argument("--max-messages", type=int, default=80, help="safety cap on statements per round")
    ap.add_argument("--clue-words", type=int, default=5, help="max words per clue")
    ap.add_argument("--no-judge", action="store_true", help="skip the too-obvious clue check (fewer calls)")
    ap.add_argument("--reveal", action="store_true", help="mark the impostor with * in live output from the start")
    args = ap.parse_args()

    backend = make_backend(args.backend, args.temperature)
    DATA.mkdir(exist_ok=True)
    (DATA / "games").mkdir(exist_ok=True)
    history_path = DATA / "history.jsonl"

    for _ in range(args.games):
        lessons = load_json(DATA / "discernment.json", [])
        used = load_json(DATA / "used_words.json", [])
        number = sum(1 for _ in history_path.open()) + 1 if history_path.exists() else 1

        record = Game(backend, number, lessons, used, args.workers, args.round_seconds, args.max_messages,
                      args.clue_words, not args.no_judge, args.reveal).play()

        # all files are written only after the game ends, so nothing secret is on disk mid-game
        (DATA / "games" / f"game-{number}.json").write_text(json.dumps(record, indent=2))
        (DATA / "used_words.json").write_text(json.dumps(used))
        (DATA / "discernment.json").write_text(json.dumps(update_lessons(backend, record, lessons), indent=2))
        summary = {"game": number, "impostor": record["impostor"], "winner": record["winner"],
                   "round_ended": len(record["rounds"]), "accuracy": summarize(record),
                   "fallbacks": len(record["fallbacks"])}
        with history_path.open("a") as f:
            f.write(json.dumps(summary) + "\n")
        log(f"  accuracy: {'; '.join(summary['accuracy'])}")


if __name__ == "__main__":
    main()
