#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator
Phase 2 implementation pending.

This stub accepts the same CLI interface as the final pipeline so that
the Docker image built in Phase 1 can be tested end-to-end before the
core processing modules are written.
"""
import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="profanity-hush — automated movie profanity censoring",
    )
    parser.add_argument(
        "input_video",
        help="Path to the source video file (inside the container, e.g. /input/movie.mkv)",
    )
    parser.add_argument(
        "subtitle_file",
        nargs="?",
        default=None,
        help="Optional SRT subtitle file for cross-reference (must be in /input/)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pause for human review of flagged words before muting (Step 4b)",
    )
    parser.add_argument(
        "--no-interactive",
        dest="no_interactive",
        action="store_true",
        help="Force unattended mode (overrides config.yaml)",
    )
    parser.add_argument(
        "--config",
        default="/config/config.yaml",
        help="Path to config.yaml inside the container (default: /config/config.yaml)",
    )

    args = parser.parse_args()

    print("=" * 72)
    print("profanity-hush — Phase 1 stub")
    print("=" * 72)
    print()
    print("The Docker image built and the argument interface is wired up correctly.")
    print("Phase 2 (core pipeline) is not yet implemented.")
    print()
    print("Received arguments:")
    print(f"  input_video    : {args.input_video}")
    print(f"  subtitle_file  : {args.subtitle_file or '(none)'}")
    print(f"  interactive    : {args.interactive}")
    print(f"  no_interactive : {args.no_interactive}")
    print(f"  config         : {args.config}")
    print()
    print("Next step: implement Phase 2 modules in src/steps/ and src/utils.py")
    print("           then replace this stub with the real orchestrator.")
    print()

    sys.exit(0)


if __name__ == "__main__":
    main()
