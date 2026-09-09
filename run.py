#!/usr/bin/env python3
"""AutoShorts launcher: `python run.py [--host 0.0.0.0] [--port 8000] [--demo]`"""
from __future__ import annotations

import argparse
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoShorts server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--demo", action="store_true",
        help="pre-load the demo playlist on startup",
    )
    args = parser.parse_args()

    import uvicorn

    if args.demo:
        from autoshorts.server import store
        from autoshorts import demo

        for ep in demo.DEMO_EPISODES:
            store.upsert_episode(
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

    print(f"AutoShorts UI → http://{args.host}:{args.port}")
    uvicorn.run(
        "autoshorts.server:app", host=args.host, port=args.port, log_level="info"
    )


if __name__ == "__main__":
    main()
