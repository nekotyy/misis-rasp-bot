#!/usr/bin/python3
"""Start a root-managed deployment that survives an interrupted SSH connection."""

import os
import re
import subprocess
import sys


def service_command(sha: str) -> list[str]:
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("Expected one full lowercase Git commit SHA")
    return [
        "/usr/bin/systemd-run", "--quiet", "--wait", "--collect",
        f"--unit=misis-deploy-{sha}", "--property=Type=exec", "--property=RuntimeMaxSec=1800",
        "--property=UMask=0077", "/usr/local/sbin/misis-deploy", sha,
    ]


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or os.name != "posix" or os.geteuid() != 0:
            raise ValueError("This entrypoint accepts one SHA and must run as root on Linux")
        result = subprocess.run(service_command(sys.argv[1]), check=False)
        if result.returncode:
            print("Deployment failed; inspect /var/lib/misis-deploy/deploy.log and the deployment unit journal", file=sys.stderr)
        sys.exit(result.returncode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
