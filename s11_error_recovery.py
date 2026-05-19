# Harness: extensibility -- injecting behavior without touching the loop.
"""
LLM call
  |
  +-- stop_reason == "max_tokens"
  |      -> append continuation reminder
  |      -> retry
  |
  +-- prompt too long
  |      -> compact context
  |      -> retry
  |
  +-- timeout / rate limit / connection error
         -> back off
         -> retry
"""

import os
import re
import json
import subprocess
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv
from otel import tracer
from prompt_toolkit import prompt
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
import datetime

load_dotenv(override=True)


client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("NVIDIA_API_KEY"),
)

WORKDIR = Path.cwd()
MODEL = os.getenv("MODEL_ID", "moonshotai/kimi-k2.6")

DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30  # seconds
# Real CC timeouts:
#   TOOL_HOOK_EXECUTION_TIMEOUT_MS = 600000 (10 minutes for tool hooks)
#   SESSION_END_HOOK_TIMEOUT_MS = 1500 (1.5 seconds for SessionEnd hooks)

TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"

# Recovery constants
MAX_RECOVERY_ATTEMPTS = 15
BACKOFF_BASE_DELAY = 1.0  # seconds
BACKOFF_MAX_DELAY = 30.0  # seconds
TOKEN_THRESHOLD = 50000   # chars / 4 ~ tokens for compact trigger
CONTINUATION_MESSAGE = (
    "Output limit hit. Continue directly from where you stopped -- "
    "no recap, no repetition. Pick up mid-sentence if needed."
)
def estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(json.dumps(messages, default=str)) // 4

def auto_compact(messages: list) -> list:
    # Save full transcript to disk

    # Ask LLM to summarize
    conversation_text = json.dumps(messages, default=str)[-80000:]
    # response = client.messages.create(
    #     model=MODEL,
    #     messages=[{"role": "user", "content":
    #         "Summarize this conversation for continuity. Include: "
    #         "1) What was accomplished, 2) Current state, 3) Key decisions made. "
    #         "Be concise but preserve critical details.\n\n" + conversation_text}],
    #     max_tokens=2000,
    # )
    # summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    
    try: 
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Summarize this conversation for continuity. Include:\n"
                        "1) Task overview and success criteria\n"
                        "2) Current state: completed work, files touched\n"
                        "3) Key decisions and failed approaches\n"
                        "4) Remaining next steps\n"
                        "Be concise but preserve critical details.\n\n"
                        + conversation_text
                    )
                }
            ],
            max_tokens=8000,
        )

        summary = response.choices[0].message.content
    except Exception as e:
        summary = f"(compact failed: {e}). Previous context lost."
        
    continuation = (
        "This session continues from a previous conversation that was compacted. "
        f"Summary of prior context:\n\n{summary}\n\n"
        "Continue from where we left off without re-asking the user."
    )
    return [{"role": "user", "content": continuation}]

import random

def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: base * 2^attempt + random(0, 1)."""
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)
    return delay + jitter

class MemoryManager:
    """
    Load, build, and save persistent memories across sessions.
    The teaching version keeps memory explicit:
    one Markdown file per memory, plus one compact index file.
    """
    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {}  # name -> {description, type, content}
        
    def load_all(self):
        """Load MEMORY.md index and all individual memory files."""
        self.memories = {}
        if not self.memory_dir.exists():
            return
        # Scan all .md files except MEMORY.md
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text())
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }
        count = len(self.memories)
        if count > 0:
            print(f"[Memory loaded: {count} memories from {self.memory_dir}]")
            
    def load_memory_prompt(self) -> str:
        """Build a memory section for injection into the system prompt."""
        if not self.memories:
            return ""
        sections = []
        sections.append("# Memories (persistent across sessions)")
        sections.append("")
        # Group by type for readability
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")
        return "\n".join(sections)
    
    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        """
        Save a memory to disk and update the index.
        Returns a status message.
        """
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        # Sanitize name for filename
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: invalid memory name"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # Write individual memory file with frontmatter
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n"
            f"{content}\n"
        )
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter)
        # Update in-memory store
        self.memories[name] = {
            "description": description,
            "type": mem_type,
            "content": content,
            "file": file_name,
        }
        # Rebuild MEMORY.md index
        self._rebuild_index()
        return f"Saved memory '{name}' [{mem_type}] to {file_path.relative_to(WORKDIR)}"
    
    def _rebuild_index(self):
        """Rebuild MEMORY.md from current in-memory state, capped at 200 lines."""
        lines = ["# Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem['description']} [{mem['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (truncated at {MAX_INDEX_LINES} lines)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n")
    def _parse_frontmatter(self, text: str) -> dict | None:
        """Parse --- delimited frontmatter + body content."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result


class HookManager:
    """
    Load and execute hooks from .hooks.json configuration.
    The hook manager does three simple jobs:
    - load hook definitions
    - run matching commands for an event
    - aggregate block / message results for the caller
    """
    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[Hooks loaded from {config_path}]")
            except Exception as e:
                print(f"[Hook config error: {e}]")
    def _check_workspace_trust(self) -> bool:
        """
        Check whether the current workspace is trusted.
        The teaching version uses a simple trust marker file.
        In SDK mode, trust is treated as implicit.
        """
        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()
    def run_hooks(self, event: str, context: dict = None) -> dict:
        """
        Execute all hooks for an event.
        Returns: {"blocked": bool, "messages": list[str]}
          - blocked: True if any hook returned exit code 1
          - messages: stderr content from exit-code-2 hooks (to inject)
        """
        result = {"blocked": False, "messages": []}
        # Trust gate: refuse to run hooks in untrusted workspaces
        if not self._check_workspace_trust():
            return result
        hooks = self.hooks.get(event, [])
        for hook_def in hooks:
            # Check matcher (tool name filter for PreToolUse/PostToolUse)
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue
            command = hook_def.get("command", "")
            if not command:
                continue
            # Build environment with hook context
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False)[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"])[:10000]
            try:
                r = subprocess.run(
                    command, shell=True, cwd=WORKDIR, env=env,
                    capture_output=True, text=True, timeout=HOOK_TIMEOUT,
                )
                if r.returncode == 0:
                    # Continue silently
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")
                    # Optional structured stdout: small extension point that
                    # keeps the teaching contract simple.
                    try:
                        hook_output = json.loads(r.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (
                                hook_output["permissionDecision"])
                    except (json.JSONDecodeError, TypeError):
                        pass  # stdout was not JSON -- normal for simple hooks
                elif r.returncode == 1:
                    # Block execution
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")
                elif r.returncode == 2:
                    # Inject message
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"  [hook:{event}] INJECT: {msg[:200]}")
                else:
                    # Block execution
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")
            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")
        return result



# Global memory manager
memory_mgr = MemoryManager()


def run_save_memory(name: str, description: str, mem_type: str, content: str) -> str:
    return memory_mgr.save_memory(name, description, mem_type, content)


MEMORY_GUIDANCE = """
When to save memories:
- User states a preference ("I like tabs", "always use pytest") -> mem_type: user
- User corrects you ("don't do X", "that was wrong because...") -> mem_type: feedback
- You learn a project fact that is not easy to infer from current code alone
  (for example: a rule exists because of compliance, or a legacy module must
  stay untouched for business reasons) -> mem_type: project
- You learn where an external resource lives (ticket board, dashboard, docs URL)
  -> mem_type: reference
When NOT to save:
- Anything easily derivable from code (function signatures, file structure, directory layout)
- Temporary task state (current branch, open PR numbers, current TODOs)
- Secrets or credentials (API keys, passwords)
"""

def normalize_tool(tool):
    if "function" in tool:
        fn = tool["function"]
        return {
            "name": fn["name"],
            "description": fn["description"],
            "parameters": fn["parameters"],
        }

    return {
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool.get("input_schema", {}),
    }

class SystemPromptBuilder:
    """
    Assemble the system prompt from independent sections.
    The teaching goal here is clarity:
    each section has one source and one responsibility.
    That makes the prompt easier to reason about, easier to test, and easier
    to evolve as the agent grows new capabilities.
    """
    def __init__(self, workdir: Path = None, tools: list = None):
        self.workdir = workdir or WORKDIR
        self.tools = tools or []
        self.skills_dir = self.workdir / "skills"
        self.memory_dir = self.workdir / ".memory"
    # -- Section 1: Core instructions --
    def _build_core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "Use the provided tools to explore, read, write, and edit files.\n"
            "Always verify before assuming. Prefer reading files over guessing."
        )
    # -- Section 2: Tool listings --
    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""
        lines = ["# Available tools"]
        for raw in self.tools:
            tool = normalize_tool(raw)

            props = tool["parameters"].get("properties", {})

            params = ", ".join(props.keys())

            lines.append(
                f"- {tool['name']}({params}): {tool['description']}"
            )
        return "\n".join(lines)
    # -- Section 3: Skill metadata (layer 1 from s05 concept) --
    def _build_skill_listing(self) -> str:
        if not self.skills_dir.exists():
            return ""
        skills = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text()
            # Parse frontmatter for name + description
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                continue
            meta = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", skill_dir.name)
            desc = meta.get("description", "")
            skills.append(f"- {name}: {desc}")
        if not skills:
            return ""
        return "# Available skills\n" + "\n".join(skills)
    # -- Section 4: Memory content --
    def _build_memory_section(self) -> str:
        if not self.memory_dir.exists():
            return ""
        memories = []
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            text = md_file.read_text()
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
            if not match:
                continue
            header, body = match.group(1), match.group(2).strip()
            meta = {}
            for line in header.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", md_file.stem)
            mem_type = meta.get("type", "project")
            desc = meta.get("description", "")
            memories.append(f"[{mem_type}] {name}: {desc}\n{body}")
        if not memories:
            return ""
        return "# Memories (persistent)\n\n" + "\n\n".join(memories)
    # -- Section 5: CLAUDE.md chain --
    def _build_claude_md(self) -> str:
        """
        Load CLAUDE.md files in priority order (all are included):
        1. ~/.claude/CLAUDE.md (user-global instructions)
        2. <project-root>/CLAUDE.md (project instructions)
        3. <current-subdir>/CLAUDE.md (directory-specific instructions)
        """
        sources = []
        # User-global
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        if user_claude.exists():
            sources.append(("user global (~/.claude/CLAUDE.md)", user_claude.read_text()))
        # Project root
        project_claude = self.workdir / "CLAUDE.md"
        if project_claude.exists():
            sources.append(("project root (CLAUDE.md)", project_claude.read_text()))
        # Subdirectory -- in real CC, this walks from cwd up to project root
        # Teaching: check cwd if different from workdir
        cwd = Path.cwd()
        if cwd != self.workdir:
            subdir_claude = cwd / "CLAUDE.md"
            if subdir_claude.exists():
                sources.append((f"subdir ({cwd.name}/CLAUDE.md)", subdir_claude.read_text()))
        if not sources:
            return ""
        parts = ["# CLAUDE.md instructions"]
        for label, content in sources:
            parts.append(f"## From {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)
    # -- Section 6: Dynamic context --
    def _build_dynamic_context(self) -> str:
        lines = [
            f"Current date: {datetime.date.today().isoformat()}",
            f"Working directory: {self.workdir}",
            f"Model: {MODEL}",
            f"Platform: {os.uname().sysname}",
        ]
        return "# Dynamic context\n" + "\n".join(lines)
    # -- Assemble all sections --
    def build(self) -> str:
        """
        Assemble the full system prompt from all sections.
        Static sections (1-5) are separated from dynamic (6) by
        the DYNAMIC_BOUNDARY marker. In real CC, the static prefix
        is cached across turns to save prompt tokens.
        """
        sections = []
        core = self._build_core()
        if core:
            sections.append(core)
        tools = self._build_tool_listing()
        if tools:
            sections.append(tools)
        skills = self._build_skill_listing()
        if skills:
            sections.append(skills)
        memory = self._build_memory_section()
        if memory:
            sections.append(memory)
        claude_md = self._build_claude_md()
        if claude_md:
            sections.append(claude_md)
        # Static/dynamic boundary
        sections.append(DYNAMIC_BOUNDARY)
        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)
        return "\n\n".join(sections)



    
# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

      
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(
        kw["path"],
        kw["old_text"],
        kw["new_text"]
    ),
    "save_memory": lambda **kw: run_save_memory(
        kw["name"],
        kw["description"],
        kw["mem_type"],
        kw["content"]
    ),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string"
                    }
                },
                "required": ["command"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "limit": {
                        "type": "integer"
                    }
                },
                "required": ["path"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "content": {
                        "type": "string"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "old_text": {
                        "type": "string"
                    },
                    "new_text": {
                        "type": "string"
                    }
                },
                "required": [
                    "path",
                    "old_text",
                    "new_text"
                ]
            }
        }
    },
    {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "Save a persistent memory that survives across sessions. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short identifier for the memory. Use snake_case."
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary of what this memory captures"
                },
                "mem_type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": "Type of memory"
                },
                "content": {
                    "type": "string",
                    "description": "Full memory content"
                }
            },
            "required": ["name", "description", "mem_type", "content"]
        }
    }
 },
   
  
]

# Global prompt builder
prompt_builder = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS)

# ---------------------------------------------------
# Agent loop
# ---------------------------------------------------
import json5
TOOLS_SAFE = json.loads(json.dumps(TOOLS))
def agent_loop(messages: list, hooks: HookManager):

    turn = 0
    max_output_recovery_count = 0

    while True:
        turn += 1

        with tracer.start_as_current_span(f"agent_turn_{turn}") as agent_span:

            agent_span.set_attribute("agent.turn", turn)
            agent_span.set_attribute("agent.message_count", len(messages))

            # -------------------------------------------------
            # PRE-CALL: token compaction guard (PROACTIVE)
            # -------------------------------------------------
            if estimate_tokens(messages) > TOKEN_THRESHOLD:
                agent_span.add_event("auto_compact_triggered")
                messages[:] = auto_compact(messages)

            # -------------------------------------------------
            # API CALL (with retry + prompt-too-long recovery)
            # -------------------------------------------------
            response = None

            for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):

                try:
                    with tracer.start_as_current_span("llm_call") as llm_span:

                        system = prompt_builder.build()

                        llm_span.set_attribute("llm.model", MODEL)
                        llm_span.set_attribute("llm.message_count", len(messages))
                        llm_span.set_attribute("llm.system_prompt", system)

                        agent_span.add_event("start query")
                        llm_span.add_event("start query 2")
                        
                        response = client.chat.completions.create(
                            model=MODEL,
                            messages=[{"role": "system", "content": system}, *messages],
                            tools=TOOLS_SAFE,
                            tool_choice="auto",
                            max_tokens=2000,
                        )

                        break  # success

                except Exception as e:

                    err = str(e).lower()

                    # -----------------------------
                    # prompt too long recovery
                    # -----------------------------
                    if "overlong" in err or "prompt" in err:
                        agent_span.add_event("prompt_too_long_recovery")

                        messages[:] = auto_compact(messages)
                        continue

                    # -----------------------------
                    # network retry backoff
                    # -----------------------------
                    if attempt < MAX_RECOVERY_ATTEMPTS:
                        delay = backoff_delay(attempt)

                        agent_span.add_event(
                            "retry",
                            {"attempt": attempt, "delay": delay}
                        )

                        time.sleep(delay)
                        continue

                    raise

            if response is None:
                return

            assistant_message = response.choices[0].message
            
            print(response.choices[0].finish_reason)
             
            if response.choices[0].finish_reason == "length":
                max_output_recovery_count += 1

                if max_output_recovery_count <= MAX_RECOVERY_ATTEMPTS:

                    agent_span.add_event("max_tokens_recovery")

                    messages.append({
                        "role": "user",
                        "content": CONTINUATION_MESSAGE
                    })
                    agent_span.add_event(
                        "max_tokens_recovery",
                        {
                            "message_preview": str(messages[-2])[:5000]
                        }
                    )

                    continue

                else:
                    agent_span.add_event("max_tokens_exhausted")
                    return

            # -------------------------------------------------
            # STORE ASSISTANT MESSAGE
            # -------------------------------------------------
            messages.append({
                "role": "assistant",
                "content": assistant_message.content or "",
                "tool_calls": assistant_message.tool_calls,
            })

            # -------------------------------------------------
            # STOP CONDITION (NO TOOL)
            # -------------------------------------------------
            if not assistant_message.tool_calls:

                agent_span.set_attribute("agent.finished", True)
                print(assistant_message.content)
                return

            # =================================================
            # TOOL EXECUTION PHASE (your original logic)
            # =================================================
            tool_results = []

            for tool_call in assistant_message.tool_calls:

                tool_name = tool_call.function.name
                args = json5.loads(tool_call.function.arguments)

                agent_span.add_event("tool_selected", {
                    "tool.name": tool_name
                })

                ctx = {"tool_name": tool_name, "tool_input": args}

                pre = hooks.run_hooks("PreToolUse", ctx)

                if pre.get("blocked"):
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"Blocked: {pre.get('block_reason')}",
                    })
                    continue

                try:
                    handler = TOOL_HANDLERS.get(tool_name)
                    output = handler(**args) if handler else f"Unknown tool: {tool_name}"

                except Exception as e:
                    output = f"Error: {e}"

                ctx["tool_output"] = output
                post = hooks.run_hooks("PostToolUse", ctx)

                for m in post.get("messages", []):
                    output += f"\n[Hook]: {m}"

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(output),
                })

                print(f"> {tool_name}: {str(output)[:200]}")

            messages.extend(tool_results)

                





if __name__ == "__main__":
    hooks = HookManager()
    # Fire SessionStart hooks
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})
    
    memory_mgr.load_all()
    mem_count = len(memory_mgr.memories)
    if mem_count:
        print(f"[{mem_count} memories loaded into context]")
    else:
        print("[No existing memories. The agent can create them with save_memory.]")
    
    full_prompt = prompt_builder.build()
    section_count = full_prompt.count("\n# ")
    print(f"[System prompt assembled: {len(full_prompt)} chars, ~{section_count} sections]")

    history = []
    session = PromptSession(
        history=FileHistory(".agent_history")
    )
    while True:
        try:
            query = session.prompt("s11 >> ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        
         # /memories command to list current memories
        if query.strip() == "/memories":
            if memory_mgr.memories:
                for name, mem in memory_mgr.memories.items():
                    print(f"  [{mem['type']}] {name}: {mem['description']}")
            else:
                print("  (no memories)")
            continue
        
        if query.strip() == "/prompt":
            print("--- System Prompt ---")
            print(prompt_builder.build())
            print("--- End ---")
            continue
        if query.strip() == "/sections":
            prompt = prompt_builder.build()
            for line in prompt.splitlines():
                if line.startswith("# ") or line == DYNAMIC_BOUNDARY:
                    print(f"  {line}")
            continue
        
        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
        
