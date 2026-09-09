"""Check an IR pair with Alive2: its exit code alone does not indicate success."""

import argparse
import re
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("alive_tv", "opt", "before", "after"):
        parser.add_argument(name)
    parser.add_argument("--timeout", type=int, default=10, help="SMT query seconds")
    parser.add_argument(
        "--wall-timeout", type=int, default=120, help="total Alive2 seconds"
    )
    args = parser.parse_args()
    if min(args.timeout, args.wall_timeout) < 1:
        parser.error("timeouts must be positive")
    try:
        with tempfile.TemporaryDirectory(prefix="alive2-") as directory:
            inputs = [Path(directory) / name for name in ("before.ll", "after.ll")]
            definitions = []
            for original, canonical in zip((args.before, args.after), inputs):
                subprocess.run(
                    [args.opt, "-S", "-passes=verify", original, "-o", str(canonical)],
                    check=True,
                )
                names = re.findall(
                    r'^define\b[^\n]*?@("(?:[^"\\]|\\.)*"|[-a-zA-Z$._0-9]+)\s*\(',
                    canonical.read_text(),
                    re.MULTILINE,
                )
                if len(names) != len(set(names)):
                    raise ValueError("Ambiguous function definitions")
                definitions.append(set(names))
            if not definitions[0] or definitions[0] != definitions[1]:
                raise ValueError(
                    "IR pairs must define the same nonempty set of functions"
                )
            result = subprocess.run(
                [args.alive_tv, f"-smt-to={args.timeout * 1000}", *map(str, inputs)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.wall_timeout,
            )
            print(result.stdout, end="", flush=True)
            counts = []
            for label in (
                "correct transformations",
                "incorrect transformations",
                "failed-to-prove transformations",
                "Alive2 errors",
            ):
                values = re.findall(
                    r"^\s*(\d+) " + re.escape(label) + r"\s*$",
                    result.stdout,
                    re.MULTILINE,
                )
                if len(values) != 1:
                    raise ValueError("Missing or ambiguous Alive2 summary")
                counts.append(int(values[0]))
            if result.returncode or counts != [len(definitions[0]), 0, 0, 0]:
                raise ValueError("Not every function was validated successfully")
            return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Not validated: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
