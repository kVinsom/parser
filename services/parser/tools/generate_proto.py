"""Generate from the gateway's single authoritative schema; never copy its source."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "access-gateway" / "bypass" / "proto"
OUTPUT = ROOT / ".generated"
OUTPUT.mkdir(exist_ok=True)
subprocess.run(
    [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{SOURCE}",
        f"--python_out={OUTPUT}",
        f"--grpc_python_out={OUTPUT}",
        str(SOURCE / "vinted_rpc" / "access.proto"),
    ],
    check=True,
)
print("Generated gateway bindings in .generated (source schema unchanged).")
