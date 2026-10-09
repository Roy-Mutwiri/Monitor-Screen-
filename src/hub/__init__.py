"""Central hub for the multi-PC Studio monitor fleet.

FastAPI + SQLAlchemy. Agents enroll with a single-use pairing code, then send
contract events (at-least-once, deduplicated by ``event_id``) and heartbeats
with a per-device secret. The hub mirrors incidents, marks devices
unreachable when heartbeats stop, routes Telegram notifications for managed
devices, and serves a small dashboard. Telegram tokens live only in the hub's
environment; they are never sent to agents.
"""
__version__ = "0.1.0"
