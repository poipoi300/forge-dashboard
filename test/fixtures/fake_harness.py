"""A deterministic stand-in for `claude -p --output-format stream-json`.

Reads the prompt from stdin, edits files in its working directory according
to the kind of brief it received, and prints stream-json records like the
real CLI: an init record, assistant text and tool_use blocks, and a result
record with cost, turns, and duration.

Modes, chosen by markers the dashboard's briefs carry:
- "# Conflict brief": resolve every conflicted file listed as `### File: <path>`
  by keeping ours then theirs. With --sabotage-first the first
  attempt (a brief without "Previous attempt") leaves the markers in place.
- "# Bring up to standards": copy forge's AGENTS.md (between FORGE_AGENTS_MD
  markers) over the project's, append forge's .gitignore lines, add a README
  setup section, and set [forge].template_revision.
- "# Adopt": write forge.project.toml (invalid first when sabotaged).
- anything else: write AGENT_WAS_HERE.md.
With --exit-code <n> it exits with that code after printing an error result.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--sabotage-first", action="store_true")
parser.add_argument("--exit-code", type=int, default=0)
options, _ = parser.parse_known_args()

prompt = sys.stdin.read()
cwd = Path.cwd()
started = time.time()
turns = 0


def emit(record: dict[str, object]) -> None:
    print(json.dumps(record), flush=True)


def tool(name: str, path: str) -> None:
    global turns
    turns += 1
    emit(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": {"file_path": path}}]},
        }
    )


def say(text: str) -> None:
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def between(marker: str) -> str | None:
    match = re.search(rf"<<<{marker}\n(.*?)\n{marker}>>>", prompt, flags=re.DOTALL)
    return match.group(1) if match else None


CONFLICT = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n", re.MULTILINE | re.DOTALL)

emit({"type": "system", "subtype": "init", "model": "fake-harness", "session_id": "fake"})
sabotage = options.sabotage_first and "Previous attempt" not in prompt

if "# Conflict brief" in prompt:
    say("Resolving conflicts by keeping both sides.")
    for path in re.findall(r"^### File: (.+)$", prompt, flags=re.MULTILINE):
        target = cwd / path.strip()
        if sabotage:
            continue
        text = target.read_text(encoding="utf-8")
        target.write_text(CONFLICT.sub(lambda m: m.group(1) + m.group(2), text), encoding="utf-8", newline="\n")
        tool("Edit", path.strip())
elif "# Bring up to standards" in prompt:
    say("Aligning AGENTS.md, .gitignore, README, and the manifest with forge.")
    agents = between("FORGE_AGENTS_MD")
    if agents is not None:
        (cwd / "AGENTS.md").write_text(agents + "\n", encoding="utf-8", newline="\n")
        tool("Write", "AGENTS.md")
    ignore = between("FORGE_GITIGNORE")
    if ignore is not None:
        existing = (cwd / ".gitignore").read_text(encoding="utf-8") if (cwd / ".gitignore").exists() else ""
        missing = [line for line in ignore.splitlines() if line and line not in existing.splitlines()]
        (cwd / ".gitignore").write_text(
            existing + "".join(f"{line}\n" for line in missing), encoding="utf-8", newline="\n"
        )
        tool("Edit", ".gitignore")
    readme = cwd / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.exists() else "# Project\n"
    if "## Setup" not in text:
        readme.write_text(text.rstrip("\n") + "\n\n## Setup\n\nRun the gates.\n", encoding="utf-8", newline="\n")
        tool("Edit", "README.md")
    revision = re.search(r"template_revision = \"([0-9a-f]+)\"", prompt)
    manifest = cwd / "forge.project.toml"
    if revision and manifest.exists():
        body = re.sub(r"\n\[forge\]\n[^\[]*", "\n", manifest.read_text(encoding="utf-8")).rstrip("\n")
        manifest.write_text(
            body + f'\n\n[forge]\ntemplate_revision = "{revision.group(1)}"\n', encoding="utf-8", newline="\n"
        )
        tool("Edit", "forge.project.toml")
elif "# Adopt" in prompt:
    say("Writing forge.project.toml from the repository.")
    name = cwd.name
    if sabotage:
        manifest_text = 'schema = 1\n\n[project]\nname = "unfinished"\n'
    else:
        manifest_text = (
            f'schema = 1\n\n[project]\nname = "{name}"\nid = "adopted"\nsummary = "Adopted by the dashboard"\n'
            'kind = "tool"\nlanguages = ["python"]\n\n[v1]\nsource = "milestones"\n\n'
            '[[v1.milestones]]\nid = "first"\ntitle = "First milestone"\n'
        )
    (cwd / "forge.project.toml").write_text(manifest_text, encoding="utf-8", newline="\n")
    tool("Write", "forge.project.toml")
else:
    say("Implementing the request.")
    (cwd / "AGENT_WAS_HERE.md").write_text("# Done\n\n" + prompt.splitlines()[0] + "\n", encoding="utf-8", newline="\n")
    tool("Write", "AGENT_WAS_HERE.md")

exit_code = options.exit_code
emit(
    {
        "type": "result",
        "subtype": "success" if exit_code == 0 else "error_during_execution",
        "is_error": exit_code != 0,
        "num_turns": max(turns, 1),
        "duration_ms": int((time.time() - started) * 1000),
        "total_cost_usd": 0.0123,
        "result": "done",
        "session_id": "fake",
    }
)
sys.exit(exit_code)
