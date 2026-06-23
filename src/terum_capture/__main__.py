"""Package entry point so `python -m terum_capture <command>` works.

The installed Stop hook invokes the CLI this way — through the signed Python
interpreter — rather than through the unsigned `terum-capture.exe` console-script
shim, which Windows Smart App Control / WDAC block on enforcing machines. See
terum_capture.commands._hook_command for the full rationale.
"""
from terum_capture.cli import main

if __name__ == "__main__":
    main()
