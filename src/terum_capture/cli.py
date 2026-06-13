import sys


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: terum-capture <command>")
        print("Commands: upload, setup, status, logout")
        sys.exit(1)

    command = args[0]

    if command == "upload":
        from terum_capture.upload import cmd_upload
        cmd_upload()

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
        print("Commands: upload, setup, status, logout")
        sys.exit(1)


if __name__ == "__main__":
    main()
