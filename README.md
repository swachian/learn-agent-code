This project comes from https://learn.shareai.run/zh/s02/, which is about an agent on LLM. The origin project is based on Anthropic apis. Although the same api style can be used through other platforms like Kimi/anthropic, they all need charging. As I only want to learn and observe, I don't intend to pay the bills. Fortunately, Nvidia provide such a platform with loads of open source LLMs, https://integrate.api.nvidia.com/v1. Most important, LLMs on Nvidia platform are free, at least currently. Nvidia is elegant, so they don't provide anthropic apis now. Callings to Anthropic apis must be converted OpenAI style. It's a snack to AI. 

In s01, an agent is built. 

s02, change the tools.


| Anthropic 风格   | OpenAI / NIM 风格        |
| -------------- | ---------------------- |
| `name`         | `function.name`        |
| `description`  | `function.description` |
| `input_schema` | `function.parameters`  |
| 无              | `"type": "function"`   |


Gave the agent 4 tools—besides s01's bash, there are also 3 tools for reading/writing/editing files. With Kimi, she always seems to lean towards using bash. I have to be very careful with my wording to get her to use the 'correct' tool. Switched to Alibaba's Qwen, and it behaves exactly like the tutorial expected from Anthropic. Qwen looks more akin to Anthropic."


s03. 
A todo manager is added as a new tool. However, the most important thing is to change the description in `System`. You need tell the SYSTEM that she has to plan firstly, and then it can do the tasks one by one.

```

SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""
```

得到任务分解和结果都是类似`{'id': '1', 'text': 'Read and summarize main.py', 'status': 'completed'}`的结构。
如果你要求她必须先列出计划，就要和她明确这个要求。


s04

这个没什么好说的，就是一个类似fork,交给subagent去做


s05 skill
搞了半天就是按需把skill的内容提交上去，本质还是为了减少token的消耗。

不过skill本身，定义了code review需要做些什么
比如定义了checklist,需要的结果格式等。这些都是AI喜欢的语料，却是人类厌恶的。


s06 是讲的上下文压缩

s08 是讲的通过hooks来装配agents的功能，这个比较好，甚至可以在hook里面配置事前事后的校验

在这组间隙，加了一下otel到jaeger和grafana的集成。感叹一下，现在观察工具真是贴心。
引入jaeger的替手grafana/tempo
 
 




