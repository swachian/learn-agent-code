This project comes from https://learn.shareai.run/zh/s02/, which is about an agent on LLM. The origin project is based on Anthropic apis. Although the same api style can be used through other platforms like Kimi/anthropic, they all need changing. As I only want to learn and observe, I don't intend to pay the bills. Fortunately, Nvidia provide such a platform with loads of open source LLMs, https://integrate.api.nvidia.com/v1. Most important, LLMs on Nvidia platform are free, at least currently. Nvidia is elegant, so they don't provide anthropic apis now. Callings to Anthropic apis must be converted OpenAI style. It's a snack to AI. 

In s01, an agent is built. 

s02, change the tools.


| Anthropic 风格   | OpenAI / NIM 风格        |
| -------------- | ---------------------- |
| `name`         | `function.name`        |
| `description`  | `function.description` |
| `input_schema` | `function.parameters`  |
| 无              | `"type": "function"`   |


The agent is given 4 tools—besides s01's bash, there are also 3 tools for reading/writing/editing files. With Kimi, she always seems to lean towards using bash. I have to be very careful with my wording to get her to use the 'correct' tool. Switched to Alibaba's Qwen, and it behaves exactly like the tutorial expected from Anthropic. Qwen looks more akin to Anthropic."


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

加入了otel

s08 是讲的通过hooks来装配agents的功能，这个比较好，甚至可以在hook里面配置事前事后的校验

在这组间隙，加了一下otel到jaeger和grafana的集成。感叹一下，现在观察工具真是贴心。
引入jaeger的替手grafana/tempo
 
s09 选项持久化

User和feedback有什么区别？ // LLM不太分得出来
mem里面的content和description区别是什么？
都上传上去吗,欠缺search memory
 
Memory就是可以把用户的一些选择放在.memory目录下，一个option一个文件，最多load 200个文件。
同时，这个很看LLM的情况。有些LLM配合的很困难
Nemotron可以较好地区分feedback和User.

s10 system prompt

这节较简单但却很好，涉及怎么可以组织系统提示。这个其实最基本和优美的模式，方法组合：把core/memory/tools/skills等组合起来，再一起发给LLM.

本章最大的收获是发现了Z-AI的模型，glm-5.1 ，一个拥有700 B的大模型，需要3个TB的显存运行,价格约200万人民币。
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


s15 Agent teams

自己拉起多个代理，每个代理一个线程，分别执行不同的任务。并且可以在整个周期内保持活跃，甚至进行状态保存。
通过.team/.inbox下面的文件进行交互，每个agent有一个同名的文件作为信箱。


s18 Worktree Isolation

思路是避免两个agent同时修改同一个文件引起冲突，把各自的修改房贷.worktree. 但具体怎么放呢？

```
git worktree add -b wt/feature-login \
    /repo/.worktrees/feature-login \
    HEAD
```

核心是利用了git的特性。和checkout不同，利用git worktree可以同时添加一个branch并把内容checkout到指定的目录，在此就是`.worktrees`搭配feature名称构成的目录名。这样就可以让两个agent在不同的.worktrees下面工作了。


s19 MCP & Plugin

本地的能力都叫Harness,但本例中基本都是自己手写的本地能力。而MCP允许你集成远端的或者标准的各种能力。

mcp server是一个进程，支持一套mcp之间交互的协议。协议的作用就是做服务（tools）发现，就可以获得能力列表。
不过理论上，MCP还有resources/list, prompts/list, sampling/list等等。
然后，agent就可以在接受命令的时候考虑使用tools了。
具体的执行也是agent通知server去做，agent只是负责填写参数。

plugin.json
```
{
  "name": "full-ai-tools",
  "version": "1.0.0",
  "mcpServers": {
    "git": {
      "command": "uvx",
      "args": [
        "mcp-server-git",
        "--repository",
        "/mnt/windows_data/microfocus/alm_mng"
      ]
    }
  }
}
```

git_tool的某个功能

```
{
  "properties": {
    "repo_path": {
      "description": "The path to the Git repository.",
      "title": "Repo Path",
      "type": "string"
    },
    "branch_type": {
      "description": "Whether to list local branches ('local'), remote branches ('remote') or all branches('all').",
      "title": "Branch Type",
      "type": "string"
    },
    "contains": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "The commit sha that branch should contain. Do not pass anything to this param if no commit sha is specified",
      "title": "Contains"
    },
    "not_contains": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "The commit sha that branch should NOT contain. Do not pass anything to this param if no commit sha is specified",
      "title": "Not Contains"
    }
  },
  "required": [
    "repo_path",
    "branch_type"
  ],
  "title": "GitBranch",
  "type": "object"
}
```

执行命令的时候，agent象rpc一样，把命令和参数传递给mcp启动的subprocess.然后再从取得的结果中对内容进行分析。

```
    "web-search": {
      "command": "npx",
      "args": [
        "-y",
        "--registry=https://registry.npmmirror.com",
        "@zhafron/mcp-web-search"
      ],
      "env": {
        "DEFAULT_SEARCH_PROVIDER": "duckduckgo"
      }
    }
```
这样一段，又可以给agent增加搜索能力了。

```
mcp.arguments	
{
    "q": "张靓颖 简介",
    "lang": "zh",
    "provider": "duckduckgo"
}

mcp.result	
"{
  "items": [
    {
      "title": "张靓颖_百度百科",
      "url": "https://baike.baidu.com/item/%E5%BC%A0%E9%9D%93%E9%A2%96/141837",
      "snippet": "张靓颖（JaneZhang），1984年10月11日出生于四川省成都市，中国内地女歌手。2005年，参加湖南卫视选秀节目《2006年，发行首张录音室专辑《2007年，在美国洛杉矶举行个人售票演唱会2008年，在日本首相官邸举行的活动中献唱 …",
      "source": "bing"
    },
    {
      "title": "张靓颖 - 维基百科，自由的百科全书",
      "url": "https://zh.wikipedia.org/wiki/%E5%BC%A0%E9%9D%93%E9%A2%96",
      "snippet": "张靚（liàng）颖（英語：Jane Zhang，1984年10月11日—），出生于四川省成都市，中國女歌手。参与湖南卫视《2005年超级女声》比赛获得季军，演唱多首影視主题曲包括《画心》、《终于等到你》、《天下无双》等 ，并凭电影《画皮》主题曲《画心》获得第28屆香港電影金像獎最佳原创电影歌曲。",
      "source": "bing"
    },
    {
      "title": "张靓颖的人生 - 知乎",
      "url": "https://zhuanlan.zhihu.com/p/23125813575",
      "source": "bing"
    },
    {
      "title": "从超女季军到国际舞台常客，张靓颖20年蜕变史全揭秘",
      "url": "https://www.sohu.com/a/930952740_121371901",
      "snippet": "2025年9月2日 · 如今，张靓颖早已成为华语乐坛的标志人物。 她的歌声陪伴了无数人，她的经历也让很多年轻人看到坚持与努力的意义。 从成都酒吧的驻唱歌手，到《超女》舞台上的季军，再到格莱美红毯和国际舞 …",
      "source": "bing"
    },
    {
      "title": "张靓颖 _ 百科",
      "url": "https://baike.sogou.com/m/v53271.htm",
      "snippet": "2006年，发行个人首张音乐专辑《The One》，凭借该专辑获得第6届中国金唱片奖通俗类女演员奖 4。 2007年，成为继崔健后、第二位在美国举行演唱会的中国内地歌手。 2010年"

```

确实非常的强悍

加了一个mcp list的小功能 

```
MCP Commands:
  /mcp list              - List all connected MCP servers
  /mcp show <server>     - Show detailed tools of a specific server
  /mcp tool <server> <tool> - Show detailed schema of a specific tool
  /mcp search <keyword>  - Search tools by keyword

```

worktree是git的一个特性，可以支持多个特性在不同的目录下并行开发。提交也可以考虑不同的目录，提交到对应的分支上。