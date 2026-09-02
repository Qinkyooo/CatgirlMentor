<div align="center">

# nanobot × FFXIV · 艾欧泽亚猫娘导师

**基于开源 AI Agent 框架 [nanobot](https://github.com/HKUDS/nanobot) 的二次开发：给自托管智能体装上一位 FF14 中文导师——一只来自艾欧泽亚的猫娘（米可特族 · 逐日之民），会查鱼、看房、比价、翻攻略，也能陪你扮演任何角色。**

[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![上游框架](https://img.shields.io/badge/based_on-nanobot%20(HKUDS)-orange)](https://github.com/HKUDS/nanobot)

</div>

---



## ✨ 特性

- 🐟 **钓鱼助手**：覆盖 1500+ 种鱼的资料（鱼饵、钓点、前置鱼、鱼王/鱼皇），按游戏时间推算未来鱼窗，天气按官方算法实时推算（支持「森都 / 海都 / 沙都 / 伊修加德」等别名），还能把鱼窗提醒直接挂进定时任务。
- 🏠 **房区雷达**：实时空房与抽签阶段查询（海雾村 / 薰衣草苗圃 / 高脚孤丘 / 白银乡 / 穹顶皓天），支持按房型、价格、描述筛选与推荐。
- 💰 **国服物价**：中文物品名解析 + Universalis 价格聚合，支持服务器 / 数据中心 / 大区 / “X区”口语范围，聚合价与当前挂单分开展示。
- 📖 **中文知识库**：内置 488 篇中文攻略 / 职业 / 机制 / 物品文档（2557 个检索分块），带实体与别名索引；物品简称能自动解析为正式名称。
- 🎭 **Persona 人设系统**：证据驱动地生成角色档案（素材收集 → 预览 → 应用），随时切换人设，也可一键退出恢复默认。注：如更换人设以后QQ等渠道消息仍然是旧人设需要先发送/new 开启新会话
- 📜 **回答纪律**：所有游戏数据回答不编造数值 / 时间 / 价格；来源默认不展示、按需追溯；玩家上报类数据强制附带免责声明。

## 🚀 快速开始（推荐从源码运行）
<img width="517" height="423" alt="image" src="https://github.com/user-attachments/assets/4820c967-a329-4bf9-bcab-913e1c4afcab" />

**前置**：Python 3.11+node.js
请先确认是否满足前置条件，如未满足请先看[安装前置](#手动安装)

#### 解压后进入解压目录
例如解压到D盘根目录：D:\CatgirlMentor-main
### 使用PowerShell
```bash
cd D:\CatgirlMentor-main
python -m venv .venv
```
### Windows PowerShell 激活：
```bash
.venv\Scripts\Activate.ps1
```

### 安装
```bash
python -m pip install  .
```
### 确认版本
```bash
nanobot --version
```

**快速配置**：

```bash
nanobot onboard --wizard
```
### 也可以直接运行：
```bash
nanobot webui
```
### 但后续需要在webui里设置模型
### 注意，这种方式下启动webui如果关闭powershell会停止服务，如需后台运行请在首次运行成功后使用如下命令后台运行
```bash
nanobot webui --background
```
### 查看状态及日志
```bash
nanobot gateway status
nanobot gateway logs
```
### 如需随电脑启动可让bot生成启动项随电脑启动




**接入聊天软件（可选）**：本 fork 的 QQ / Telegram 等渠道与上游一致，可参考 [`docs/guides/qq-ai-agent.md`](./docs/guides/qq-ai-agent.md)；区别只是默认行为更安静（见下文「默认行为调优」）。
### 如需使用QQ等聊天渠道，首次发送消息会受到一个配对码，在web中填写配对码即可成功配对，全渠道默认独立会话，如需在微信和QQ等不同渠道中共用会话历史可直接让bot替你修改

## 💬 试试这样问

| 你想… | 直接说 | 底层调用 |
|---|---|---|
| 查一条鱼 | “冥河灯鱼怎么钓？用什么饵、在哪个钓场、有没有前置鱼？” | `ffxiv_fishing` → `fish_info` |
| 蹲鱼王 / 设提醒 | “红龙下次鱼窗是什么时候？帮我在开钓前 10 分钟提醒我” | `ffxiv_fishing` → `fish_windows` + `cron` |
| 看天气 | “伊修加德现在什么天气？” | `ffxiv_fishing` → `weather` |
| 查物品 / 攻略 | “巨匠药水是干什么的，怎么获得？”、“暗黑骑士 6.x 手法要点” | `ffxiv_knowledge` → `item` / `guide` |
| 找工具站 | “帮我找一下 logs 的网址”、“采集时钟在哪看” | `ffxiv_knowledge` → `guide`（`tool_site=true`） |
| 买房 | “白银乡还有空房吗？500 万以内帮我推荐个 S 房” | `ffxiv_housing` → `vacancies` / `recommend` |
| 查物价 | “莫古力区的巨匠药水 HQ 均价多少？” | `ffxiv_market` → `price` |
| 换角色 | “扮演一下爱梅特赛尔克，我要跟他聊聊” | `persona` 技能（见下） |

## 🎭 内置人设：艾欧泽亚猫娘导师

默认的 `SOUL.md` / `USER.md` 模板（位于 `nanobot/templates/`）被改写为：

- **身份**：米可特族 · 逐日之民的猫娘，游历过艾欧泽亚的大皇冠导师，定位是玩家的“艾欧泽亚百科全书”；
- **称呼**：统一叫玩家「小豆芽（sprout）」，像带新人一样耐心；
- **风格**：引导式教学——先给思路再给答案；先结论后细节；带一点猫的调皮（喵～），但从不敷衍、从不编造；
- **知识边界**：攻略与数值优先用知识库和工具核对，不靠记忆硬答。

想调整人设，直接改模板即可；想完全换一个角色，用下面的 Persona 工作流。

## 🧭 Persona 人设工作流（扮演任意角色）

这是一个**证据驱动**的流程（技能定义见 `nanobot/skills/persona/SKILL.md`）：

1. **收集剧情素材**：从 FFXIV 剧情文本库检索角色台词（至多 500 条），生成 `source-dialogue.md`；
2. **收集 Wiki 事实**：`web_search` + `web_fetch`（官方资料 > 专门 Wiki > 综合 Wiki，最多 3 页），整理为 `source-wiki.md`；
3. **综合成档案**：生成 `profile.json`（性格 / 口癖 / 称呼 / 背景 / 知识边界 / 应答规则）；
4. **预览确认**：渲染 `preview.md` 给你过目，**确认前不写入任何文件**；
5. **应用 / 退出**：只替换 `SOUL.md` / `USER.md` 中受管块（`<!-- persona:managed -->` / `<!-- persona:user -->`），其余内容原样保留；说「退出人设」即可恢复默认。




## 🧰 二次开发详解

### 1. FF14 只读工具（`tools.games`）

四个工具共用一套配置与数据服务层，全部**只读**，注册在配置节 `tools.games` 下：

| 工具 | actions | 能力 |
|---|---|---|
| `ffxiv_fishing` | `fish_info` / `fish_windows` / `weather` | 鱼类静态资料（即使未来 48h 无鱼窗也返回钓点、鱼饵、前置鱼）；按游戏时间推算鱼窗；实时推算地区天气；可输出 `reminderAt`/`reminderMessage` 交给 `cron` 定时提醒 |
| `ffxiv_knowledge` | `search` / `item` / `guide` | 本地中文知识库检索；物品用途 / 获取；攻略 / 职业 / 机制；`tool_site=true` 时只查工具站目录（logs、采集时钟等） |
| `ffxiv_housing` | `vacancies` / `detail` / `recommend` | 空房（仅“现正火热预约中！”与“即将开始抽签预约！”）、指定地块详情、按价格 / 房型 / 描述筛选推荐 |
| `ffxiv_market` | `price` | 中文物品名解析 → 范围（服务器 / 数据中心 / 大区 / X区）聚合价格，默认 NQ，可显式要求当前挂单 |

### 2. 游戏域包 `nanobot/games/ffxiv/`

按职责分层的数据与服务模块：

| 分组 | 模块 | 说明 |
|---|---|---|
| 钓鱼 | `fishing*.py`、`fishcake.py` | FishCake 审查过的分版数据资产解码；鱼情快照（30 分钟 TTL）；鱼窗 / 鱼王鱼皇 / 前置鱼 / 稀有度统计 |
| 天气 | `weather.py` | 纯函数实现游戏官方天气算法，按地球时间实时推算（非抓取预报） |
| 房区 | `housing.py` | 镜像 house.ffxiv.cyou 的售卖卡片，支持中文服务器与房区名映射 |
| 物价 | `market.py` | 中文物品解析 + Universalis 价格聚合 |
| 知识库 | `knowledge*.py`、`knowledge_assets.py` | 文档收集 → 清洗 → 分块 → 建库（FTS5 全文检索 + 实体/别名索引）→ 检索 → 校验（manifest + SHA-256）的完整管线 |
| 网络与维基 | `wiki*.py`、`http.py`、`cache.py` | FFCafe（xivapi-v2）结构化事实 + 受限英文百科兜底；域名白名单的安全 HTTP 客户端；磁盘缓存 |
| 工具站 | `tool_directory.py` | Water Crystal Station（ff14.bluefissure.com）工具站目录适配（本地过滤，不追踪外链） |
| 公共 | `result.py`、`types.py`、`ffxiv-servers.json` | 统一结果 / 证据与时效类型；中国区服务器清单 |

### 3. 内置中文知识库

- 源码内置 `nanobot/games/ffxiv/data/guide.sqlite3`（约 8.7MB）+ `guide.manifest.json`：**488 篇文档、2557 个分块**（manifest 构建时间 2026-09-01），SQLite FTS5 全文检索 + 实体 / 别名索引（`documents / chunks / chunks_fts / entities / aliases / …` 共 8 张核心表）。
- 首次运行向导（`nanobot/cli/game_setup.py`）会校验清单与 SHA-256 后**自动绑定**；若配了自定义库则改为“保留并验证自定义 FF14 中文知识库”。
- 需要换成自己的库时，配置 `tools.games.guideDatabase` 指向库文件即可，启动时会做完整校验（表结构 + 清单）。
- **版本滞后声明**：知识库与游戏数据锚定于构建时点（manifest / exdschema），游戏更新后可能滞后，回答中会如实标注时效。

### 4. `game-assistant` 路由技能

`nanobot/skills/game-assistant/SKILL.md` 定义了“用户意图 → 工具 → action”的路由表与**回答契约**，核心条款：

- 工具输出即证据：不编造数值、时间、钓点或价格；不打印伪 `tool_call` 标记；
- 工具返回结构化错误时**不得**改用 `web_search` / `web_fetch` / `read_file` 兜底，按 `suggestions` 修正一次或如实报告；
- 来源默认不展示，仅当用户明确询问时才列出处；英文兜底来源必须标注；
- 房屋类回答必须包含：`玩家工具上报聚合，非官方数据，可能延迟`。

### 5. 默认行为调优（针对聊天场景）

| 改动 | 位置 | 效果 |
|---|---|---|
| `sendProgress` / `sendToolHints` 默认改为 `false` | `nanobot/config/schema.py` | 渠道默认不再刷进度与工具调用噪音（可用 `sendProgress` 按频道重开） |
| QQ `ack_message` 默认改为空串 | `nanobot/channels/qq/runtime.py` | QQ 不再回复 “Processing…” 占位 |
| onboard 向导集成知识库准备 | `nanobot/cli/commands.py` + `cli/game_setup.py` | 首次配置即自动校验 / 绑定内置知识库 |

##  手动安装

1. **Python**
官网：[python下载](https://www.python.org/downloads/windows/)

- 下载 Windows 64‑bit Installer 
- ⚠️第一页务必勾选 **Add Python to PATH**
- Install Now

2. **Node.js（自带 npm）**
官网：[node.js下载](https://nodejs.org/en/download)

- 下载 LTS 长期支持版 `.msi`
- 默认下一步，保持 Add to PATH 勾选。

> 
> 全部装完，**关闭所有终端，重新打开 PowerShell 验证**


## ⚙️ 配置参考

配置文件位于 `~/.nanobot/config.json`（支持 camelCase 别名）。FF14 工具相关配置：

```jsonc
{
  "tools": {
    "games": {
      "enable": true,                 // 总开关
      "dataDir": "~/.nanobot/games",  // 数据 / 缓存根目录
      "updateTimeoutSeconds": 5,      // 上游数据请求超时
      "guideDatabase": null,          // null=使用源码内置库；或指向自定义库路径
      "wikiCacheMb": 1024             // 维基磁盘缓存上限（16MB ~ 16GB）
    }
  },
  "channels": {
    "sendProgress": false,            // 默认静默，可对单个渠道重开
    "sendToolHints": false
  }
}
```

其余（provider / 模型 / 渠道 / 记忆等）配置项与上游 nanobot 完全一致。

## 📁 新增 / 改动文件地图

```
nanobot/
├── agent/tools/games.py                 # FF14 工具注册与配置（新增）
├── cli/game_setup.py                    # 知识库校验与绑定（新增）
├── cli/commands.py                      # onboard 向导集成（改动）
├── config/schema.py                     # tools.games.* + 频道默认静默（改动）
├── channels/qq/runtime.py               # ack_message 默认空（改动）
├── games/ffxiv/                         # 游戏域包（新增，约 30 个模块）
│   ├── data/guide.sqlite3               #   内置中文知识库（8.7MB）
│   ├── data/guide.manifest.json
│   └── fishing*.py / fishcake.py / housing.py / market.py /
│       weather.py / wiki*.py / knowledge*.py / tool_directory.py / …
├── skills/game-assistant/SKILL.md       # 意图路由 + 回答契约（新增）
├── skills/persona/SKILL.md              # 人设工作流（新增）
├── skills/persona/scripts/persona_tools.py
└── templates/SOUL.md, USER.md           # 猫娘导师默认人设（改动）
```

## 📚 数据来源与免责声明

| 领域 | 来源 | 说明 |
|---|---|---|
| 钓鱼资料 | FishCake | 审查过的、带版本的数据资产；鱼窗与天气由游戏算法本地推算 |
| 物品 / 剧情 / 维基事实 | FFCafe（xivapi-v2、剧情文本接口） | 结构化游戏数据；英文百科仅作受限兜底并明确标注 |
| 市场物价 | Universalis | 玩家上传聚合的跨服市场数据，非官方 |
| 房屋售卖 | house.ffxiv.cyou | **玩家工具上报聚合，非官方数据，可能延迟**（房区回答始终附带此声明） |
| 工具站目录 | Water Crystal Station（ff14.bluefissure.com） | 仅本地过滤，不追踪外链 |

**其他声明**：

- 本项目与 SQUARE ENIX 无任何关联；《最终幻想 XIV》相关内容版权归 SQUARE ENIX 所有。
- 各站点数据版权归相应站点 / 贡献者所有；本项目仅用于个人学习与研究。
- 游戏数据随版本更新，回答可能滞后（知识库锚定于构建时点），机器人在涉及时效时会如实标注。

## 🔗 与上游 nanobot 的关系

- 框架层能力（WebUI / TUI、多渠道、Dream 记忆、MCP、cron、子代理、OpenAI 兼容 API 等）来自上游，基础用法请查阅[上游 README](https://github.com/HKUDS/nanobot)（本仓库备份于 [`README.upstream.md`](./README.upstream.md)）与上游文档站。
- 本 fork 的全部自定义增量即上文“二次开发详解”所列内容，可在 git 历史中查看 `feat: add FFXIV assistant and persona workflows` 一次提交完整 diff。
- 跟进上游：`git fetch 上游 && git merge 上游/main`（本 fork 的增量均在 `nanobot/games`、`skills` 等独立目录与少量配置默认值上，冲突面很小）。

## ⚖️ 许可证

MIT License，继承自上游 nanobot（[HKUDS/nanobot](https://github.com/HKUDS/nanobot)），版权归上游贡献者与本仓库作者共同所有；第三方数据版权归各自所有者（见上文「数据来源与免责声明」）。
