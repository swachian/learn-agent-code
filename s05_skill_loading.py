import os
import re
import json
import subprocess
import shlex
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)


client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("NVIDIA_API_KEY"),
)

WORKDIR = Path.cwd()
MODEL = os.getenv("MODEL_ID", "moonshotai/kimi-k2.5")
SKILLS_DIR = WORKDIR / "skills"

@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path

@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str

class SkillRegistry:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.documents: dict[str, SkillDocument] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return 
        
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            meta, body = self._parse_frontmatter(path.read_text())
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "No description")
            manifest = SkillManifest(name=name, description=description, path=path)
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip())

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)

    def describe_available(self) -> str:
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "\n".join(lines)
    
    def load_full_text(self, name: str) -> str:
        document = self.documents.get(name)
        if not document:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknown skill '{name}'. Available skills: {known}"
        
        return (
            f"<skill name=\"{document.manifest.name}\">\n"
            f"{document.body}"
            "</skill>"
        )
    
    
SKILL_REGISTRY = SkillRegistry(SKILLS_DIR)

SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill when a task needs specialized instructions before you act.
Meanwhile, if anyone asks what skills you have, you can use the following skills info.

Skills available:
{SKILL_REGISTRY.describe_available()}
"""


def safe_path(p: str) -> Path:
    """Validate and sanitize a file path to prevent directory traversal attacks.

    Args:
        p: The path string to validate.

    Returns:
        A resolved Path object relative to WORKDIR.

    Raises:
        ValueError: If the path attempts to escape WORKDIR.
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError("Access denied")
    return path

def run_bash(command: str) -> str:
    """Execute a shell command safely without using shell=True.

    Blocks dangerous patterns like pipes, redirections, and command chaining.

    Args:
        command: The shell command string to execute.

    Returns:
        Command output (stdout + stderr) or error message.
    """
    # Block dangerous patterns more comprehensively
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    """Read file contents with optional line limit.

    Args:
        path: Path to the file to read.
        limit: Maximum number of lines to read (optional).

    Returns:
        File content as string, with truncation indicator if limited.
    """
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"
    
def run_write(path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        path: Path to write the file.
        content: Content to write.

    Returns:
        Success message with byte count or error message.
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """Replace old_text with new_text in a file (first occurrence only).

    Args:
        path: Path to the file to edit.
        old_text: Text to search for.
        new_text: Text to replace with.

    Returns:
        Success message or error message if old_text not found.
    """
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
    
    
    
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full body of a named skill into the current context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}
                },
                "required": ["name"],
            },
        },
    },
]   

TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_REGISTRY.load_full_text(kw["name"]),
}
 
    
def extract_text(message) -> str:
    """Extract text content from an OpenAI message object.

    Args:
        message: An OpenAI message object with .content attribute.

    Returns:
        Stripped text content or empty string if none.
    """
    return message.content.strip() if message.content else ""


def agent_loop(messages: list) -> None:
    """Main agent loop that handles tool calls from LLM responses.

    Continuously calls the LLM, executes any requested tools,
    and feeds results back until the LLM returns without tool calls.

    Args:
        messages: Conversation history list, modified in-place.
    """
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                *messages
            ],
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=2000,
        )

        msg = response.choices[0].message
        messages.append(msg)

        # No tool call, end
        if not msg.tool_calls:
            return

        results = []

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")

            handler = TOOL_HANDLERS.get(name)

            try:
                output = handler(**args) if handler else f"Unknown tool: {name}"
            except Exception as exc:
                output = f"Error: {exc}"

            print(f"> {name}: {str(output)[:200]}")

            results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(output),
            })

        messages.extend(results)


if __name__ == "__main__":
    history = []

    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})

        agent_loop(history)

        final_text = extract_text(history[-1])
        if final_text:
            print(final_text)

        print()