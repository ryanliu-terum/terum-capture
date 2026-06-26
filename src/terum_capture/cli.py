import sys


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: terum-capture <command>")
        print("Commands: upload, setup, backfill, status, logout")
        sys.exit(1)

    command = args[0]

    if command == "upload":
        from terum_capture.upload import cmd_upload
        cmd_upload()

    elif command == "backfill":
        from terum_capture.backfill import cmd_backfill
        window_days = 30
        limit = None
        i = 1
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                window_days = _parse_int(args[i + 1], "--days")
                i += 2
            elif args[i] == "--limit" and i + 1 < len(args):
                limit = _parse_int(args[i + 1], "--limit")
                i += 2
            elif args[i] == "--all":
                window_days = None  # no time window — explicit opt-in to the full history
                i += 1
            else:
                i += 1
        cmd_backfill(window_days=window_days, limit=limit)

    elif command == "setup":
        from terum_capture.commands import cmd_setup
        url = None
        token = None
        i = 1
        while i < len(args):
            if args[i] == "--url" and i + 1 < len(args):
                url = args[i + 1]
                i += 2
            elif args[i] == "--token" and i + 1 < len(args):
                token = args[i + 1]
                i += 2
            else:
                i += 1
        cmd_setup(api_url=url, token=token)

    elif command == "status":
        from terum_capture.commands import cmd_status
        cmd_status()

    elif command == "logout":
        from terum_capture.commands import cmd_logout
        cmd_logout()

    else:
        print(f"Unknown command: {command}")
        print("Commands: upload, setup, backfill, status, logout")
        sys.exit(1)


def _parse_int(value: str, flag: str) -> int:
    try:
        return int(value)
    except ValueError:
        print(f"Error: {flag} expects an integer, got {value!r}.")
        sys.exit(1)


if __name__ == "__main__":
    main()
