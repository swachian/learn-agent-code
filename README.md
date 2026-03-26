This project comes from https://learn.shareai.run/zh/s02/, which is about an agent on LLM. The origin project is based on Anthropic apis. Although the same api style can be used through other platforms like Kimi/anthropic, they all need charging. As I only want to learn and observe, I don't intend to pay the bills. Fortunately, Nvidia provide such a platform with loads of open source LLMs, https://integrate.api.nvidia.com/v1. Most important, LLMs on Nvidia platform are free, at least currently. Nvidia is elegant, so they don't provide anthropic apis now. Callings to Anthropic apis must be converted OpenAI style. It's a snack to AI. 

In s01, an agent is built. 

s02, change the tools.

```
| Anthropic 风格   | OpenAI / NIM 风格        |
| -------------- | ---------------------- |
| `name`         | `function.name`        |
| `description`  | `function.description` |
| `input_schema` | `function.parameters`  |
| 无              | `"type": "function"`   |

```

Gave the agent 4 tools—besides s01's bash, there are also 3 tools for reading/writing/editing files. With Kimi, she always seems to lean towards using bash. I have to be very careful with my wording to get her to use the 'correct' tool. Switched to Alibaba's Qwen, and it behaves exactly like the tutorial expected from Anthropic. Qwen looks more akin to Anthropic."



