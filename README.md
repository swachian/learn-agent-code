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
 
s09 选项持久化

User和feedback有什么区别？ // LLM不太分得出来
mem里面的content和description区别是什么？
都上传上去吗,欠缺search memory
 
Memory就是可以把用户的一些选择放在.memory目录下，一个option一个文件，最多load 200个文件。
同时，这个很看LLM的情况。有些LLM配合的很困难

s10 system prompt

这节较简单但却很好，涉及怎么可以组织系统提示。这个其实最基本和优美的模式，方法组合：把core/memory/tools/skills等组合起来，再一起发给LLM.

本章最大的收获是发现了Z-AI的模型，glm-5.1 ，一个拥有700 B的大模型，需要3个TB的显存运行。
以及英伟达自己根据DS思路训练的120 B的模型，nemotron-3-super-120b-a12b，这两个目前看来都非常好用。
Z-AI的polished效果尤其出色。

s11 Error Recovery 

这个也是基本的错误处理方式。不过作为agent代理，错误的处理要考虑重试/压缩/继续几种特有的情况。
其实重试算不上特有，所有的分布式系统里面，timeout+重试都是基本的操作手段。但压缩确实是LLM交互中特有的。
当token超出上限，提示LLM继续返回也是正常的。

s12 Task System

这个概念上很好理解，试验出结果也很容易。可以把复杂的任务，分解成有关联关系（DAG）的任务，建立任务间的依赖关系。然后让LLM利用Tools，skills一个一个地去解决并更新任务的状态和可否执行的关系。
这样的话，其实还是控制了上下文的规模，并生成了持久化的断点。是非常实用的agent能力。

s13 Background Tasks

对于系统开发人员而言，这是很常见的概念。不过AI交互最慢的地方其实是LLM,所以这个客户端异步的意义可能并不大。
交互是基于线程和消息队列的模式。是否需要改成Async的呢？

s14 Cron Schedule

这个是加出来的一节，其核心设计理念就是在agent里面多开一个thread,该thread负责不停地check cron job,如发现生效，就把内容写入一个队列queue. 然后agent运行时，每个回合会去drain（非阻塞）一下这个队列，如果取到queue中的notification,就会作为一个message发给LLM,就像用户又输入了一条指示一样。



