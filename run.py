#!/usr/bin/env python3
"""Qyro launcher: `python run.py [--host 0.0.0.0] [--port 8000] [--demo]`"""
from __future__ import annotations

import argparse
import importlib


def _pick_server(mode: str) -> str:
    """Resolve 'auto' -> 'fastapi' when installed, else the stdlib server."""
    if mode in ("fastapi", "stdlib"):
        return mode
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return "stdlib"
    return "fastapi"


def main() -> None:
    from autoshorts import __version__

    parser = argparse.ArgumentParser(
        description=f"Qyro {__version__} server"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--demo", action="store_true",
        help="pre-load the demo playlist on startup",
    )
    parser.add_argument(
        "--server", choices=("auto", "fastapi", "stdlib"), default="auto",
        help="web server backend (auto prefers FastAPI when installed, "
        "otherwise uses the built-in pure-Python server)",
    )
    args = parser.parse_args()

    backend = _pick_server(args.server)

    if args.demo:
        server_mod = importlib.import_module(
            "autoshorts.server" if backend == "fastapi"
            else "autoshorts.server_stdlib"
        )
        from autoshorts import demo

        for ep in demo.DEMO_EPISODES:
            server_mod.store.upsert_episode(
                {
                    "id": ep["id"],
                    "title": ep["title"],
                    "url": ep["url"],
                    "duration": ep["duration"],
                    "source": "demo",
                    "status": "new",
                    "clips": [],
                    "added_at": __import__("time").time(),
                }
            )
        print(f"Demo playlist pre-loaded ({len(demo.DEMO_EPISODES)} episodes).")

    if backend == "fastapi":
        import uvicorn

        print(f"Qyro UI → http://{args.host}:{args.port}")
        uvicorn.run(
            "autoshorts.server:app", host=args.host, port=args.port, log_level="info"
        )
    else:
        if args.server == "auto":
            print("(FastAPI not installed — using the built-in Python server. "
                  "Same app, zero extra dependencies.)")
        from autoshorts.server_stdlib import serve

        serve(args.host, args.port)


if __name__ == "__main__":
    main()
