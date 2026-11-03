"""The event kinds and their data fields: the contract between the agents that write events and the
evaluation tools that read them (HARNESS.md 3.3, 8.1). Writers may add fields; these are the ones read.

Keys (task, order, unit, sku, ledger_seq, counterpart) go in the event's keys, not in data.
"""
KINDS = {
    # seller (our shop)
    "seller.request": "a buyer message arrived. data: excerpt, looks_like_instruction",
    "seller.decision": "one decision. data: action (counter|accept|reject), price, ms, model, tool_calls, guard (str|None)",
    "seller.closed": "a negotiation ended. data: outcome (completed|rejected|canceled|failed), price, cost, turns, "
                     "claim, warranty_hours, cert, below_cost (bool)",
    "seller.settled": "the market settled our sale. data: price, cost, ledger",
    "seller.refund": "a warranty refund left our balance. data: amount",
    # buyer (our buyer)
    "buyer.order": "an order arrived. data: category, budget, kind, use_within_hours, acceptable_n",
    "buyer.pick": "a shop was picked. data: shop, why, explored (bool: a shop we had not traded with)",
    "buyer.decision": "one move in a negotiation. data: action (offer|walk_away), price, tool_calls, thought",
    "buyer.closed": "a negotiation ended. data: outcome (completed|walked|rejected|expired|failed), agreed, "
                    "receipt_price, verdict (fair|cheated: ...|no deal ...), list_price",
    "buyer.payment": "the kit's payment result. data: paid (bool), price, reason, ledger",
    "buyer.received": "what a settled purchase was worth. data: value, price, grade, claim_honest, surplus",
    "buyer.broken": "goods we bought broke. data: price, warranty (bool), refund, matched (bool)",
    # restock
    "restock.plan": "a plan was made. data: lines, refused (reason|None), used_model (bool)",
    "restock.bought": "units arrived. data: qty, cost_total, kind",
    # losses of our shop
    "loss.expired": "a food unit was thrown away. data: cost",
    "loss.storage": "storage paid. data: amount",
    # review and forum
    "review.written": "we reviewed a deal. data: stars, fact_stars, why",
    "forum.posted": "we posted. data: as (shop|buyer), refs_valid (bool), post_id",
    "forum.votes": "votes our posts received, counted. data: post_id, likes, dislikes",
    # model calls (every seat)
    "model.call": "one model call. data: seat, model, ms, ok, rate_limited, fallback, prompt_chars",
    # counterpart guesses
    "guess.made": "data: task, top (name|None), confidence",
    "guess.confirmed": "the ledger said who it was. data: task, actual",
    # strategy loop
    "strategy.proposal": "data: proposal_id, version_from",
    "strategy.decision": "the user answered. data: proposal_id, accepted (bool)",
    "audit.opinion": "data: proposal_id, agrees (bool)",
    "audit.outcome": "later: who was right. data: proposal_id, audit_right (bool)",
}

SOURCES = ("seller", "buyer", "restock", "review", "forum", "models", "outer", "tools")
