#!/usr/bin/env python
"""
batch.py — run run.py on every scad2py example, each in its own subprocess
with a per-example timeout, so a pathological heavy example (huge CSG tree
that OCCT chokes on) cannot hang the whole sweep.

Writes a one-line-per-example result table to stdout and batch_results.txt.
"""
import os
import subprocess
import sys

PROTO = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = "/Users/ochafik/github/scad2py/examples"
PY = sys.executable
TIMEOUT = 90  # seconds per example


def run_one(scad_path):
    try:
        p = subprocess.run(
            [PY, os.path.join(PROTO, "run.py"), scad_path],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=PROTO)
        out = p.stdout
        # extract the single SUMMARY line
        line = ""
        for ln in out.splitlines():
            if ln.startswith("#   OK") or ln.startswith("#   FAIL"):
                line = ln
        return line or "#   ???  (no summary line)", out
    except subprocess.TimeoutExpired:
        return f"#   TIMEOUT {os.path.basename(scad_path):<16} (>{TIMEOUT}s)", ""
    except Exception as e:
        return f"#   CRASH {os.path.basename(scad_path):<16} {e}", ""


def main():
    files = sorted(f for f in os.listdir(EXAMPLES) if f.endswith(".scad"))
    results = []
    for f in files:
        line, _ = run_one(os.path.join(EXAMPLES, f))
        print(line, flush=True)
        results.append(line)
    with open(os.path.join(PROTO, "batch_results.txt"), "w") as fp:
        fp.write("\n".join(results) + "\n")
    ok = sum(1 for r in results if "OK" in r)
    print(f"\n# {ok}/{len(results)} examples rendered to build123d")


if __name__ == "__main__":
    main()
