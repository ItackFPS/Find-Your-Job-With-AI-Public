"""``python -m src.launcher [start|dev|status|stop|shortcut]`` — see ``--help``."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from src.launcher import core
from src.launcher.run import Launcher, Output, cmd_shortcut, cmd_status, cmd_stop

COMMANDS = ("start", "dev", "status", "stop", "shortcut")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, help="backend port (default: FYJ_PORT or 8000)")

    run = argparse.ArgumentParser(add_help=False, parents=[common])
    run.add_argument("--n8n", action="store_true",
                     help="also start n8n (weekly e-mail digest) if it is installed and not running")
    run.add_argument("--window", choices=("app", "browser", "none"), default="app",
                     help="open the dashboard in an app window (Edge/Chrome; falls back to the "
                          "default browser), the default browser, or not at all (default: app)")
    run.add_argument("--timeout", type=float, default=180.0,
                     help="seconds to wait for the backend to answer (default: 180)")

    p = argparse.ArgumentParser(
        prog="FindYourJob.cmd",
        description="Run Find Your Job With AI (backend + dashboard) from one window. "
                    "It stops only what it started. Without a command: start.",
    )
    sub = p.add_subparsers(dest="command")
    sub.add_parser("start", parents=[run],
                   help="build the dashboard if needed, start the backend, open the dashboard")
    dev = sub.add_parser("dev", parents=[run], help="backend + Vite dev server with hot reload")
    dev.add_argument("--reload", action="store_true",
                     help="also restart the backend on code changes (interrupts running scans)")
    sub.add_parser("status", parents=[common], help="show what is running; starts nothing")
    sub.add_parser("stop", parents=[common], help="ask the running launcher to stop what it started")
    sc = sub.add_parser("shortcut", parents=[common],
                        help="create (or --remove) a shortcut for your Windows account")
    sc.add_argument("--where", choices=("start-menu", "desktop"), default="start-menu")
    sc.add_argument("--remove", action="store_true")
    return p


def parse(argv: list[str]) -> argparse.Namespace:
    if not any(a in COMMANDS for a in argv) and not {"-h", "--help"} & set(argv):
        argv = ["start", *argv]
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(list(sys.argv[1:] if argv is None else argv))
    root = core.project_root()
    os.chdir(root)  # the backend resolves logs/, data/ and config/ from here
    settings = core.Settings.load(root, backend_port=args.port)
    if args.command in ("start", "dev"):
        settings = dataclasses.replace(settings, startup_timeout_s=args.timeout)
        out = Output(settings.logs_dir / "launcher.log")
        return Launcher(settings, out, mode=args.command, with_n8n=args.n8n, window=args.window,
                        reload=getattr(args, "reload", False)).run()
    out = Output(None)
    if args.command == "status":
        return cmd_status(settings, out)
    if args.command == "stop":
        return cmd_stop(settings, out)
    return cmd_shortcut(settings, out, args.where, args.remove)


if __name__ == "__main__":
    sys.exit(main())
