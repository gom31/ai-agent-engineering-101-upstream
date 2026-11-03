"""One 2-hour slice of the outer runtime (HARNESS.md 2.3, 5): market analyst -> self analyst -> strategy, then the
audit in its own process (outer.audit).

    py -m outer.slice            # run one slice now

Each step is one agents.loop turn of its role, ending with its rec.* tool. A step that fails is recorded and
the next one still runs with what there is; the audit never blocks the brief: the slice starts it and goes on. At night (NIGHT hours) the
brief is a report only. The slice's window starts where the last slice ended (memory/facts/slice_state.json).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agents import loop
from core import atomic, bus, paths
from core.events import EventWriter
from outer import audit, brief
from tools.registry import Ctx, call

CODE = Path(__file__).resolve().parent.parent   # the code; HARNESS_ROOT (the records) may be elsewhere
NIGHT = range(1, 8)            # 01:00-07:59 (HARNESS.md 5)
PROFILES_PER_SLICE = 6


def _j(x, n=1500):
    return json.dumps(x, ensure_ascii=False, default=str)[:n]


def market_text(ctx, since):
    live = atomic.read_json(paths.memory() / "facts" / "live.json", default={}) or {}
    state = atomic.read_json(paths.memory() / "facts" / "collect_state.json", default={}) or {}
    shops = sorted((state.get("shops") or {}).items(), key=lambda x: -(x[1].get("deals_seen") or 0))[:20]
    customers = sorted((live.get("customer") or {}).items(), key=lambda x: -(x[1].get("deals_with_us") or 0))[:15]
    return "\n".join([
        "# the analysis at hand",
        f"Slice {ctx.situation['slice']} (since {time.strftime('%m-%d %H:%M', time.localtime(since))}).",
        f"Selling in the last {live.get('window_hours')} h: {_j(live.get('selling'), 1200)}",
        f"Participants: {_j(live.get('participants'), 400)}",
        f"Most active shops (deals seen, stars): {_j(shops, 1500)}",
        f"Our customers: {_j(customers, 1000)}",
        f"You have {loop.ROLES['market analyst']['steps']} steps. "
        f"Write up to {PROFILES_PER_SLICE} profiles (rec.profile) for the participants that matter most this slice "
        "(new, changed, or dealing with us), then end with rec.summary: what sells, who does what, who changed."])


def self_text(ctx, since):
    return "\n".join([
        "# the analysis at hand",
        f"Slice {ctx.situation['slice']}: evaluate our shop, our buyer and each inner agent since {since:.0f} "
        "(pass since to the eval.* tools). Say where we lost money, deals or votes and why, with evidence, "
        "then end with rec.summary."])


def strategy_text(ctx):
    s = ctx.strategy
    pending = paths.memory() / "strategy" / "counterparts_pending.jsonl"
    lines = ["# the brief at hand",
             f"Strategy in force: v{s.get('version')} ({s.get('epoch')}), phase {s.get('phase')}, approved {s.get('approved')!r}.",
             f"Experiment: {_j(s.get('experiment'), 800)}",
             f"Delegation: {_j(s.get('delegation'), 300)}"]
    for kind in ("market", "self"):
        r = call(ctx, "memory.summary", {"kind": kind})
        lines.append(f"This slice's {kind} summary: {_j(r['result'] if r['ok'] else r['reason'], 2500)}")
    if pending.exists():
        lines.append("Stances waiting for the user: " + _j(pending.read_text(encoding="utf-8").splitlines()[-10:], 1000))
    lines.append("It is night: write a report only (no options)." if ctx.situation["night"]
                 else "Write the brief with rec.brief: 2-3 options and a recommendation if something should change, "
                      "else no options.")
    return "\n".join(lines)


def run(now=None, model_call=None):
    now = now or time.time()
    st_path = paths.memory() / "facts" / "slice_state.json"
    st = atomic.read_json(st_path, default=None) or {"last": now - 7200}
    tm = time.localtime(now)
    slice_id = time.strftime("%Y-%m-%dT%H", tm)
    tool_w, w = EventWriter("outer", source="tools"), EventWriter("slice", source="outer")
    out = {"slice": slice_id, "steps": {}}
    try:
        base = Ctx(caller="outer", _writer=tool_w)
        epoch = base.strategy.get("epoch") or "e0"
        w.context(strategy_version=base.strategy.get("version"), epoch=epoch)
        common = {"epoch": epoch, "slice": slice_id, "night": tm.tm_hour in NIGHT, "proposal_id": slice_id}

        def step(role, caller, text_fn, extra=None):
            ctx = Ctx(caller=caller, _writer=tool_w)
            ctx.situation = dict(common, **(extra or {}))
            t0 = time.time()
            try:
                d, trace = loop.decide(role, ctx, text_fn(ctx), model_call=model_call)
            except Exception as e:                       # one broken step does not stop the slice
                d, trace = None, [{"observation": f"{type(e).__name__}: {e}"}]
            res = {"ok": d is not None, "s": round(time.time() - t0), "steps": len(trace)}
            if d is None:
                res["why"] = str(trace[-1]["observation"])[:200] if trace else "no answer"
            out["steps"][role] = res
            w.emit("slice.step", {"role": role, **res}, )
            return d

        step("market analyst", "market-analyst", lambda c: market_text(c, st["last"]), {"kind": "market"})
        step("self analyst", "self-analyst", lambda c: self_text(c, st["last"]), {"kind": "self"})
        d = step("strategy", "strategy", strategy_text)
        doc = atomic.read_json(paths.memory() / "strategy" / "proposals" / f"{slice_id}.json", default=None)
        if d and doc:
            if doc.get("options"):
                w.emit("strategy.proposal", {"proposal_id": slice_id, "version_from": doc.get("version_from"),
                                             "options": [o["id"] for o in doc["options"]]})
            pub = atomic.read_json(brief.briefs_dir() / f"{slice_id}.json", default=None)
            if pub:
                brief.write_markdown(pub)
            brief.refresh_pending()
            if model_call is not None:                   # tests: the audit in this process, with the same script
                out["steps"]["audit"] = {"ok": audit.run(slice_id, model_call=model_call) is not None}
            else:                                        # its own process; the slice does not wait for it
                subprocess.Popen([sys.executable, "-m", "outer.audit", slice_id], cwd=CODE,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                                    else {"start_new_session": True}))
                out["steps"]["audit"] = {"started": True}
        else:
            bus.alert("slice", "warn", "no_brief", f"slice {slice_id}: the strategy agent wrote no brief", out["steps"])
        atomic.write_json(st_path, {"last": now, "slice": slice_id})
        w.emit("slice.done", out)
    finally:
        w.close()
        tool_w.close()
    return out


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
