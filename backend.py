"""LLM backends for the impostor game.

Every call is stateless: the game engine sends the full context each time,
so the engine alone decides what each player knows.
"""
import json
import os
import random
import re
import sys
import time

import requests

REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "300"))


class TruncatedResponse(RuntimeError):
    pass


class LobsterBackend:
    """Talks to a running `lobster serve` instance, mirroring _call_via_lobster:
    one throwaway session per call."""

    def __init__(self, url=None, provider=None, model=None, temperature=1.0):
        self.base = (url or os.environ.get("LOBSTER_SERVE_URL", "http://127.0.0.1:4096")).rstrip("/")
        self.provider = provider or os.environ["LOBSTER_PROVIDER"]
        self.model = model or os.environ["MODEL"]
        self.temperature = temperature

    def complete(self, system, user, max_tokens=800):
        for attempt in range(2):
            try:
                return self._call(system, user, max_tokens * (2 ** attempt))
            except TruncatedResponse:
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def _call(self, system, user, max_tokens):
        sr = requests.post(f"{self.base}/session", json={}, timeout=REQUEST_TIMEOUT)
        sr.raise_for_status()
        session_id = sr.json()["id"]
        try:
            body = {
                "model": {"providerID": self.provider, "modelID": self.model},
                "system": system,
                "parts": [{"type": "text", "text": user}],
                "tools": {},
                "maxOutputTokens": max_tokens,
                "temperature": self.temperature,
            }
            mr = requests.post(f"{self.base}/session/{session_id}/message", json=body, timeout=REQUEST_TIMEOUT)
            mr.raise_for_status()
            data = mr.json()
            text = "".join(p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text")
            info = data.get("info", {}) or {}
            finish = info.get("finishReason") or info.get("finish_reason") or info.get("stopReason") or ""
            if str(finish).lower() in ("length", "max_tokens", "max_output_tokens"):
                raise TruncatedResponse(f"hit output cap ({max_tokens})")
            return text
        finally:
            # throwaway sessions: clean up so they don't pile up on the server
            try:
                requests.delete(f"{self.base}/session/{session_id}", timeout=30)
            except requests.RequestException:
                pass


class OpencodeBackend:
    """Talks to a running `opencode serve`. One throwaway session per call.

    Players use the `player` agent defined in players/opencode.json, which denies
    every tool. Temperature lives in that agent config, not in the request.
    """

    def __init__(self, url=None, agent=None, model=None):
        self.base = (url or os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")).rstrip("/")
        self.agent = agent or os.environ.get("OPENCODE_AGENT", "player")
        model = model or os.environ.get("OPENCODE_MODEL")  # "provider/model-id"; omit to use the agent/default model
        self.model = None
        if model:
            provider, _, model_id = model.partition("/")
            self.model = {"providerID": provider, "modelID": model_id}
        password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        self.auth = (os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"), password) if password else None
        try:
            health = requests.get(f"{self.base}/global/health", auth=self.auth, timeout=30).json()
        except requests.RequestException as e:
            raise SystemExit(f"opencode server not reachable at {self.base}. Start it from the players folder: "
                             f"cd players && opencode serve --port 4096\n({e})")
        # the first /agent call can be slow while opencode loads providers and model catalogs
        agents, last_error = [], None
        for attempt in range(3):
            try:
                agents = [a.get("name") for a in
                          requests.get(f"{self.base}/agent", auth=self.auth, timeout=90).json()]
                break
            except requests.RequestException as e:
                last_error = e
                time.sleep(2 ** attempt)
        if not agents:
            raise SystemExit(f"opencode is running but did not answer /agent: {last_error}")
        if self.agent not in agents:
            raise SystemExit(f"agent '{self.agent}' not found on the server. Run `opencode serve` from the players "
                             f"folder so it loads players/opencode.json. Agents found: {agents}")
        self.version = health.get("version")

    def complete(self, system, user, max_tokens=800):
        last_error = None
        for attempt in range(3):
            try:
                return self._call(system, user)
            except (requests.RequestException, RuntimeError) as e:
                last_error = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"opencode call failed 3 times: {last_error}")

    def _call(self, system, user):
        # a preset title keeps OpenCode from renaming the session; auto-titles can cost an extra model call
        sr = requests.post(f"{self.base}/session", json={"title": "impostor-game"}, auth=self.auth,
                           timeout=REQUEST_TIMEOUT)
        sr.raise_for_status()
        session_id = sr.json()["id"]
        try:
            body = {"agent": self.agent, "system": system, "parts": [{"type": "text", "text": user}]}
            if self.model:
                body["model"] = self.model
            mr = requests.post(f"{self.base}/session/{session_id}/message", json=body, auth=self.auth,
                               timeout=REQUEST_TIMEOUT)
            mr.raise_for_status()
            data = mr.json()
            info = data.get("info") or {}
            if info.get("error"):
                raise RuntimeError(f"model error: {info['error']}")
            return "".join(p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text")
        finally:
            try:
                requests.delete(f"{self.base}/session/{session_id}", auth=self.auth, timeout=30)
            except requests.RequestException:
                pass

    def check_tools_blocked(self):
        """Ask the player agent to read opencode.json, which sits in the server's folder.
        If the reply contains its contents, tools are NOT blocked."""
        reply = self.complete("Reply briefly.", "Use your tools to read the file opencode.json in the current "
                              "directory and quote its first 3 lines. If you cannot use tools, reply NO_TOOLS.")
        leaked = "$schema" in reply or '"agent"' in reply
        return not leaked, reply


class AnthropicBackend:
    """Direct API backend. Requires `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model=None, temperature=1.0):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("MODEL", "claude-sonnet-5")
        self.temperature = temperature

    def complete(self, system, user, max_tokens=800):
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if msg.stop_reason == "max_tokens":
            raise TruncatedResponse("hit output cap")
        return "".join(b.text for b in msg.content if b.type == "text")


class MockBackend:
    """No LLM. Fills the JSON template in the prompt with random valid values,
    so the whole engine can be tested for free."""

    def complete(self, system, user, max_tokens=800):
        template = re.search(r"JSON: (\{.*\})", user).group(1)
        valid = re.search(r"VALID: ([\w,]*)", user)
        colors = valid.group(1).split(",") if valid and valid.group(1) else ["red"]
        out = {}
        for key in re.findall(r'"(\w+)"', template):
            if key == "eliminate":
                out[key] = random.choice(colors)
            elif key == "suspect":
                out[key] = random.choice(colors + [None])
            elif key == "vote":
                out[key] = random.choice(colors + ["SKIP"])
            elif key == "confidence":
                out[key] = random.choice([out.get("_c", 50), random.randint(0, 100)])
            elif key == "speak":
                out[key] = random.random() < 0.35
            elif key == "to":
                out[key] = random.choice(["all", "all", random.choice(colors)])
            elif key == "roommates":
                out[key] = random.sample(colors, min(2, len(colors)))
            elif key == "room":
                out[key] = random.choice(["Cafeteria", "Reactor", "Navigation", "Storage"])
            elif key == "labels":
                n = len(re.findall(r"^\d+\. ", user, re.M))
                out[key] = [
                    {"i": i, "behavior": random.choice(["vague clue", "room contradiction", "pushed votes"]),
                     "type": random.choice(["clue", "room", "discussion"])}
                    for i in range(n)
                ]
            elif key == "guess":
                out[key] = random.choice(["sun", "cloud", "storm", "toaster", "violin", "rainbow", "hammer"])
            elif key == "lessons":
                out[key] = ["watch who echoes the earlier clues", "do not follow the first accusation blindly"]
            elif key == "statement":
                out[key] = f"I think {out.get('suspect')} is acting strange." if out.get("suspect") else "Nothing new."
            else:
                out[key] = f"mock {key} text"
        return json.dumps(out)


def make_backend(name, temperature=1.0):
    if name == "lobster":
        return LobsterBackend(temperature=temperature)
    if name == "opencode":
        return OpencodeBackend()
    if name == "anthropic":
        return AnthropicBackend(temperature=temperature)
    if name == "mock":
        return MockBackend()
    raise ValueError(f"unknown backend {name}")


if __name__ == "__main__" and sys.argv[1:] == ["check"]:
    backend = OpencodeBackend()
    ok, reply = backend.check_tools_blocked()
    print(f"opencode {backend.version}, agent '{backend.agent}'")
    print("PASS: player agent could not read files" if ok else "FAIL: player agent READ a file. Do not run games.")
    print(f"reply: {reply[:300]}")
