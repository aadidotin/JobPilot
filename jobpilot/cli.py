"""jobpilot CLI — thin entry points; logic lives in the modules."""

import argparse


def main():
    parser = argparse.ArgumentParser(prog="jobpilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb", help="create tables (WAL mode)")
    sub.add_parser("bot", help="run the Telegram bot daemon (long polling)")
    run = sub.add_parser("run", help="one chained pipeline run (what cron calls)")
    run.add_argument("--force-jobspy", action="store_true", help="run the JobSpy leg regardless of window")
    run.add_argument("--force-digest", action="store_true", help="send the digest regardless of window")
    args = parser.parse_args()

    if args.command == "initdb":
        from dotenv import load_dotenv

        load_dotenv()
        from jobpilot.db import DB_PATH, init_db

        init_db()
        print(f"initialized {DB_PATH}")
    elif args.command == "bot":
        from jobpilot.bot import main as bot_main

        bot_main()
    elif args.command == "run":
        from jobpilot.pipeline import main as pipeline_main

        raise SystemExit(pipeline_main(args.force_jobspy, args.force_digest))


if __name__ == "__main__":
    main()
