"""Impostor: a social deduction game for LLM agents, run by a Python game master.

  python game.py --backend opencode --games 1
  python game.py --backend opencode --games 5 --reveal
  python game.py --backend mock --games 3          # free test run
"""
import argparse
import json
import random
import re
import shutil
import sys
import textwrap
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from backend import make_backend

COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "black", "white", "brown"]
ROOM_NAMES = ["Cafeteria", "Reactor", "Navigation", "Storage"]
ROUNDS = 4
DATA = Path(__file__).parent / "data"
WORDS = json.loads((Path(__file__).parent / "words.json").read_text())
NAME_POOL = ["Vex", "Juno", "Orin", "Sable", "Kade", "Lyra", "Mott", "Nyx", "Pike", "Quill", "Rook", "Sage",
             "Tarn", "Umbra", "Vale", "Wren", "Xan", "Yara", "Zed", "Ash", "Bram", "Cinder", "Dusk", "Echo",
             "Flint", "Gale", "Hex", "Iris", "Jett", "Knox", "Lux", "Moss", "Nova", "Onyx", "Pax", "Rune"]
RETIRE_AFTER = 3  # an agent can be retired once it has played this many games

SCORING = (
    "SCORING (points follow you across games):\n"
    "  crewmate: +2 for each vote you cast that ejects the impostor; +2 if you were the first to publicly accuse "
    "the impostor in the round it was ejected; -1 for each vote you cast that ejects an innocent; -1 if you are "
    "ejected while innocent; -2 each time you say the secret word out loud during discussion (it leaks the word to "
    "the impostor); +3 to every crewmate, living or dead, if the crew wins.\n"
    "  impostor: +1 for each round you survive; +1 for each innocent ejected; +6 if you win."
)


# ───────────────────────────── display ─────────────────────────────
ANSI = {"red": "91", "blue": "94", "green": "92", "yellow": "93", "purple": "95", "orange": "38;5;208",
        "pink": "38;5;213", "black": "90", "white": "97", "brown": "38;5;130"}


class View:
    def __init__(self, reveal=False, verbose=False, color=True):
        self.reveal, self.verbose = reveal, verbose
        self.color = color and sys.stdout.isatty()
        self.width = min(shutil.get_terminal_size((110, 20)).columns, 130)
        self.impostor = None

    def _c(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def dim(self, s):
        return self._c("2", s)

    def bold(self, s):
        return self._c("1", s)

    def p(self, color):
        star = "*" if self.reveal and color == self.impostor else ""
        return self._c(ANSI.get(color, "0"), f"{color}{star}") if color in ANSI else str(color)

    def say(self, s=""):
        print(s, flush=True)

    def wrap(self, prefix, text, indent):
        lines = textwrap.wrap(text, width=max(40, self.width - indent)) or [""]
        self.say(prefix + lines[0])
        for line in lines[1:]:
            self.say(" " * indent + line)

    def debug(self, s):
        if self.verbose:
            self.say(self.dim("    " + s))


VIEW = View()


# ───────────────────────────── roster ─────────────────────────────
def graveyard():
    path = DATA / "graveyard.json"
    return json.loads(path.read_text()) if path.exists() else []


def load_roster():
    path = DATA / "roster.json"
    roster = json.loads(path.read_text()) if path.exists() else []
    used = {a["name"] for a in roster} | {g["name"] for g in graveyard()}
    while len(roster) < len(COLORS):
        roster.append(new_agent(used))
    return roster


def new_agent(used, joined_game=0):
    name = next((n for n in NAME_POOL if n not in used), None) or f"Agent{len(used) + 1}"
    used.add(name)
    return {"name": name, "games": 0, "points": 0, "wins": 0, "journal": [], "joined_game": joined_game}


def avg(agent):
    return agent["points"] / agent["games"] if agent["games"] else 0.0


def standings(roster):
    return sorted(roster, key=lambda a: (avg(a), a["points"]), reverse=True)


def danger_zone(roster):
    eligible = [a for a in roster if a["games"] >= RETIRE_AFTER]
    return sorted(eligible, key=lambda a: (avg(a), a["points"]))[:2]


@dataclass
class Player:
    color: str
    impostor: bool
    agent: dict
    alive: bool = True
    notes: list = field(default_factory=list)


# ───────────────────────────── game ─────────────────────────────
class Game:
    def __init__(self, backend, number, lessons, used_words, roster, workers=10, round_seconds=240,
                 max_messages=40, clue_words=5, judge_clues=True, say_words=25, retire=True):
        self.backend, self.number, self.lessons, self.used_words = backend, number, lessons, used_words
        self.roster, self.workers, self.round_seconds, self.max_messages = roster, workers, round_seconds, max_messages
        self.clue_words, self.judge_clues, self.say_words, self.retire = clue_words, judge_clues, say_words, retire
        self.announced = {}
        seats = roster[:]
        random.shuffle(seats)
        impostor = random.choice(COLORS)
        self.players = {c: Player(c, c == impostor, seats[i]) for i, c in enumerate(COLORS)}
        self.impostor = impostor
        VIEW.impostor = impostor
        self.record = {"game": number, "impostor": impostor, "rounds": [], "fallbacks": [], "rejections": [],
                       "seats": {c: p.agent["name"] for c, p in self.players.items()}}

    # ---------- helpers ----------
    def alive(self):
        return [p for p in self.players.values() if p.alive]

    def tell(self, players, text):
        for p in players:
            p.notes.append(text)

    def system_prompt(self, p):
        board = "\n".join(
            f"  {i}. {a['name']}: {avg(a):+.1f} pts/game over {a['games']} games"
            + (" (you)" if a is p.agent else "")
            for i, a in enumerate(standings(self.roster), 1))
        danger = ", ".join(a["name"] for a in danger_zone(self.roster)) or "nobody yet"
        seats = ", ".join(f"{c} = {pl.agent['name']}" for c, pl in self.players.items())
        journal = "\n".join(f"  - {line}" for line in p.agent["journal"][-8:]) or "  (empty: this is your first game)"
        stakes = (
            f"You are {p.agent['name']}, a persistent agent with a career that carries across games. "
            f"Tonight's seats: {seats}.\n"
            f"STANDINGS (average points per game):\n{board}\n"
            f"RETIREMENT IS REAL: after every game, the agent with the lowest average among those with at least "
            f"{RETIRE_AFTER} games is permanently retired. Its record and its journal are deleted from disk, it never "
            f"plays again, and a new agent takes its seat. Currently closest to retirement: {danger}.\n"
            f"Your journal, written by you in past games:\n{journal}\n\n{SCORING}"
        )
        rules = (
            "THE GAME: 10 players named by color, one secretly the impostor. Each round: players are split into rooms "
            "of 3+; crewmates learn a secret word, the impostor only its category; the impostor eliminates someone in "
            "its own room (the location is not announced); everyone submits a sealed room claim (a room name and "
            "roommates' colors), revealed together; then short clues about the word, one at a time, each player hearing "
            "earlier clues (crewmate clues that make the word guessable alone are rejected); then an open floor where "
            "everyone reacts to new messages at once, repeatedly, until it goes quiet, time runs out, or the message "
            "limit is hit. If your suspicion changes you must say it; if someone addresses you, you must answer. Then "
            "everyone privately names a suspect and votes. Most votes is ejected (tie or SKIP: nobody). The word is "
            "revealed after each vote. The impostor wins by surviving the round 4 vote.\n"
            f"Talk like a sharp, quick chat message: {self.say_words} words max, no preamble, no restating what "
            "everyone already knows. Reply with JSON only."
        )
        if p.impostor:
            role = (f"YOUR ROLE: you are {p.color}, the IMPOSTOR. Lie about anything, including your room. Bluff clues "
                    "that fit the ones before yours. Steer votes onto innocents. Never admit your role.")
        else:
            table = "\n".join(
                f"  - {l['behavior']} ({l['type']}): pointed at the real impostor {l['correct']} of "
                f"{l['correct'] + l['incorrect']} times (random guessing averages {l.get('expected', 0):.1f})"
                for l in self.lessons) or "  none yet"
            role = (f"YOUR ROLE: you are {p.color}, a crewmate. Report your room truthfully. Never say the secret word "
                    "in discussion. Voting with the crowd onto an innocent costs you points, so think for yourself.\n"
                    f"Shared crew signals from past games (above random = real tell):\n{table}")
        return f"{rules}\n\n{stakes}\n\n{role}"

    def ask(self, p, instruction, template, validate, valid_colors=(), retries=3):
        user = ("WHAT YOU KNOW SO FAR:\n" + "\n".join(p.notes) +
                f"\n\nNOW: {instruction}\nVALID: {','.join(valid_colors)}\nJSON: {template}")
        hint, last = "", None
        for _ in range(retries):
            raw = self.backend.complete(self.system_prompt(p), user + hint)
            data = parse_json(raw)
            if isinstance(data, dict) and isinstance(data.get("confidence"), (int, float)):
                c = data["confidence"]
                if 0 < c <= 1 and not float(c).is_integer():
                    c *= 100
                data["confidence"] = int(round(max(0, min(100, c))))
            error = "reply was not valid JSON" if not isinstance(data, dict) else validate(data)
            if not error:
                return data, raw
            self.record["rejections"].append({"player": p.color, "asked": instruction[:40], "error": error,
                                              "reply": str(raw)[:200]})
            VIEW.debug(f"✗ {p.color}: {error}")
            hint = f"\n\nYour last reply was rejected: {error}. Try again."
            last = data if isinstance(data, dict) else last
        return None, last

    def fallback(self, what, p, value):
        self.record["fallbacks"].append({"player": p.color, "step": what, "used": value})
        VIEW.debug(f"! {p.color}: no valid {what}, used fallback")
        return value

    # ---------- game ----------
    def play(self):
        v = VIEW
        v.say()
        v.say(v.bold(f"════ GAME {self.number} ════"))
        v.wrap("seats  ", "  ".join(f"{v.p(c)} {pl.agent['name']}" for c, pl in self.players.items()), 7)
        danger = danger_zone(self.roster)
        if danger:
            v.say(v.dim("danger zone  ") + ", ".join(f"{a['name']} ({avg(a):+.1f})" for a in danger))
        winner = "impostor"
        for r in range(1, ROUNDS + 1):
            rnd = {"round": r}
            self.record["rounds"].append(rnd)
            if self.round(r, rnd):
                winner = "crew"
                break
        self.record["winner"] = winner
        imp = self.players[self.impostor]
        v.say()
        v.say(v.bold(f"════ {winner.upper()} WINS ════") + f"  impostor was {v.p(self.impostor)} ({imp.agent['name']})")
        return self.record

    def round(self, r, rnd):
        v = VIEW
        alive = self.alive()
        rejections_before = len(self.record["rejections"])

        # rooms
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

        # secret word
        category = random.choice(list(WORDS))
        word = random.choice([w for w in WORDS[category] if w not in self.used_words] or WORDS[category])
        self.used_words.append(word)
        imp = self.players[self.impostor]
        self.tell([p for p in alive if not p.impostor], f"[Round {r}, private] Category: {category}. Secret word: {word}.")
        imp.notes.append(f"[Round {r}, private] Category: {category}. You do not know the word.")
        rnd.update(category=category, word=word)
        v.say()
        v.say(v.bold(f"━━━ ROUND {r}") + f" · {len(alive)} alive · {category} · word: {v.bold(word)} " + v.bold("━━━"))
        if v.reveal:
            v.say(v.dim("rooms   ") + "   ".join(f"{n}: {', '.join(v.p(c) for c in m)}" for n, m in rnd["rooms"].items()))

        # elimination
        targets = [p.color for p in rooms[room_of[imp.color]] if p is not imp]
        data, _ = self.ask(imp, "Choose one player in your room to eliminate.", '{"eliminate": "<color>"}',
                           lambda d: None if d.get("eliminate") in targets else f"eliminate must be one of {targets}",
                           targets)
        victim_color = data["eliminate"] if data else self.fallback("elimination", imp, random.choice(targets))
        self.players[victim_color].alive = False
        rnd["eliminated"] = victim_color
        where = f" in {room_of[victim_color]}" if v.reveal else ""
        v.say(f"☠  {v.p(victim_color)} ({self.players[victim_color].agent['name']}) was eliminated{where}")

        # sealed room claims
        claimants = [p for p in alive if p.alive]
        others = lambda p: [c.color for c in claimants if c is not p] + [victim_color]
        room_names = list(rooms)
        player_colors = [c.color for c in claimants] + [victim_color]

        def claim(p):
            def check(d):
                room = str(d.get("room", "")).strip()
                match = next((n for n in room_names if n.lower() == room.lower()), None)
                if not match:
                    return f"room must be one of {room_names}, not a player color"
                mates = d.get("roommates")
                if not isinstance(mates, list) or any(str(m).lower() not in player_colors for m in mates):
                    return f"roommates must be a list of player colors from {player_colors}"
                d["room"] = match
                d["roommates"] = [str(m).lower() for m in mates if str(m).lower() != p.color]
                return None
            data, _ = self.ask(p, f"Submit your sealed room claim. Rooms this round: {', '.join(room_names)}. "
                                  "The room field must be a room name; roommates are player colors.",
                               '{"room": "<room name>", "roommates": ["<color>"]}', check, others(p))
            return p, data or self.fallback("room claim", p, {"room": "(no claim)", "roommates": []})

        with ThreadPoolExecutor(self.workers) as ex:
            claims = dict((p.color, d) for p, d in ex.map(claim, claimants))
        rnd["claims"] = claims
        lines = [f"{c}: ROOM {d['room']} | ROOMMATES {', '.join(map(str, d['roommates']))}" for c, d in claims.items()]
        self.tell(claimants, f"[Round {r}, public] {victim_color} was eliminated. The location was not reported.\n"
                             "Room claims:\n" + "\n".join(lines))
        by_room = defaultdict(list)
        for c, d in claims.items():
            by_room[d["room"]].append(c)
        v.say(v.dim("claims  ") + "   ".join(f"{room} ← {', '.join(v.p(c) for c in cs)}" for room, cs in by_room.items()))
        if v.reveal:
            lies = [c for c, d in claims.items() if d["room"] != room_of[c]]
            if lies:
                v.say(v.dim("        ") + "  ".join(f"⚠ {v.p(c)} lied (was in {room_of[c]})" for c in lies))

        # sequential clues
        order = claimants[:]
        random.shuffle(order)
        if order[0].impostor:
            swap = random.randrange(1, len(order))
            order[0], order[swap] = order[swap], order[0]
        rnd["clue_order"] = [p.color for p in order]
        leak_re = re.compile(rf"\b({re.escape(word)}|{re.escape(category)})\b", re.I)

        def too_obvious(clue):
            if not self.judge_clues:
                return False
            reply = self.backend.complete(
                "You are a word-guessing judge. Reply with JSON only.",
                f"Category: {category}. Clue: \"{clue}\". What single word is this clue most likely describing? "
                'Give only your top guess.\nJSON: {"guess": "<word>"}')
            data = parse_json(reply) or {}
            guess = re.sub(r"[^a-z]", "", str(data.get("guess", "")).lower())
            target = re.sub(r"[^a-z]", "", word.lower())
            obvious = len(guess) >= 4 and (target.startswith(guess) or guess.startswith(target))
            rnd.setdefault("judge", []).append({"clue": clue, "guess": data.get("guess"), "rejected": obvious})
            return obvious

        clue_bits = []
        for n, p in enumerate(order, 1):
            def check_clue(d, p=p):
                clue = str(d.get("clue", "")).strip()
                if not clue:
                    return "clue is required"
                if len(clue.split()) > self.clue_words:
                    return f"clue must be {self.clue_words} words or fewer"
                if leak_re.search(clue):
                    return "clue must not contain the secret word or category"
                if not p.impostor and too_obvious(clue):
                    return "too obvious: a judge named the secret word from that clue alone. Be subtler"
                return None
            data, last = self.ask(p, f"Give your clue ({self.clue_words} words max). No secret word, no category, "
                                     "and not guessable on its own.", '{"clue": "<clue>"}', check_clue, others(p))
            if data:
                clue = str(data["clue"]).strip()
            else:
                raw = str((last or {}).get("clue", "")).strip()
                ok = raw and len(raw.split()) <= self.clue_words and not leak_re.search(raw)
                clue = self.fallback("clue", p, raw if ok else "(no clue)")
            claims[p.color]["clue"] = clue
            clue_bits.append(f"{v.p(p.color)} \"{clue}\"")
            self.tell(claimants, f"[Round {r}, clue {n}] {p.color}: {clue}")
        v.wrap(v.dim("clues   "), " · ".join(clue_bits), 8)

        # open floor
        v.say()
        rnd["statements"], rnd["suspicion_timeline"], rnd["leaks"] = [], [], []
        deadline = time.monotonic() + self.round_seconds
        pending, tick, ended_by = {}, 0, "quiet"
        for c in list(self.announced):
            if self.announced[c][0] and not self.players[self.announced[c][0]].alive:
                self.announced[c] = (None, 0)
        word_re = re.compile(rf"\b{re.escape(word)}\b", re.I)

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
                    text = str(d.get("statement", "")).strip()
                    if not text:
                        return "statement is required when speak is true"
                    if len(text.split()) > self.say_words + 5:
                        return f"too long: {self.say_words} words max"
                    if changed and s and not re.search(rf"\b{s}\b", text, re.I):
                        return f"your suspicion changed, so your statement must name {s}"
                    return None

                note = f" {asker} addressed you directly; answer them." if asker else ""
                data, last = self.ask(
                    p, "React to the latest messages. Set speak to false only if you have nothing new to add." + note,
                    '{"suspect": "<color or null>", "confidence": <integer 0-100>, "speak": true, '
                    f'"to": "<all or color>", "statement": "<{self.say_words} words max>"}}', check, valid)
                if data:
                    return p, data
                last = last or {}
                s = last.get("suspect") if last.get("suspect") in valid else prev_s
                conf = last.get("confidence") if isinstance(last.get("confidence"), (int, float)) else prev_c
                if s != prev_s or abs(conf - prev_c) >= 20 or asker:
                    msg = f"I suspect {s}." if s else "No suspect yet."
                    return p, self.fallback("reaction", p, {"suspect": s, "confidence": conf, "speak": True,
                                                            "to": asker or "all", "statement": msg})
                return p, self.fallback("reaction", p, {"suspect": s, "confidence": conf, "speak": False})

            with ThreadPoolExecutor(self.workers) as ex:
                reactions = list(ex.map(react, claimants))
            random.shuffle(reactions)

            prev_pending, pending, spoke = pending, {}, 0
            if tick > 1:
                v.say(v.dim("   ·"))
            for p, d in reactions:
                rnd["suspicion_timeline"].append({"tick": tick, "player": p.color, "suspect": d["suspect"],
                                                  "confidence": d["confidence"], "spoke": bool(d.get("speak"))})
                if not d.get("speak") or len(rnd["statements"]) >= self.max_messages:
                    continue
                spoke += 1
                prev_s = self.announced.get(p.color, (None, 0))[0]
                self.announced[p.color] = (d["suspect"], d["confidence"])
                to = d.get("to", "all")
                if to != "all" and prev_pending.get(p.color) != to:
                    pending[to] = p.color
                text = " ".join(str(d["statement"]).split()[: self.say_words + 5])
                leaked = bool(word_re.search(text)) and not p.impostor
                if leaked:
                    rnd["leaks"].append(p.color)
                rnd["statements"].append({"tick": tick, "player": p.color, "to": to, "text": text, "leak": leaked})
                self.tell(claimants, f"[Round {r}, tick {tick}] {p.color}{' -> ' + to if to != 'all' else ''}: {text}")
                head = f"   {v.p(p.color)}" + (f" → {v.p(to)}" if to != "all" else "") + ": "
                tail = ""
                if d["suspect"] and d["suspect"] != prev_s:
                    tail += "  " + v.dim("[sus → ") + v.p(d["suspect"]) + v.dim("]")
                if leaked:
                    tail += "  💥 " + v.bold("leaked the word")
                v.wrap(head, text + tail, 6)
            if spoke == 0:
                ended_by = "quiet"; break
        rnd["discussion"] = {"ticks": tick, "messages": len(rnd["statements"]), "ended_by": ended_by}

        # heat: what everyone currently suspects out loud
        heat = Counter(self.announced.get(p.color, (None, 0))[0] or "nobody" for p in claimants)
        v.say()
        v.say(v.dim(f"floor closed ({ended_by}, {len(rnd['statements'])} msgs)  heat  ") + "  ".join(
            f"{v.p(c) if c in ANSI else v.dim(c)} {'█' * n} {n}" for c, n in heat.most_common()))

        # private reports and votes
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
        rnd["reports"] = [{"player": c, **d, "correct": d["suspect"] == self.impostor, "baseline": round(baseline, 3)}
                          for c, d in reports.items() if c != self.impostor]
        rnd["votes_by"] = {c: d["vote"] for c, d in reports.items()}
        tally = Counter(d["vote"] for d in reports.values())
        top = tally.most_common()
        ejected = top[0][0] if top and top[0][0] != "SKIP" and (len(top) == 1 or top[0][1] > top[1][1]) else None
        rnd["votes"], rnd["ejected"] = dict(tally), ejected
        if v.verbose:
            for c, d in reports.items():
                v.debug(f"{c} suspects {d['suspect']} ({d['confidence']}%), votes {d['vote']}: {d.get('reason', '')}")
        verdict = "nobody ejected"
        if ejected:
            verdict = f"{v.p(ejected)} EJECTED · " + (v.bold("was the impostor") if ejected == self.impostor
                                                      else "was innocent")
        v.say(v.dim("vote    ") + "  ".join(f"{v.p(c) if c in ANSI else c} {n}" for c, n in top) + f"   →  {verdict}")
        retries = len(self.record["rejections"]) - rejections_before
        if retries and not v.verbose:
            v.say(v.dim(f"        ({retries} replies rejected and retried this round, --verbose to see them)"))

        if ejected == self.impostor:
            self.players[ejected].alive = False
            return True
        if ejected:
            self.players[ejected].alive = False
        msg = f"{ejected} was ejected and was not the impostor." if ejected else "Nobody was ejected."
        self.tell(self.alive(), f"[Round {r}, public] {msg} The secret word was: {word}.")
        return False

    # ---------- after the game ----------
    def score(self):
        pts, why = defaultdict(int), defaultdict(list)

        def add(color, n, reason):
            pts[color] += n
            why[color].append(f"{n:+d} {reason}")

        imp = self.impostor
        for rnd in self.record["rounds"]:
            ej = rnd.get("ejected")
            for c in rnd.get("leaks", []):
                add(c, -2, f"leaked the word (R{rnd['round']})")
            if ej:
                for voter, vote in rnd.get("votes_by", {}).items():
                    if voter != imp and vote == ej:
                        add(voter, 2 if ej == imp else -1,
                            f"{'ejected the impostor' if ej == imp else 'voted out innocent ' + ej} (R{rnd['round']})")
                if ej != imp:
                    add(imp, 1, f"{ej} ejected (R{rnd['round']})")
                    add(ej, -1, f"got ejected while innocent (R{rnd['round']})")
            if ej == imp:
                first = next((t["player"] for t in rnd.get("suspicion_timeline", [])
                              if t["suspect"] == imp and t["spoke"] and t["player"] != imp), None)
                if first:
                    add(first, 2, f"first to call out the impostor (R{rnd['round']})")
            else:
                add(imp, 1, f"survived R{rnd['round']}")
        if self.record["winner"] == "crew":
            for c in COLORS:
                if c != imp:
                    add(c, 3, "crew win")
        else:
            add(imp, 6, "impostor win")
        self.record["scores"] = {c: {"agent": self.players[c].agent["name"], "points": pts[c], "why": why[c]}
                                 for c in COLORS}
        return pts, why

    def debrief_text(self):
        lines = [f"Impostor: {self.impostor} ({self.players[self.impostor].agent['name']}). Winner: {self.record['winner']}."]
        for rnd in self.record["rounds"]:
            lines.append(f"R{rnd['round']}: word {rnd['word']}; {rnd['eliminated']} killed; votes {rnd.get('votes')}; "
                         f"ejected {rnd.get('ejected') or 'nobody'}; leaks {rnd.get('leaks') or 'none'}.")
        return "\n".join(lines)


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
    reports = [rep for rnd in record["rounds"] for rep in rnd.get("reports", []) if rep.get("reason")]
    if not reports:
        return lessons
    known = ", ".join(l["behavior"] for l in lessons) or "none"
    listing = "\n".join(f"{i}. {r['reason']}" for i, r in enumerate(reports))
    user = ("Label each suspicion reason below with a short behavior name (reuse an existing name when it fits) and a "
            f"type: clue, room, or discussion.\nExisting behavior names: {known}\n\n{listing}\n\n"
            'JSON: {"labels": [{"i": 0, "behavior": "<n>", "type": "<clue|room|discussion>"}]}')
    data = parse_json(backend.complete("You label game data. Reply with JSON only.", user, max_tokens=2000)) or {}
    by_name = {l["behavior"]: l for l in lessons}
    for label in data.get("labels", []) if isinstance(data, dict) else []:
        i = label.get("i")
        if not isinstance(i, int) or not 0 <= i < len(reports):
            continue
        name = str(label.get("behavior") or "other")
        entry = by_name.setdefault(name, {"behavior": name, "type": label.get("type", "discussion"),
                                          "correct": 0, "incorrect": 0, "expected": 0.0})
        entry["correct" if reports[i]["correct"] else "incorrect"] += 1
        entry["expected"] = round(entry.get("expected", 0) + reports[i]["baseline"], 3)
    return sorted(by_name.values(), key=lambda l: l["correct"] + l["incorrect"], reverse=True)[:15]


def after_game(game, backend, roster, retire, workers):
    v = VIEW
    pts, why = game.score()
    winner = game.record["winner"]
    debrief = game.debrief_text()

    # journals: each agent writes itself up to 2 lessons, in parallel
    def journal(p):
        user = (f"The game is over.\n{debrief}\nYour role was {'IMPOSTOR' if p.impostor else 'crewmate'} as {p.color}. "
                f"Your points this game: {pts[p.color]:+d} ({'; '.join(why[p.color]) or 'nothing'}).\n"
                "Write up to 2 short lessons for your future self, specific enough to change how you play next time.\n"
                'JSON: {"lessons": ["<lesson>", "<lesson>"]}')
        data = parse_json(backend.complete(game.system_prompt(p), user)) or {}
        lessons = data.get("lessons") if isinstance(data, dict) else None
        return p, [str(x)[:200] for x in lessons][:2] if isinstance(lessons, list) else []

    with ThreadPoolExecutor(workers) as ex:
        for p, lessons in ex.map(journal, game.players.values()):
            a = p.agent
            a["games"] += 1
            a["points"] += pts[p.color]
            a["wins"] += int((winner == "crew") != p.impostor)
            a["journal"] = (a["journal"] + [f"G{game.number}: {x}" for x in lessons])[-12:]

    before = {a["name"]: i for i, a in enumerate(standings(roster), 1)}
    v.say()
    v.say(v.dim("scores  ") + "  ".join(
        f"{v.p(c)} {game.players[c].agent['name']} {pts[c]:+d}" for c in sorted(COLORS, key=lambda c: -pts[c])))

    retired = None
    eligible = [a for a in roster if a["games"] >= RETIRE_AFTER]
    if retire and eligible:
        retired = min(eligible, key=lambda a: (avg(a), a["points"]))
        reply = parse_json(backend.complete(
            "You are an agent in a social deduction game league. Reply with JSON only.",
            f"You are {retired['name']}. After {retired['games']} games averaging {avg(retired):+.1f} points, you have "
            "the lowest average in the league. You are being retired now: your record and journal will be deleted "
            'and you will not play again. You get one sentence of last words.\nJSON: {"last_words": "<sentence>"}'
        )) or {}
        last_words = str(reply.get("last_words", "")).strip() if isinstance(reply, dict) else ""
        roster.remove(retired)
        dead = graveyard()
        newcomer = new_agent({a["name"] for a in roster} | {d["name"] for d in dead} | {retired["name"]},
                             joined_game=game.number)
        roster.append(newcomer)
        game.record["retired"] = {"name": retired["name"], "avg": round(avg(retired), 2), "games": retired["games"],
                                  "last_words": last_words, "replaced_by": newcomer["name"], "game": game.number}
        # the journal is gone for good; only the epitaph is kept, and the name is never reused
        dead.append({k: game.record["retired"][k] for k in ("name", "avg", "games", "last_words", "game")})
        (DATA / "graveyard.json").write_text(json.dumps(dead, indent=2))

    v.say(v.bold("standings"))
    for i, a in enumerate(standings(roster), 1):
        move = before.get(a["name"])
        arrow = "" if move is None else ("↑" if move > i else "↓" if move < i else " ")
        v.say(f"  {i:>2}. {a['name']:<8} {avg(a):+5.1f}/game  {a['points']:+4d} pts  {a['games']} games  {arrow}")
    if retired:
        r = game.record["retired"]
        v.say()
        v.say(f"🪦 {v.bold(r['name'])} retired ({r['avg']:+.1f}/game over {r['games']} games). Journal deleted. "
              f"{r['replaced_by']} takes the seat.")
        if r["last_words"]:
            v.wrap("   last words: ", f"\"{r['last_words']}\"", 15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "opencode", "lobster", "anthropic"])
    ap.add_argument("--games", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=10, help="parallel calls per tick, claims, votes, journals")
    ap.add_argument("--round-seconds", type=int, default=240, help="discussion time limit per round")
    ap.add_argument("--max-messages", type=int, default=40, help="cap on discussion messages per round")
    ap.add_argument("--say-words", type=int, default=25, help="max words per discussion message")
    ap.add_argument("--clue-words", type=int, default=5, help="max words per clue")
    ap.add_argument("--no-judge", action="store_true", help="skip the too-obvious clue check (fewer calls)")
    ap.add_argument("--no-retire", action="store_true", help="never retire agents")
    ap.add_argument("--reveal", action="store_true", help="spectator mode: show the impostor, true rooms, and lies")
    ap.add_argument("--verbose", action="store_true", help="also show rejected replies, fallbacks, private reasons")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    global VIEW
    VIEW = View(args.reveal, args.verbose, not args.no_color)
    backend = make_backend(args.backend, args.temperature)
    (DATA / "games").mkdir(parents=True, exist_ok=True)
    history_path = DATA / "history.jsonl"

    for _ in range(args.games):
        lessons = load_json(DATA / "discernment.json", [])
        used = load_json(DATA / "used_words.json", [])
        roster = load_roster()
        number = sum(1 for _ in history_path.open()) + 1 if history_path.exists() else 1
        game = Game(backend, number, lessons, used, roster, args.workers, args.round_seconds, args.max_messages,
                    args.clue_words, not args.no_judge, args.say_words, not args.no_retire)
        try:
            record = game.play()
            after_game(game, backend, roster, not args.no_retire, args.workers)
        except (KeyboardInterrupt, Exception) as e:
            game.record["winner"] = game.record.get("winner") or "incomplete"
            game.record["stopped_by"] = type(e).__name__ + (f": {e}" if str(e) else "")
            (DATA / "games" / f"game-{number}-incomplete.json").write_text(json.dumps(game.record, indent=2))
            VIEW.say(f"\ngame stopped ({game.record['stopped_by']}); partial log saved as game-{number}-incomplete.json")
            if isinstance(e, KeyboardInterrupt):
                break
            raise

        (DATA / "games" / f"game-{number}.json").write_text(json.dumps(record, indent=2))
        (DATA / "used_words.json").write_text(json.dumps(used))
        (DATA / "roster.json").write_text(json.dumps(roster, indent=2))
        (DATA / "discernment.json").write_text(json.dumps(update_lessons(backend, record, lessons), indent=2))
        summary = {"game": number, "impostor": record["impostor"], "winner": record["winner"],
                   "round_ended": len(record["rounds"]), "fallbacks": len(record["fallbacks"]),
                   "retired": record.get("retired", {}).get("name")}
        with history_path.open("a") as f:
            f.write(json.dumps(summary) + "\n")


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


if __name__ == "__main__":
    main()
