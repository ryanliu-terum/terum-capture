import sys


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: terum-capture <command>")
        print("Commands: upload, setup, status, logout, mcp")
        sys.exit(1)

    command = args[0]

    if command == "upload":
        from terum_capture.upload import cmd_upload
        cmd_upload()

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

    elif command == "logout":
        from terum_capture.commands import cmd_logout
        cmd_logout()

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
        print("Commands: upload, setup, status, logout, mcp")
        sys.exit(1)


if __name__ == "__main__":
    main()
