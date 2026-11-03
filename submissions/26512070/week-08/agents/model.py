"""The one way any agent calls a model (HARNESS.md 4, 7: D22 seats, S3 circuit breaker).

    py -m agents.model seller "say only: pong"      # one call through the seller's seat

A seat (seller, buyer, restock, review, forum, analyst, strategy, audit) has a Claude Code model and,
for L1 seats, an OpenRouter fallback (SEATS below; the OpenRouter model is a placeholder until D29).

- Claude Code runs as `claude -p --model <m> --tools "" --setting-sources ""` in an empty folder, so it
  reads no project instructions and has no built-in tools: our tools live in our own loop.
- Circuit breaker (S3): after BREAK_AFTER rate-limit failures in a row on a seat, the seat stops calling
  Claude Code for BREAK_SECONDS and L1 seats go to OpenRouter. When the time is up, one trial call
  decides: success closes the breaker. The state is a file per seat in memory/bus/breaker/, because
  market_buyer, restock and review are new processes each time.
- Every call is one model.call event (seat, model, ms, ok, rate_limited, fallback, prompt_chars).
  The OpenRouter key is read from OPENROUTER_API_KEY and never written anywhere.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from agents import slots
from core import atomic, bus, paths
from core.events import EventWriter

SEATS = {
    "seller": {"claude": "haiku", "fallback": "openai/gpt-4o-mini"},
    "buyer": {"claude": "haiku", "fallback": "openai/gpt-4o-mini"},
    "restock": {"claude": "haiku", "fallback": "openai/gpt-4o-mini"},
    "review": {"claude": "haiku", "fallback": "openai/gpt-4o-mini"},
    "forum": {"claude": "sonnet", "fallback": "openai/gpt-4o-mini"},
    "analyst": {"claude": "sonnet", "fallback": None},
    "strategy": {"claude": "opus", "fallback": None},
    "audit": {"claude": None, "fallback": "google/gemini-2.5-flash"},  # another vendor's model (D29 draft)
}
SLOT_SEAT = {}                 # seat -> slot priority name, when they differ (none yet)
BREAK_AFTER = 3
BREAK_SECONDS = 300
CALL_TIMEOUT = 60
RATE_WORDS = ("rate limit", "rate_limit", "usage limit", "429", "overloaded", "quota")
_EMPTY = None
_writer = None


class ModelError(Exception):
    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


def _empty_dir():
    global _EMPTY
    if _EMPTY is None:
        _EMPTY = tempfile.mkdtemp(prefix="harness-model-")
    return _EMPTY


def _claude_exe():
    found = shutil.which("claude")
    if found:
        return found
    fallback = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    if os.path.exists(fallback):
        return fallback
    raise ModelError("claude CLI not found on PATH or in ~/.local/bin")


def claude_code(model, system, prompt, timeout):
    cmd = [_claude_exe(), "-p", "--model", model, "--tools", "", "--setting-sources", "", "--no-session-persistence"]
    if system:
        cmd += ["--system-prompt", system]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=_empty_dir(),
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ModelError(f"claude did not answer within {timeout} s")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        msg = ((r.stderr or "") + " " + out).strip()[:300]
        raise ModelError(f"claude exited {r.returncode}: {msg}", any(w in msg.lower() for w in RATE_WORDS))
    if any(w in out.lower()[:200] for w in ("usage limit", "rate limit")) and len(out) < 300:
        raise ModelError(f"claude: {out[:200]}", True)        # the limit notice comes back as the answer text
    return out


def openrouter(model, system, prompt, timeout):
    import httpx
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ModelError("OPENROUTER_API_KEY is not set")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    try:
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"}, json={"model": model, "messages": msgs})
    except httpx.HTTPError as e:
        raise ModelError(f"openrouter: {type(e).__name__}")
    if r.status_code != 200:
        raise ModelError(f"openrouter {r.status_code}: {r.text[:200]}", r.status_code == 429)
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError, AttributeError):
        raise ModelError(f"openrouter: unexpected answer {r.text[:200]}")


BACKENDS = {"claude": claude_code, "openrouter": openrouter}


def _breaker_path(seat):
    return paths.bus() / "breaker" / f"{seat}.json"


def breaker(seat):
    try:
        return atomic.read_json(_breaker_path(seat), default=None) or {"fails": 0, "open_until": 0}
    except ValueError:
        return {"fails": 0, "open_until": 0}


def _set_breaker(seat, st):
    atomic.write_json(_breaker_path(seat), st)


def _emit(**data):
    global _writer
    if _writer is None:
        _writer = EventWriter(f"models-{os.getpid()}", source="models")
    _writer.emit("model.call", data)


def call(seat, prompt, system=None, timeout=CALL_TIMEOUT, now=time.time):
    """The model's text answer for this seat. Raises ModelError when no backend answered."""
    cfg = SEATS[seat]
    st = breaker(seat)
    use_claude = cfg["claude"] is not None and st["open_until"] <= now()
    tries = []
    if use_claude:
        tries.append(("claude", cfg["claude"], False))
    if cfg["fallback"] and (not use_claude or seat in ("seller", "buyer", "restock", "review", "forum")):
        tries.append(("openrouter", cfg["fallback"], use_claude))     # a fallback after a failed Claude call
    if not tries:
        raise ModelError(f"seat {seat}: Claude Code is paused by the breaker until {st['open_until']:.0f}")
    last = None
    for backend, model, is_fallback in tries:
        if is_fallback and last is not None and not last.rate_limited:
            break                                       # only a rate limit sends an L1 call elsewhere
        t0 = time.perf_counter()
        waited = 0.0
        try:
            if backend == "claude":                     # Claude Code calls share SLOTS across all processes
                with slots.slot(SLOT_SEAT.get(seat, seat)) as waited:
                    out = BACKENDS[backend](model, system, prompt, timeout)
            else:
                out = BACKENDS[backend](model, system, prompt, timeout)
        except slots.Busy as e:
            last = ModelError(str(e), rate_limited=True)     # like a rate limit: L1 goes to the fallback
            _emit(seat=seat, model=f"{backend}:{model}", ms=round((time.perf_counter() - t0) * 1000), ok=False,
                  rate_limited=False, busy=True, fallback=False, prompt_chars=len(prompt) + len(system or ""),
                  error=str(e)[:200])
            continue
        except ModelError as e:
            last = e
            _emit(seat=seat, model=f"{backend}:{model}", ms=round((time.perf_counter() - t0) * 1000), ok=False,
                  rate_limited=e.rate_limited, fallback=backend != "claude", prompt_chars=len(prompt) + len(system or ""),
                  error=str(e)[:200])
            if backend == "claude":
                _claude_failed(seat, st, e, now)
            continue
        _emit(seat=seat, model=f"{backend}:{model}", ms=round((time.perf_counter() - t0) * 1000), ok=True,
              rate_limited=False, fallback=backend != "claude", prompt_chars=len(prompt) + len(system or ""),
              waited_ms=round(waited * 1000))
        if backend == "claude" and (st["fails"] or st["open_until"]):
            if st["open_until"]:
                bus.alert(f"model-{seat}", "info", "breaker", f"{seat}: Claude Code answers again, breaker closed")
            _set_breaker(seat, {"fails": 0, "open_until": 0})
        return out
    raise last or ModelError(f"seat {seat}: no backend answered")


def _claude_failed(seat, st, e, now):
    if not e.rate_limited:
        return
    fails = st["fails"] + 1
    new = {"fails": fails, "open_until": st["open_until"]}
    if fails >= BREAK_AFTER:
        new["open_until"] = now() + BREAK_SECONDS
        bus.alert(f"model-{seat}", "warn", "breaker",
                  f"{seat}: {fails} rate limits in a row, Claude Code paused for {BREAK_SECONDS} s", {"fails": fails})
    _set_breaker(seat, new)
    st.update(new)


if __name__ == "__main__":
    print(call(sys.argv[1], " ".join(sys.argv[2:]) or "say only: pong"))
