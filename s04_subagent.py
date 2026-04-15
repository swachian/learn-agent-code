import os
import subprocess
from pathlib import Path

# from anthropic import Anthropic
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

# if os.getenv("LLM_BASE_URL"):
#     os.environ.pop("ANTHROPIC_API_KEY", None)

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("NVIDIA_API_KEY"),
)

WORKDIR = Path.cwd()
MODEL = os.environ["MODEL_ID"]



SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

CHILD_TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read file contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exact text in file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}}
]

PARENT_TOOLS = CHILD_TOOLS + [{"type": "function", "function": {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}}}]

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
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"
    
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
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
    
# -- Subagent: fresh context, filtered tools, summary-only return --
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]  # fresh context

    for _ in range(30):  # safety limit
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SUBAGENT_SYSTEM},
                *sub_messages
            ],
            tools=CHILD_TOOLS,
            tool_choice="auto",
            max_tokens=2000,
        )

        msg = response.choices[0].message

        # ✅ 记录 assistant（必须带 tool_calls）
        sub_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": msg.tool_calls
        })

        # ❌ 没有工具调用 → 结束
        if not msg.tool_calls:
            break

        tool_results = []

        # ✅ 执行工具
        for call in msg.tool_calls:
            func_name = call.function.name
            args = eval(call.function.arguments)

            handler = TOOL_HANDLERS.get(func_name)
            output = handler(**args) if handler else f"Unknown tool: {func_name}"

            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(output)[:50000]
            })

        # ✅ 把 tool 结果喂回去
        sub_messages.extend(tool_results)

    # ✅ 返回最终文本（OpenAI是 msg.content）
    return msg.content or "(no summary)"
    

# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                *messages
            ],
            tools=PARENT_TOOLS,
            tool_choice="auto",
            max_tokens=2000,
        )
        
        # Append assistant turn
        msg = response.choices[0].message

        print("=== OUTPUT ===")
        print(msg.content)

        if msg.tool_calls:
            print("=== TOOL CALL ===")
            print(msg.tool_calls)

        print("=== TOKENS ===")
        print(response.usage)
        
        messages.append({
            "role": "assistant",
            "tool_calls": msg.tool_calls,
            "content": []
        })
        # If the model didn't call a tool, we're done
        if not msg.tool_calls:
            # print(msg.content)
            return
        # Execute each tool call, collect results
        tool_results = []
        for call in msg.tool_calls:
            func_name = call.function.name
            args = eval(call.function.arguments)

            if func_name == "task":
                desc = args.get("description", "subtask")
                print(f"> task ({desc}): {args['prompt'][:80]}")
                output = run_subagent(args["prompt"])
            else:
                handler = TOOL_HANDLERS.get(func_name)
                output = handler(**args) if handler else f"Unknown tool: {func_name}"

            print(f"  {str(output)[:200]}")

            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(output)
            })
        messages.extend(tool_results)


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()