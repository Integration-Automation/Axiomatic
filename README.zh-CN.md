# Axiomatic

[English](README.md) · [繁體中文](README.zh-TW.md) · **简体中文** · [日本語](README.ja.md)

**一台用聊天操作的自动化主机。** 你人在哪个聊天平台，就从那里对这台机器下命令，
它在机器上替你干活：桌面与窗口自动化、进程与文件操作、定时任务、可替换后端的
对话问答，以及一条长时间运行的浏览器批处理——按你用聊天编辑的队列连续出图。

**没有任何单一平台是这个项目的身份。** 聊天界面是一层适配：**一个平台一个**受监督
的进程，每个平台都能单独关掉，各有自己的锁、日志与状态。新增一个平台就是一个
transport 模块加一段配置；移除一个平台就是一个 `false`。工作负载待在适配层后面，
并不知道一条命令是从哪个平台进来的。

**适合谁**：有一台机器要在人不在的时候继续干活——那种想用手机启动、观察、调整、
停止的长时间无人值守任务，而且操作主机的入口要锁在每个平台各自的所有者身份上。

> **写作约定**：正文用泛称指代外部依赖（出图服务、目标网站、网页界面、对话后端）；
> 文件名、命令名、配置键与代码符号则照实写。

<!-- section: contents -->
## 目录

- [这是什么](#这是什么)
- [需要什么](#需要什么)
- [安装配置](#安装配置)
- [跑一个或多个平台](#跑一个或多个平台)
- [配置文件](#配置文件)
- [队列与提示词文件](#队列与提示词文件)
- [命令](#命令)
- [批处理行为](#批处理行为)
- [监督与重启](#监督与重启)
- [实际操作示例](#实际操作示例)
- [更深入的文档在哪](#更深入的文档在哪)
- [开发](#开发)

---

<!-- section: what-this-is -->
## 这是什么

```
    平台 A            平台 B            平台 C …
      │                 │                 │
      ▼                 ▼                 ▼
 ┌───────────┐     ┌───────────┐     ┌───────────┐
 │  bot 进程 │     │  bot 进程 │     │  bot 进程 │   一个平台一个进程
 │  自己的锁 │     │  自己的锁 │     │  自己的锁 │   自己的日志与状态
 │  自己的态 │     │  自己的态 │     │  自己的态 │
 └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
       └─────────────────┼─────────────────┘
                         │  磁盘上的文件（唯一的耦合）
        ┌────────────────┼──────────────────┬───────────────────┐
        ▼                ▼                  ▼                   ▼
   桌面与窗口       主机命令、作业        对话后端            出图批处理
     自动化           与调度            （可替换）        （由锁决定谁监督）
```

- **平台进程**是意图层：接命令、判断是谁在问、编辑磁盘上的文件、启动并监督工作、
  回报结果。每个进程只服务一个聊天平台，彼此不共享任何可变状态。
- **工作负载**是执行层。桌面自动化在平台进程里跑；出图批处理是它自己的受监督子进程，
  由它操作浏览器——登录、填提示词、点生成、下载。
- **中间只有文件。** 所有共享状态都在磁盘上，所以任何一边重启都不会弄坏另一边，
  批处理也永远不会 import bot、反之亦然。

唯一被允许的第三条路是**被动共享模块**（`_batch_config`、`_queue_consume`、
`_webrunner_shared` …）：两边都可以 import 它们，因为那是独立、无状态、与 driver
无关的纯代码。

### 组件

**对话平台层**——让 bot 与平台无关的那一层

| 文件 | 用途 |
|---|---|
| `axiomatic/_chat_platform.py` | 适配层接缝：身份映射、能力标志、发送参数归一化、transport 注册表 |
| `axiomatic/_telegram_transport.py` | 其中一个平台。之后每多一个平台就多一个 `_*_transport.py` |
| `axiomatic/_platform_runtime.py` | 这个进程服务哪一个平台，以及它自己的状态、锁与日志放在哪 |

**命令主体**（模块名是历史遗留，它并不绑任何平台）

| 文件 | 用途 |
|---|---|
| `axiomatic/discord_bot.py` | 13 个顶层 slash 命令 ＋ 25 个命令组（合计 269 个斜杠子命令）、身份闸、桌面与主机控制、批处理监督、对话后端编排、单张出图队列 |
| `axiomatic/_gui_control.py` | 桌面自动化门面（鼠标、键盘、窗口、剪贴板、文字识别、图片定位） |
| `axiomatic/dorossi_backend.py` | 对话后端与它的会话——多种后端藏在同一个接口后面 |

**出图这个工作负载**

| 文件 | 用途 |
|---|---|
| `axiomatic/webrunner_novelai.py` | 批处理出图器，Selenium 变体（正式默认） |
| `axiomatic/webrunner_je_only.py` | 批处理出图器，wrapper 变体（备选；`/run` 先试这个） |
| `axiomatic/_webrunner_shared.py` | 两个变体的共用核心：DOM 操作、纯函数、批处理主循环，与 driver 无关 |

**启动器**（repo 根目录）

| 文件 | 用途 |
|---|---|
| `start_platforms.py` | 把每个开着的平台各起一个受监督进程 |
| `start_discord_bot.py` | **单一平台**的监督循环（`--platform <名称>`） |
| `start_webrunner.py` | 出图批处理的监督循环 |
| `run_batch.py` | 本机一键批处理：前置检查、打印 run-plan、交棒 |
| `install_autostart.py` | 注册／移除 Windows 任务计划程序的登录任务 |

---

<!-- section: requirements -->
## 需要什么

Windows、Python 3.11 以上、一个浏览器，以及 `requirements.txt` 里的包。
`requirements.txt` **不锁版本**——fresh clone 拿到的是各个包当下的版本——但对
「本项目真的碰得到、而且有已知安全通告」的包会设下限。

| 包 | 用途 |
|---|---|
| `selenium` | Selenium 变体的浏览器驱动 |
| `je_web_runner` | wrapper 变体的浏览器驱动 |
| `urllib3` | 两个变体直接接 `ReadTimeoutError`（selenium 不转出这个异常） |
| `discord.py` | 有原生斜杠菜单那个平台的 transport 与事件循环 |
| `aiohttp` | 对外 API 调用，以及其他平台用的长轮询 transport |
| `psutil` | 进程存活探测与窗口匹配（**必需**） |
| `je-auto-control` | 桌面自动化的**唯一实现**：鼠标、键盘、窗口、剪贴板、文字识别、图片定位 |
| `pytesseract` | `/locate text find\|click\|wait` 的文字识别 wrapper。**只装这个 pip 包不够**——识别引擎要另外装并放进 PATH；没装时那三个命令回泛化提示，不会崩 |
| `comtypes` | `/locate ui …` 的元素定位。装不起来时这组命令回泛化提示，其余定位方式照常 |
| `Pillow` | `/grid` 拼 2×2 图 |
| `pyfiglet` | `/fun ascii` |
| `simpleeval` | `/fun calc`——安全求值，不是 `eval` |
| `anthropic` | 对话后端的 `api` 那一种（可选；缺了 bot 仍可启动，该路径降级回报） |
| `matplotlib` | 旧版 token 图表 helper 的兼容性测试 |

浏览器自动化库由两个批处理出图器通过 `sys.path` 从**同层目录**或 `WEBRUNNER_PATH`
导入：

```
<parent>/
├── Axiomatic/    ← 本 repo
└── WebRunner/    ← 同层 clone
```

---

<!-- section: setup -->
## 安装配置

### 一、安装依赖

```powershell
py -3 -m pip install -r requirements.txt
```

### 二、填凭据

**凭据与配置都不在版本库里。** repo 带的是模板，第一步是复制它：

```powershell
copy auth.example.md                   auth.md
copy discord_bot_token.example.md      discord_bot_token.md
copy telegram_bot_token.example.md     telegram_bot_token.md
copy bot_config.example.json           bot_config.json
```

| 文件 | 内容 |
|---|---|
| `auth.md` | 出图服务登录账号密码，两行：`username: ...` / `password: ...`（按第一个冒号分隔） |
| `discord_bot_token.md` | 有原生斜杠菜单那个平台的 bot token（整个文件就是 token，或一行 `Token: xxx`） |
| `telegram_bot_token.md` | 第二个平台的 bot token。**要用那个平台才复制**——文件不存在或是空的，就等于那个平台关着 |

不要把它们加回版本库；提交时**逐个 `git add`**，不要 `git add -A`。

### 三、配置 bot

编辑 `bot_config.json`，至少填两个值：

- `channel_id`——限频道命令只在这个频道生效；
- `owner_user_id`——你自己的用户 ID。操作主机的命令组与 `/dorossi` 只认它，
  留 `0` 的话那些命令对所有人一律拒绝。

两个都没填时 bot 会在启动时打印一段说明并干净退出，不是 traceback。

在有原生斜杠菜单的那个平台上，要在它的开发者后台把 **Message Content**、
**Presence** 与 **Server Members** 三个特权 intent 打开，否则启动会抛
`PrivilegedIntentsRequired`。

### 四、放队列内容

最少只要有一个队列有东西就能跑——其余三个会自动用 `prompt.md`、`character1.md`、
`character2.md`、`undesired.md` 当 fallback。这几个 fallback 同样不在版本库里，
复制模板过来再填：

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

队列本身通常不用手建——`/todo char1 add <角色描述>` 就会写出来。格式是**一行一条**、
不按逗号切；`todo_character2.md` 是按位置对应的，空行代表「这一对不要第二个角色」，
要保留。

---

<!-- section: running-one-or-several-platforms -->
## 跑一个或多个平台

**一个平台一个进程。** 每个进程各有自己的单实例锁、自己的日志与自己的状态文件，
全部住在 `state/<平台>/`。两个平台完全不共享可变状态，所以其中一个崩溃、重启或被
关掉都不会碰到其他平台。

把开着的平台全部拉起来：

```powershell
py -3 start_platforms.py
```

看哪些会起来、哪些不会、为什么：

```powershell
py -3 start_platforms.py --list
```

只跑其中一个：

```powershell
py -3 start_discord_bot.py --platform telegram
```

完全不用 bot 的本机批处理：

```powershell
py -3 run_batch.py
```

> ⚠️ **不要同时用 bot 的 `/run` 与本机启动器**——两边都会生一个批处理出图器，抢同一
> 份浏览器配置文件锁。

### 要同时跑两个平台，你要做的事

1. 填好那个平台的 token 文件（`<平台>_bot_token.md`）。
2. 在 `bot_config.json` 把 `platforms.<平台>.enabled` 设成 `true`，并列出那个平台的
   `owner_user_ids` 与 `allowed_chat_ids`（是**那个平台上的** id，字符串）。
3. 运行 `start_platforms.py`。

其余都是自动的：状态目录、锁、日志与自动启动任务都以平台名命名。没填凭据的平台
是**缺席，不是坏掉**——它不会被启动，也不会每次开机都抱怨一次；要问原因就跑
`--list`。

默认平台也能用同样的方式关掉（`platforms.discord.enabled: false`）。完全没有那一段
时它视为开着，因为一个 fresh clone 的 bot 什么都不做，看起来就跟「配置没生效」
一模一样。

### 出图批处理仍然只有一份

批处理是**全机共享**的资源（一个浏览器、一组队列文件、一个输出目录），所以它不跟着
平台分身。谁拿到批处理监督锁，谁就监督它；其他进程收到批处理控制命令时会回一句
「另一个进程已经在监督」，而不是再开一套——两套监督会互相终止、互相重生，而且两边
的日志看起来都正常。

编辑队列不受影响：队列本来就在磁盘上，从哪个平台编都一样有效。只有「谁监督批处理」
这一件事认锁。

### 登录时自动启动（Windows）

```powershell
py -3 install_autostart.py --install    # 幂等
py -3 install_autostart.py --status
py -3 install_autostart.py --remove
```

它会给每个开着的平台各注册一条（`\Axiomatic\Bot-<平台>`），加上批处理那一条
（`\Axiomatic\Batch`）。触发条件是**登录**而不是开机：批处理要一个真有桌面的
会话才开得起浏览器。

---

<!-- section: configuration-files -->
## 配置文件

| 文件 | 装什么 | 进版本库？ |
|---|---|---|
| `batch_config.json` | 批处理出图参数：每对几张、等待时间、工作／休息时段、浏览器定期重启 | 是 |
| `bot_config.json` | 频道与所有者 ID、角色、对话后端参数、可启动程序白名单、`platforms.*` | 否（模板：`bot_config.example.json`） |
| `presence_games.json`、`presence_music.json`、`presence_rpc.json` | 本机状态映射 | 否（模板：`*.example.json`） |
| `bot_prompts/` | 10 个纯文本文件（人设、自走循环各段守则、单张出图的默认画风后缀） | 是 |

`bot_config.json` **开机读一次**——改完要 `/sys restart` 才生效。不认识的键会被忽略
并打印一行警告，因为键名打错的症状跟「配置没生效」一模一样。

`batch_config.json` 可以用 `/config set` 在线改，批处理会在两张图之间重读它。

---

<!-- section: queues-and-prompt-files -->
## 队列与提示词文件

| 队列 | 空的时候的 fallback |
|---|---|
| `todo_prompt.md` | `prompt.md` |
| `todo_character1.md` | `character1.md` |
| `todo_character2.md` | `character2.md`（空行代表「这一对没有第二个角色」） |
| `todo_undesired.md` | `undesired.md` |

配对会把几条队列一起走，较短的那条用它的 fallback 补齐。`todo_prompt.md` 里的
`end` 是**终止标记**：批处理跑完它前面那一对就干净收工，这是把一条长队列先停下来
而不删任何东西的做法。

读取端只切行边界——一条内容本来就可能含逗号、冒号与括号——而且会把不换行空格归一化
成普通空格。写入端会拒绝含有行边界的内容，因为那会被读回成好几条。

---

<!-- section: commands -->
## 命令

在有原生斜杠菜单的平台上，命令就是**斜杠命令**：打 `/` 会自动补全，参数在发送前就
有类型与取值范围检查。没有斜杠菜单的平台则走文本入口（见下），一份实现、一组权限闸。
共 **13 个顶层 slash 命令**与 **25 个命令组**（37 个顶层条目，平台上限 100），底下
合计 **269 个斜杠子命令**。

命令组不管装几个子命令都只占一个顶层名额，所以把低频命令收进组里是唯一能长期扩展的
做法。逐条说明看 [`COMMANDS.md`](COMMANDS.md)、[`commands/`](commands/README.md)，
或者直接打 `/help`。

- 🔒 **限频道**：只在 `channel_id` 那个频道回应（**所有者可跨频道**）。
- 🌐 **跨频道**：bot 看得到的任何频道都能用。
- 🔑 **限所有者**：操作 bot 那台机器的命令，**不看 `user_roles`**。

> 🔑 **主机控制一律只有所有者能用。** `/input`、`/screen`、`/win`、`/clip`、
> `/locate`、`/macro`、`/watch`、`/proc`、`/host` **整组**受闸（新增子命令自动受
> 保护），另加 `/sys restart` 这类会动到主机的零散命令。闸在分发**之前**、而且排在
> 角色闸**之前**——`user_roles` 三份清单都空时（默认）角色闸等于停用，把桌面控制挂
> 在它下面等于没有保护。

> **所有回复一律泛化**：不暴露服务名、主机路径、文件名、PID 或原始错误文本。详细内容
> 只写 stderr 与日志。

### 🔒 限频道

| 命令 | 说明 |
|---|---|
| `/eta` | 预计完成时间（有终止标记只算到标记为止） |
| `/latest` | 上传最近 N 张产出图 |
| `/queue` | 各队列剩余条数与实际会跑的配对数 |
| `/run` | 启动批处理（可调度：in 90m / at 02:00 / cancel） |
| `/status` | 批处理状态与正在跑的变体 |
| `/stop` | 停止批处理 |

| 家族 | 子命令 |
|---|---|
| **出图队列** | `/todo dedupe\|duplicate\|find\|move\|shuffle\|swap`<br>`/todo char1 add\|addx3\|clear\|list\|pop\|remove`<br>`/todo char2 add\|clear\|default\|list\|pop\|remove`<br>`/todo negp add\|clear\|list\|pop\|remove`<br>`/todo prompt add\|clear\|default\|end\|insert\|list\|pop\|remove\|template\|unend` |
| **队列空时的默认值** | `/preset info`<br>`/preset main append\|clear\|set`<br>`/preset neg append\|clear\|set` |
| **批处理控制** | `/gen current\|image\|image_queue\|pause\|plan\|preview\|progress\|resume` |
| **产出查看** | `/out debug_show\|history\|latest_for\|rate\|sample\|stats` |
| **收藏** | `/fav clear\|list\|remove\|show` |
| **运行记录** | `/log clear\|errors\|grep\|size\|tail` |
| **运维与诊断** | `/sys audit\|backfill_paths\|cleanup_debug\|dashboard\|disk\|doctor\|git_pull\|health\|introspect_dom\|metrics\|probe_status\|restart\|undo\|update_check` |
| **进程控制** | `/proc kill\|launch\|list\|usage` |
| **批处理参数** | `/config reload\|reset\|set\|show` |
| **屏幕** | `/screen all\|gif\|info\|main\|pixel\|region\|text\|window` |
| **窗口** | `/win focus\|grid\|list\|move\|pos\|snap\|state\|wait`<br>`/win layout list\|remove\|restore\|save` |
| **键盘与鼠标** | `/input click\|hotkey\|type`<br>`/input key clear\|down\|press\|status\|up`<br>`/input mouse click\|dclick\|down\|drag\|move\|pos\|scroll\|up` |
| **剪贴板** | `/clip files\|formats\|image\|paste\|read\|set\|setimage` |
| **画面定位** | `/locate gone\|pixel`<br>`/locate image click\|find\|wait`<br>`/locate text click\|find\|wait`<br>`/locate ui click\|find\|gone\|read\|tree\|wait` |
| **宏** | `/macro delete\|edit\|insert\|list\|record\|rm_line\|run\|save\|show\|stop` |
| **主机命令与文件收发** | `/host get\|panic\|put`<br>`/host job clear\|eof\|list\|log\|run\|send\|stop`<br>`/host sh cd\|run\|stop` |
| **条件监视** | `/watch clip\|job\|list\|pixel\|port\|process\|stop\|text\|ui\|window` |
| **定时调度** | `/schedule add\|list\|remove\|run` |
| **独立监督者** | `/launcher start\|status\|stop` |

### 🌐 跨频道

| 命令 | 说明 |
|---|---|
| `/booru` | 图库搜图：tag 随机一张（可模糊；不带 tag 用默认图） |
| `/e621` | furry 取向图库随机一张（默认 NSFW；加 rating:safe 限 SFW） |
| `/grid` | 图库最新 4 张拼 2×2 上传 |
| `/help` | 命令说明（tw / cn / en） |
| `/iqdb` | 跨图库反向图搜（回前几名来源与相似度 %） |
| `/nsfw` | 图库搜图的 NSFW 快捷方式 |
| `/safebooru` | 全站 SFW 图库随机一张 |

| 家族 | 子命令 |
|---|---|
| **对话后端**（🔑 限所有者） | `/dorossi abort\|ai\|ask\|compact\|effort\|errors\|fullmode\|health\|logs\|model\|retry\|running\|status\|tokens\|workspace_clean`<br>`/dorossi allowdir add\|list\|remove`<br>`/dorossi queue clear\|detail\|failed_clear\|move\|remove\|retry_failed\|show\|undo`<br>`/dorossi session archive\|continue\|delete\|export\|list\|new\|rename\|reset\|switch` |
| **图库 tag 工具** | `/tag autocomplete\|count\|suggest\|wiki` |
| **趣味／随机** | `/fun 8ball\|ascii\|calc\|choose\|coinflip\|rand\|reverse\|roll\|rps\|timer` |
| **编码与小工具** | `/tool base64\|color\|hash\|qr\|say\|unbase64\|urldecode\|urlencode` |
| **信息与元数据** | `/info avatar\|channel\|ping\|server\|uptime\|version` |
| **公开数据查询** | `/web anime\|cat\|crypto\|dict\|dog\|fact\|github\|joke\|quote\|wiki\|xkcd` |

### `@bot <问题>`——自由提问

不接子命令的 mention 是自由提问入口。**刻意保留 mention 形式**而不是改成斜杠命令：
一条消息带得动多行内容、附件与回复上下文，选项输入框带不动；而且交互 token 的寿命
远短于一个长回合。

### 表情回应

对 bot 上传的图点 ⭐ 加入收藏、🗑️ 删文件。写入走备份机制，`/sys undo` 可恢复。

### 没有斜杠菜单的平台

在没有原生斜杠菜单的平台上，同一组命令改从文本入口进入。那个入口只为那些平台写在
[`docs/platforms.md`](docs/platforms.md)；有斜杠菜单的地方，斜杠命令仍然是唯一对外
宣传的接口。

---

<!-- section: batch-behaviour -->
## 批处理行为

1. **每次启动一次完整 setup**（每次浏览器重启也是）：登录、套用配置快照、确认页面
   在预期状态。
2. **动态消耗队列**：每个角色都重读一次队列，所以跑到一半做的编辑会在下一对生效，
   而不是下一轮。
3. **逐对循环**：出到配置的张数为止，边出边下载。
4. **完成阈值**：存够张数才从队列 pop——所以中途崩溃会重跑那一对，而不是悄悄跳过。
5. **续跑检查点**：进度以原子写入落盘，任何时刻被杀掉都能接回正确位置。
6. **浏览器定期重启**：长时间运行会让浏览器内存膨胀，所以定期回收它。
7. **事件流**：批处理追加结构化事件，bot 监视并回报。

返回码：`0` 干净收工、`1` 无事可做、`2` session setup 失败、`3` 零产出、
`4` 被挡住不重生、`5` 配置还没填（同样不重生）。常量在
`axiomatic/_supervisor.py`。

---

<!-- section: supervision-and-restart -->
## 监督与重启

每个启动器都在一个受监督的循环里跑它的子进程，带指数退避与快速失败放弃，并把子进程
的控制台输出 tee 进日志。在启动器窗口按 Ctrl+C 会干净地结束那个循环。

**单实例保护**是操作系统持有的文件锁，所以进程一消失就释放，没有任何残留标志要清：

| 锁 | 挡什么 |
|---|---|
| `state/<平台>/.<平台>.discord_bot_supervisor.lock` | 那个平台的第二个监督者 |
| `state/<平台>/.<平台>.discord_bot.lock` | 那个平台的第二个 bot 进程 |
| `.webrunner_supervisor.lock` | 第二个批处理监督者 |
| `.batch_supervisor.lock` | 决定哪一个 bot 进程监督批处理 |
| `chrome_slot.lock` | 跨进程的浏览器槽，让批处理与验证脚本不会同时开两套浏览器 |

---

<!-- section: worked-examples -->
## 实际操作示例

几个小场景，演示这个 bot 平常怎么操作。更完整、逐步注释的走查（一个场景一个文件）在
[`Examples/`](Examples/README.md)。下面命令以**有斜杠菜单的平台**为例；没有菜单的
平台，同一组命令改用文字输入（见 [`docs/platforms.md`](docs/platforms.md)）。下面每一个
id 都是占位值。

**配置并开始一次批处理。** 填队列（一行一条，跑到一半改的会在下一对生效）、标好要停在
哪里、预览，再开始并盯着它：

```
/todo prompt add    scenery, wide shot, soft light
/todo char1 add     example-character
/todo prompt end          # 停止标记：批处理跑到这里结束
/gen plan                 # 预览真正会跑的配对
/run                      # 开始（也可以 /run in 90m、/run at 02:00）
/gen current              # 正在跑的那一对
/gen progress             # 已完成／还剩几张
/eta                      # 预估完成时间（算到停止标记为止）
/gen pause                # 以及 /gen resume——检查点会留着
```

**问对话后端。** 带自由文字的 mention 就是提问入口，其余是限拥有者的会话管理：

```
@bot <你的问题，例如：帮我总结上一次批处理产出了什么、有没有哪里奇怪>
```

`/dorossi session continue all` 会一次接回所有中断的自走任务——**包含你自己中止过
的**；要让某个会话连这个都不接，用 `/dorossi session delete` 把它归档。
`/dorossi ai` 换后端，`@bot /model <别名> …` 换这个会话的模型。撞到套餐用量上限
的那一轮会**自己停进队列**、带一个墙上时钟的重跑时刻，时间到自动回来——
`/dorossi queue show` 看得到停放中的提问，`/dorossi queue remove`（用 id）取消其中一条。

**看看这台机器现在在忙什么。** 一次长批处理跑着的时候，这会显示浏览器吃了多少内存、
批处理跑了多久、整机还剩多少内存与磁盘——那是 `/gen current` 与 `/dorossi running`
都看不到的操作系统这一侧：

```
/proc usage
```

**同时跑好几个平台。** 一个平台一个受监督的进程，各有自己的锁、日志与
`state/<平台>/` 底下的状态；哪个进程握着批处理锁，批处理就由它监督：

```
py -3 start_platforms.py          # 把开着的平台全部起起来
py -3 start_platforms.py --list   # 哪些会起来、为什么
```

**不必看着的恢复。** 批处理或自走循环断了网络会停在原地，网络回来就从同一个点接着
做——没有次数上限，只有 `/stop`（批处理）或 `/dorossi abort`（循环）能让它停。撞到套餐
用量上限而停放的那一轮，会在上限重置后自己重跑，不必记得回来再问一次。

---

<!-- section: where-the-deeper-docs-are -->
## 更深入的文档在哪

| 文档 | 给谁看 |
|---|---|
| [`docs/`](docs/index.md) | 完整手册（Sphinx／Read the Docs 格式） |
| [`docs/setup.md`](docs/setup.md) | 第一次安装的逐步说明 |
| [`docs/config.md`](docs/config.md) | 每一个配置键 |
| [`docs/platforms.md`](docs/platforms.md) | 在没有斜杠菜单的平台上运行 |
| [`docs/workflow.md`](docs/workflow.md) | 配对规则、fallback、终止标记 |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 跑到一半卡住、浏览器崩溃、登录失败 |
| [`COMMANDS.md`](COMMANDS.md) | 命令总表 |
| [`commands/`](commands/README.md) | 逐组参考，含参数、取值范围与权限（由命令树生成） |
| [`architecture.md`](architecture.md) | 分层、入口、主要流程、扩展点 |
| [`CLAUDE.md`](CLAUDE.md) | 要动这个 repo 的人必须遵守的常驻硬规则 |

本机构建手册：

```powershell
py -3 -m pip install -r docs/requirements.txt
py -3 -m sphinx -b html docs docs/_build/html
```

---

<!-- section: development -->
## 开发

```powershell
py -3 -m pytest              # 全部
py -3 -m pytest test/test_platform_processes.py   # 单个文件
```

测试住在 repo 根目录的 `test/`，不在包里。整套测试有很大一部分是把本项目的硬规则
变成静态守门：模块边界、所有者专属闸、原子写入、明写文本编码、繁体用词，以及这一组
README 也在其中的文档一致性。

提交前至少要做到：

1. `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')"` 打印出 `OK`。
2. 新的斜杠命令同步写进每一份用户文档语料，而 `commands/*.md` 是用
   `py -3 axiomatic/gen_command_docs.py` 重新生成的，不是手改的。
3. 改动触及 `architecture.md` 描述的东西时，同时更新它。
4. commit 主题描述改了什么；文件逐个 stage。

完整清单是 [`CLAUDE.md`](CLAUDE.md) 的 Definition of Done。
