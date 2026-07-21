import sys


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: terum-capture <command>")
        print("Commands: upload, setup, backfill, status, update, setup-hook, logout, mcp, delivery")
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
        mcp = None
        i = 1
        while i < len(args):
            if args[i] == "--url" and i + 1 < len(args):
                url = args[i + 1]
                i += 2
            elif args[i] == "--token" and i + 1 < len(args):
                token = args[i + 1]
                i += 2
            elif args[i] == "--mcp":
                mcp = True
                i += 1
            elif args[i] == "--no-mcp":
                mcp = False
                i += 1
            else:
                i += 1
        cmd_setup(api_url=url, token=token, mcp=mcp)

    elif command == "status":
        from terum_capture.commands import cmd_status
        cmd_status()

    elif command == "update":
        from terum_capture.updater import cmd_update
        cmd_update()

    elif command == "setup-hook":
        from terum_capture.commands import cmd_setup_hook
        cmd_setup_hook()

    elif command == "logout":
        from terum_capture.commands import cmd_logout
        cmd_logout()

    elif command == "delivery-hook":
        # Invoked by the Claude Code hooks themselves (reads the hook payload from stdin), like
        # `upload`. Not run by hand. `delivery-hook prompt` = UserPromptSubmit, `pretooluse` = PreToolUse.
        from terum_capture.delivery_hooks import run_prompt_hook, run_pretooluse_hook
        event = args[1] if len(args) >= 2 else ""
        if event == "prompt":
            run_prompt_hook()
        elif event == "pretooluse":
            run_pretooluse_hook()
        else:
            print("Usage: terum-capture delivery-hook <prompt|pretooluse>")
            sys.exit(1)

    elif command == "delivery":
        from terum_capture.delivery_hooks import cmd_delivery
        cmd_delivery(args[1] if len(args) >= 2 else "")

    elif command == "mcp":
        if len(args) >= 2 and args[1] == "install":
            from terum_capture.commands import cmd_mcp_install
            client = "claude"
            i = 2
            while i < len(args):
                if args[i] == "--client":
                    if i + 1 >= len(args):
                        print("Error: --client requires a value (claude|cursor).")
                        sys.exit(1)
                    client = args[i + 1]
                    i += 2
                else:
                    i += 1
            if client not in ("claude", "cursor"):
                print(f"Error: unknown MCP client '{client}'. Use claude or cursor.")
                sys.exit(1)
            cmd_mcp_install(client)
        else:
            print("Usage: terum-capture mcp install [--client claude|cursor]")
            sys.exit(1)

    else:
        print(f"Unknown command: {command}")
        print("Commands: upload, setup, backfill, status, update, setup-hook, logout, mcp, delivery")
        sys.exit(1)


def _parse_int(value: str, flag: str) -> int:
    try:
        return int(value)
    except ValueError:
        print(f"Error: {flag} expects an integer, got {value!r}.")
        sys.exit(1)


if __name__ == "__main__":
    main()
