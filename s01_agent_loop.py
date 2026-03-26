import os
import subprocess

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
print(os.getenv("NVIDIA_API_KEY"))

print(client)
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# TOOLS = [{
#     "name": "bash",
#     "description": "Run a shell command.",
#     "input_schema": {
#         "type": "object",
#         "properties": {"command": {"type": "string"}},
#         "required": ["command"],
#     },
# }]

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
    }
]

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


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
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

            if func_name == "bash":
                print(f"\033[33m$ {args['command']}\033[0m")
                output = run_bash(args["command"])
                print(output[:-1])

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