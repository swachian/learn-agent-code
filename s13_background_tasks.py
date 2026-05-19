# Harness: extensibility -- injecting behavior without touching the loop.
"""
s13_background_tasks.py - Background Tasks
Run slow commands in background threads. Before each LLM call, the loop
drains a notification queue and hands finished results back to the model.
    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+
    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]
                 |              |
                 +-- notification queue --> [results injected]
Background tasks here are runtime execution slots, not the durable task-board
records introduced in s12.
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
TASKS_DIR = WORKDIR / ".tasks"

# -- TaskManager: CRUD for a persistent task graph --
class TaskManager:
    """Persistent TaskRecord store.
    Think "work graph on disk", not "currently running worker".
    """
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1
    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0
    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())
    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))
    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "blocks": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)
    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2)
    
    @classmethod
    def ensure_list(cls, x):
        if x is None:
            return []
        if isinstance(x, list):
            return x
        if isinstance(x, str):
            return [x]
        if isinstance(x, int):
            return [str(x)]
        return [str(x)]
    
    def update(self, task_id: int, status: str = None, owner: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        add_blocked_by = TaskManager.ensure_list(add_blocked_by)
        add_blocks = TaskManager.ensure_list(add_blocks)
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            if status not in ("pending", "in_progress", "completed", "deleted"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            # When a task is completed, remove it from all other tasks' blockedBy
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # Bidirectional: also update the blocked tasks' blockedBy lists
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        self._save(task)
        return json.dumps(task, indent=2)
    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)
    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "deleted": "[-]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)



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

TASKS = TaskManager(TASKS_DIR)

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

import threading
RUNTIME_DIR = WORKDIR / ".runtime-tasks"
RUNTIME_DIR.mkdir(exist_ok=True)
import uuid

STALL_THRESHOLD_S = 45  # seconds before a task is considered stalled
# -- BackgroundManager: threaded execution + notification queue --
class BackgroundManager:
    def __init__(self):
        self.dir = RUNTIME_DIR
        self.tasks = {}  # task_id -> {status, result, command, started_at}
        self._notification_queue = []  # completed task results
        self._lock = threading.Lock()
    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"
    def _output_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"
    def _persist_task(self, task_id: str):
        record = dict(self.tasks[task_id])
        self._record_path(task_id).write_text(
            json.dumps(record, indent=2, ensure_ascii=False)
        )
    def _preview(self, output: str, limit: int = 500) -> str:
        compact = " ".join((output or "(no output)").split())
        return compact[:limit]
    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]
        output_file = self._output_path(task_id)
        self.tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "result": None,
            "command": command,
            "started_at": time.time(),
            "finished_at": None,
            "result_preview": "",
            "output_file": str(output_file.relative_to(WORKDIR)),
        }
        self._persist_task(task_id)
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return (
            f"Background task {task_id} started: {command[:80]} "
            f"(output_file={output_file.relative_to(WORKDIR)})"
        )
    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue."""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        final_output = output or "(no output)"
        preview = self._preview(final_output)
        output_path = self._output_path(task_id)
        output_path.write_text(final_output)
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = final_output
        self.tasks[task_id]["finished_at"] = time.time()
        self.tasks[task_id]["result_preview"] = preview
        self._persist_task(task_id)
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "preview": preview,
                "output_file": str(output_path.relative_to(WORKDIR)),
            })
    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            visible = {
                "id": t["id"],
                "status": t["status"],
                "command": t["command"],
                "result_preview": t.get("result_preview", ""),
                "output_file": t.get("output_file", ""),
            }
            return json.dumps(visible, indent=2, ensure_ascii=False)
        lines = []
        for tid, t in self.tasks.items():
            lines.append(
                f"{tid}: [{t['status']}] {t['command'][:60]} "
                f"-> {t.get('result_preview') or '(running)'}"
            )
        return "\n".join(lines) if lines else "No background tasks."
    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications."""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs
    def detect_stalled(self) -> list[str]:
        """
        Return task IDs that have been running longer than STALL_THRESHOLD_S.
        """
        now = time.time()
        stalled = []
        for task_id, info in self.tasks.items():
            if info["status"] != "running":
                continue
            elapsed = now - info.get("started_at", now)
            if elapsed > STALL_THRESHOLD_S:
                stalled.append(task_id)
        return stalled

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
    "task_create": lambda **kw: TASKS.create(
        kw["subject"],
        kw.get("description", "")
    ),

    "task_update": lambda **kw: TASKS.update(
        kw["task_id"],
        kw.get("status"),
        kw.get("owner"),
        kw.get("addBlockedBy"),
        kw.get("addBlocks")
    ),

    "task_list": lambda **kw: TASKS.list_all(),

    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
    
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
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
   
{
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a new task with subject and optional description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["subject"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update a task's status, owner, or dependency relations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "owner": {"type": "string"},
                    "addBlockedBy": {"type": "string"},
                    "addBlocks": {"type": "string"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List all tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_get",
            "description": "Get a task by task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"}
                },
                "required": ["task_id"]
            }
        }
    },  
    {
        "type": "function",
        "function": {
            "name": "background_run",
            "description": "Run command in background thread. Returns task_id immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute in background."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_background",
            "description": "Check background task status. Omit task_id to list all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Optional task ID."
                    }
                },
                "required": []
            }
        }
    }

]

# Global prompt builder
prompt_builder = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS)

BG = BackgroundManager()
# ---------------------------------------------------
# Agent loop
# ---------------------------------------------------
import json5
TOOLS_SAFE = json.loads(json.dumps(TOOLS))
def agent_loop(messages: list, hooks: HookManager):

    turn = 0

    while True:
        notifs = BG.drain_notifications()
        if notifs and messages:
            with tracer.start_as_current_span(
                f"backgournd"
            ) as agent_span:

                notif_text = "\n".join(
                    f"[bg:{n['task_id']}] {n['status']}: {n['preview']} "
                    f"(output_file={n['output_file']})"
                    for n in notifs
                )
                agent_span.add_event(
                    "background_result_received",
                    {
                        "task_count": len(notifs),
                        "preview": notif_text[:1000],
                    }
                )
                                        
                messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})


        turn += 1

        with tracer.start_as_current_span(
            f"agent_turn_{turn}"
        ) as agent_span:

            agent_span.set_attribute(
                "agent.turn",
                turn
            )

            agent_span.set_attribute(
                "agent.message_count",
                len(messages)
            )

            with tracer.start_as_current_span(
                "llm_call"
            ) as llm_span:

                llm_span.set_attribute(
                    "llm.model",
                    MODEL
                )

                llm_span.set_attribute(
                    "llm.message_count",
                    len(messages)
                )
                
                preview = str(messages[-3:])[:1000]

                llm_span.set_attribute(
                    "llm.prompt_preview",
                    preview
                )
                system = prompt_builder.build()
                llm_span.set_attribute("llm.system_prompt", system)
                response = client.chat.completions.create(
                    model=MODEL,

                    messages=[
                        {
                            "role": "system",
                            "content": system,
                        },
                        *messages
                    ],

                    tools=TOOLS_SAFE,

                    tool_choice="auto",

                    max_tokens=8000,
                )

                # Token usage
                if response.usage:

                    llm_span.set_attribute(
                        "llm.prompt_tokens",
                        response.usage.prompt_tokens
                    )

                    llm_span.set_attribute(
                        "llm.completion_tokens",
                        response.usage.completion_tokens
                    )

                    llm_span.set_attribute(
                        "llm.total_tokens",
                        response.usage.total_tokens
                    )

                    llm_span.set_attribute("response", str(response.choices[0].message)[0:1000])
                    
            assistant_message = response.choices[0].message

            # ----------------------------------------
            # Append assistant message
            # ----------------------------------------

            messages.append({
                "role": "assistant",
                "content": assistant_message.content or "",
                "tool_calls": assistant_message.tool_calls,
            })

            tool_calls = assistant_message.tool_calls

            # ----------------------------------------
            # Final response
            # ----------------------------------------

            if not tool_calls:

                agent_span.set_attribute(
                    "agent.finished",
                    True
                )

                print(assistant_message.content)
                return

            manual_compact = False

            # ----------------------------------------
            # Execute tools
            # ----------------------------------------

            tool_results = []
            
            for tool_call in tool_calls:

                tool_name = tool_call.function.name
                agent_span.add_event(
                    "tool_selected",
                    {
                        "tool.name": tool_name,
                    }
                )

                args = json5.loads(
                    tool_call.function.arguments
                )
                ctx = {"tool_name": tool_name, "tool_input": args}

                pre = hooks.run_hooks("PreToolUse", ctx)
                
                for m in pre.get("messages", []):
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"[Hook]: {m}",
                    })
                    
                if pre.get("blocked"):
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"Blocked: {pre.get('block_reason')}",
                    })
                    return
                with tracer.start_as_current_span(
                    f"tool:{tool_name}"
                ) as tool_span:

                    tool_span.set_attribute(
                        "tool.name",
                        tool_name
                    )

                    tool_span.set_attribute(
                        "tool.args_preview",
                        str(args)[:300]
                    )

                    try:

                        if tool_name == "compact":

                            manual_compact = True

                            output = "Compressing..."
                            
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": str(output),
                            })

                        else:

                            handler = TOOL_HANDLERS.get(
                                tool_name
                            )

                            output = (
                                handler(**args)
                                if handler
                                else f"Unknown tool: {tool_name}"
                            )

                        tool_span.set_attribute(
                            "tool.success",
                            True
                        )

                    except Exception as e:

                        output = f"Error: {e}"

                        tool_span.record_exception(e)

                        tool_span.set_attribute(
                            "tool.success",
                            False
                        )

                    tool_span.set_attribute(
                        "tool.output_length",
                        len(str(output))
                    )
                    ctx["tool_output"] = output
                    post = hooks.run_hooks("PostToolUse", ctx)
                    
                    for m in post.get("messages", []):
                        output += f"\n[Hook]: {m}"

                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(output),
                    })
                    messages.extend(tool_results)


                print(f"> {tool_name}:")
                print(str(output)[:200])

                





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
            query = session.prompt("s13 >> ")
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
        
