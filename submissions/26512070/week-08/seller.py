"""Our shop's seller behind A2A v1.0 (the kit starts it: "seller": {"cmd": "py seller.py --port {port}"}).

This file only speaks A2A. Every move comes from agents.seller_core, which asks the model through
agents.loop with the seller's tools. The kit gives MARKET_DIR (inventory, catalog, demand) and makes
shop_key.pem; the card's name and description are in config/shop.json.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part  # noqa: E402
from a2a.server.agent_execution import AgentExecutor  # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes  # noqa: E402
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater  # noqa: E402
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402

from agents.seller_core import SellerCore, load_key  # noqa: E402

SHOP = json.loads((HERE / "config" / "shop.json").read_text(encoding="utf-8"))
TURN_SECONDS = 50          # one reply; buyers wait for it


class Seller(AgentExecutor):
    def __init__(self, core):
        self.core = core

    async def execute(self, context, event_queue):
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text or "...")])
        text = get_message_text(context.message) or ""
        try:
            out = await asyncio.to_thread(self.core.handle, task.id, text, time.time() + TURN_SECONDS)
        except Exception as e:                       # a broken turn ends this negotiation, not the shop
            print(f"[seller] turn failed: {type(e).__name__}: {e}", flush=True)
            self.core.cancel(task.id)
            await up.reject(say("Sorry, something went wrong on our side."))
            return
        print(f"[seller] {task.id[:8]} {out['state']}: {out['say'][:120]}", flush=True)
        if out["state"] == "completed":
            await up.add_artifact(parts=[new_data_part(out["receipt"])], name="receipt")
            await up.complete(say(out["say"]))
        elif out["state"] == "rejected":
            await up.reject(say(out["say"]))
        else:
            await up.requires_input(say(out["say"]))

    async def cancel(self, context, event_queue):
        task = context.current_task
        self.core.cancel(task.id)
        await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build(port, core):
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=SHOP["name"], description=SHOP["description"], version="1.0.0",
        default_input_modes=["text/plain"], default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[AgentSkill(id="negotiate", name="Negotiate a purchase",
                           description="Ask for an item by SKU, category or name, then send a price. The shop answers "
                                       "with a counteroffer or a signed receipt.",
                           tags=["shopping", "negotiation"], examples=SHOP["examples"])])
    handler = DefaultRequestHandler(agent_executor=Seller(core), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9301)
    ap.add_argument("--key", default=str(HERE / "shop_key.pem"))
    a = ap.parse_args()
    core = SellerCore(SHOP["name"], load_key(a.key))
    uvicorn.run(build(a.port, core), host="127.0.0.1", port=a.port, log_level="warning")
