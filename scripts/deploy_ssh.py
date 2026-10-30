#!/usr/bin/python3
"""Forced SSH command for the production deploy account; installed by root."""

import os
import re
import sys


def deployment_command(command: str) -> list[str]:
    match = re.fullmatch(r"deploy ([0-9a-f]{40})", command)
    if match is None:
        raise ValueError("Only 'deploy <40 lowercase hex characters>' is allowed")
    return ["/usr/bin/sudo", "-n", "/usr/local/sbin/misis-deploy-request", match[1]]


if __name__ == "__main__":
    try:
        argv = deployment_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    os.execv(argv[0], argv)  # noqa: S606 - fixed executable and strictly validated SHA; no shell
