"""Castor CLI — command-line interface for agent management."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="castor",
        description="Castor: secure microkernel for LLM agents",
    )
    parser.add_argument(
        "--db",
        default="castor.db",
        help="SQLite database path (default: castor.db)",
    )

    sub = parser.add_subparsers(dest="command")

    # Process commands
    sub.add_parser("ps", help="List agent processes")

    inspect_p = sub.add_parser("inspect", help="Inspect a checkpoint")
    inspect_p.add_argument("pid", help="Agent PID")

    # HITL commands
    approve_p = sub.add_parser("approve", help="Approve pending HITL")
    approve_p.add_argument("pid", help="Agent PID")

    reject_p = sub.add_parser("reject", help="Reject pending HITL")
    reject_p.add_argument("pid", help="Agent PID")
    reject_p.add_argument("--reason", required=True, help="Rejection reason")

    modify_p = sub.add_parser("modify", help="Modify pending HITL with feedback")
    modify_p.add_argument("pid", help="Agent PID")
    modify_p.add_argument("--feedback", required=True, help="Modification feedback")

    # Run command
    run_p = sub.add_parser("run", help="Run an agent")
    run_p.add_argument(
        "agent",
        help="Agent module path (e.g. agent.py or agent.py:func)",
    )
    run_p.add_argument(
        "--budget",
        action="append",
        help="Budget as key=value (repeatable)",
    )
    run_p.add_argument(
        "--hitl",
        choices=["auto", "interactive"],
        default="auto",
        help="HITL policy",
    )
    run_p.add_argument(
        "--store",
        help="Checkpoint store URI (default: sqlite:///castor.db)",
    )

    # Resume command
    resume_p = sub.add_parser("resume", help="Resume agent from checkpoint")
    resume_p.add_argument("pid", help="Agent PID")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    from castor.scheduler.persistence import CheckpointStore

    # Commands that need a store
    if args.command in ("ps", "inspect", "approve", "reject", "modify", "resume"):
        store = CheckpointStore(f"sqlite:///{args.db}")
        if args.command == "ps":
            from castor.cli.process import cmd_ps

            cmd_ps(store)
        elif args.command == "inspect":
            from castor.cli.process import cmd_inspect

            cmd_inspect(store, args.pid)
        elif args.command == "reject":
            from castor.cli.hitl import cmd_reject

            cmd_reject(store, args.pid, args.reason)
        elif args.command == "modify":
            from castor.cli.hitl import cmd_modify

            cmd_modify(store, args.pid, args.feedback)
        elif args.command == "approve":
            print(
                "Error: approve requires runtime — use the host application.",
                file=sys.stderr,
            )
            sys.exit(1)
        elif args.command == "resume":
            print("Error: resume not yet implemented.", file=sys.stderr)
            sys.exit(1)
    elif args.command == "run":
        from castor.cli.run import cmd_run

        cmd_run(args)


if __name__ == "__main__":
    main()
