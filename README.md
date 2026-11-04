# WorkBuddy 状态联动桌宠

> 让另一台电脑上的 [dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 实时显示本机 WorkBuddy 正在做什么。

跨机器把 AI 助手的工作状态（思考中 / 执行中 / 等你确认 / 完成 / 出错）推给一只桌面宠物，让它跟着你的编码节奏动起来。

纯 Python 标准库实现，**零第三方依赖**，兼容 Python 3.9+，在 Windows 10/11 上验证通过。

---

## 背景

WorkBuddy 跑在办公机上，桌宠装在另一台机器上（客厅的副屏 / 家里的台式机）。想在不切换窗口的情况下知道"AI 现在是在干活还是卡在等我确认"，最直接的办法是让桌宠替你看。

难点在跨机器：

| 约束 | 后果 |
|---|---|
| 两台机器不在同一局域网 | 不能直连内网 IP |
| 手头只有 UU 远程（自带端口映射） | 映射是**单向**的：只能把被控端的 TCP 服务拉到主控端的回环口 |
| 两台机器都没装 Node.js | 不能用现成的 Node 方案 |
| dsh-pet 的 HTTP 面只监听 `127.0.0.1` 且**无鉴权** | 绝不能把它暴露到网络上，跨机调用只能靠同机进程转发 |

这几个约束基本决定了整体架构的长相。

## 实现思路

**一、顺着隧道的方向设计数据流。** UU 的端口映射只能"远端服务 → 本地回环"，所以不能让 WorkBuddy 主动 push，只能让桌宠那侧主动 pull：

```
WorkBuddy 所在机器（被控端）            桌宠所在机器（主控端）
┌──────────────────────────┐          ┌──────────────────────────┐
│  WorkBuddy hook          │          │  gate.cmd  ← 门控         │
│      ↓ POST /event       │          │      ↓ 隧道通才拉起        │
│  state_server.py :8787   │◄─────────│  bridge.py                │
│  （只读 /state）          │  UU 隧道  │      ↓ POST /work-status  │
└──────────────────────────┘          │  dsh-pet（DSH 桌面版）     │
                                       └──────────────────────────┘
```

**二、把"协议翻译"放在两头，中间只搬字节。** WorkBuddy 的事件模型和 dsh-pet 的状态机是两套词表，翻译必须有人做。选择放在**被控端的服务里**，这样 hook 可以退化成一条 `curl`——hook 是同步阻塞的，每毫秒都算在对话延迟里（实测 Python 版 275ms，curl 版 150ms）。

**三、状态是幂等的"最新值"，不是事件流。** 桥接器维护一个 `seq`，只处理变化；断线重连后拉一次快照即可恢复，不需要补发历史。这让 TCP 抖动、重连、重启都不会导致状态错乱。

**四、桌宠必须能自己回空闲。** dsh-pet 的 `success` / `error` / `waiting` 档位写进去后**不会自动清**，所以桥接器要按档位设置超时并主动 `DELETE`，否则桌宠会永久停在最后一个动作上。

## 主要功能模块

| 模块 | 位置 | 职责 |
|---|---|---|
| 状态服务 | `local/state_server.py` | 接收 hook 事件、映射成 dsh-pet 档位、维护 `seq`、支持长轮询 |
| hook 适配 | `local/hooks.snippet.json` | 把 WorkBuddy 的 7 个生命周期事件接到状态服务 |
| 桥接器 | `pet/bridge.py` | 拉取状态、转写、判重、超时自清、端口自动发现、三种诊断模式 |
| 门控 | `pet/gate.cmd` | 隧道可用才拉起桥接器，隧道断开立即停掉并清理桌宠 |
| 启动器 | `pet/run-bridge.cmd` | 由门控调用的薄壳，也可手动运行 |

**状态映射**（改文案只需动 `state_server.py` 的 `map_event`）：

| WorkBuddy 事件 | dsh-pet 档位 | 头顶文案 |
|---|---|---|
| `UserPromptSubmit` | `thinking` | 正在理解需求 |
| `PreToolUse`（读类工具） | `working` | 正在读资料 |
| `PreToolUse`（写类工具） | `working` | 正在改文件 |
| `PreToolUse`（Bash） | `working` | 正在执行命令 |
| `PermissionRequest` | `waiting` | 要写文件，等你点 |
| `Notification`（`permission_prompt`） | `waiting` | 要执行命令，等你点 |
| `Notification`（`idle_prompt`） | *清空* | — |
| `Notification`（其他，如 `auth_success`） | *忽略，不改状态* | — |
| `Stop` | `success` | 这轮做完了 |
| `SessionEnd` | *清空* | — |

> `task` 字段上限 40 个字符，服务端会自动截断。

**权限确认只显示工具类别，不显示命令内容。** 提示语形如「要执行命令，等你点」「要写文件，等你点」——刻意不带命令行、文件路径和参数，因为这条文案会一直挂在屏幕上直到你点掉。工具名与中文的对应关系在 `state_server.py` 的 `TOOL_ALIAS` 里，可直接增改。

## 技术栈与依赖

| 项 | 说明 |
|---|---|
| 语言 | Python 3.9+，**仅标准库**（`http.server` / `http.client` / `socket` / `subprocess`） |
| 运行环境 | Windows 10/11（门控脚本依赖 `cmd.exe`、`curl.exe`、`ping`） |
| 上游 | [WorkBuddy](https://www.workbuddy.cn) hooks、[dsh-pet](https://github.com/PC2005-cloud/dsh-pet) v0.4+ |
| 网络 | UU 远程的「端口映射」（仅 Windows 主控端、仅 TCP） |
| 打包 | 无构建步骤，拷贝即用 |

**为什么不用 Node / PowerShell**（同机空转实测，供参考）：

| 宿主 | 工作集 | 取舍 |
|---|---:|---|
| `cmd.exe` | 5.4 MB | 最省，但没法当 HTTP 服务端、也不适合解析 JSON |
| `python` | 20.8 MB | ✅ 能当服务端、能解析 JSON、两个平台都有 |
| `node` | ≈45 MB | 目标机器未安装 |
| `powershell 5.1` | 72.0 MB | 零依赖但最重（CLR + cmdlet 自动加载） |

常驻内存实测：状态服务约 30 MB，桥接器约 27 MB，门控约 5 MB。

## 目录结构

```
.
├── local/                      # 放在「跑 WorkBuddy 的那台机器」
│   ├── state_server.py         # 状态服务（映射表在这里）
│   ├── pet_hook.py             # hook 转发器（无 curl 时的备用）
│   ├── hooks.snippet.json      # 贴进 settings.json 的 hooks 片段
│   └── start-state.cmd         # 启动状态服务
├── pet/                        # 放在「跑 dsh-pet 的那台机器」
│   ├── bridge.py               # 桥接器（含 --check / --dump / --poke 等诊断）
│   ├── gate.cmd                # 门控：跟随隧道启停
│   └── run-bridge.cmd          # 由门控拉起的启动壳
├── .gitignore
└── README.md
```

## 运行与使用

### 0. 前置条件

- 两台机器都能跑 Python 3.9+，`python` 在 `PATH` 里
- 桌宠机器上 DSH 桌面版已启动，且已安装 `dsh-pet` 插件
- 桌宠设置页「桌宠配置」里，该宠物的**工作状态联动**开关已打开

### 1. 被控端（跑 WorkBuddy 的机器）

启动状态服务：

```cmd
cd /d <项目目录>\local
start-state.cmd
```

验证：

```bash
curl http://127.0.0.1:8787/health
```

装 hooks：把 `local/hooks.snippet.json` 里的 `hooks` 字段**合并**进 `~/.workbuddy/settings.json`（已有配置不要整份覆盖），或在 WorkBuddy 里用 `/hooks` 面板逐条添加。改完立即生效，无需重启。

> 系统没有 `curl.exe` 时（Win10 1803 之前），把 hook 的 command 换成
> `python -S "<项目目录>/local/pet_hook.py"`，功能一致，每次多约 125ms。

### 2. 建端口映射（在桌宠机器上操作）

UU 远程 → 目标设备 →「更多」→「端口映射」→ 添加：

| 字段 | 值 |
|---|---|
| 目标服务地址 | `127.0.0.1` |
| 目标服务端口 | `8787` |
| 本地访问端口 | `18787` |

保持「已连接」。**不要映射 DSH 自己的端口**——它的接口无鉴权，只该留在回环。

### 3. 主控端（跑桌宠的机器）

```cmd
cd /d <项目目录>\pet
python bridge.py --check
```

自检会逐项列出两端连通性、端口、单实例锁，出错时直接给下一步提示。确认无误后：

```cmd
gate.cmd
```

想开机自启，把 `gate.cmd` 的快捷方式放进 `shell:startup`，或用任务计划程序建一个「登录时」触发的任务。

### 4. 验证

```bash
# 手动推一个档位，桌宠应立刻切换动作
curl -X POST -H "content-type: application/json" \
     -d "{\"state\":\"working\",\"task\":\"链路测试\"}" \
     http://127.0.0.1:8787/state

# 清空
curl -X DELETE http://127.0.0.1:8787/state
```

## 命令行工具

`pet/bridge.py` 的诊断模式是排查主力：

| 命令 | 作用 |
|---|---|
| `--check` | 两端 + 端口 + 锁，一次全查 |
| `--discover` | 只探测 DSH 端口（枚举本机所有监听端口） |
| `--dump` | 列出本机监听端口与所属进程，人工定位用 |
| `--poke working` | 直接向 dsh-pet 写一个测试档位，验证"最后一跳" |
| `--reset` | 清空 dsh-pet 档位（桌宠卡住时用） |
| `--dry-run -v` | 只看会发什么，不真的调用 dsh-pet |

主要参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--state-url` | `http://127.0.0.1:18787/state` | 隧道本地口 |
| `--dsh` | 自动探测 | DSH 端口；桌面版是动态端口，一般不用指定 |
| `--poll-ms` | 8000 | 长轮询挂起时长 |
| `--success-ttl` | 12000 | `success`/`error` 停留多久后回空闲（ms） |
| `--waiting-ttl` | 120000 | `waiting` 最长停留（ms），防止卡在等待动作；`0` 表示不超时 |
| `--stale` | 45 | 多久无成功拉取判定为链路中断（秒） |
| `--max-fail` | 2 | 连续失败几次后退出（交给门控重启） |

## 设计说明：门控与「跟随 UU 启停」

### 判据选什么

**用隧道端口的连通性判断，不用 UU 的进程名。** 实测 UU 的进程结构不适合当判据：后台服务进程常驻（关掉界面也还在），而容易误认的那个进程其实是它内置终端的复用器，与远程控制无关。

```cmd
curl -f -s -m 1 http://127.0.0.1:18787/health
```

这条命令的语义正好是「隧道可用 **且** 映射已建立」，同时覆盖"客户端关了"和"客户端开着但映射规则被删了"两种情况，而且 UU 改名升级都不影响脚本。

### 怎么做到"关闭后很快停"

不是靠轮询发现，而是靠**连接本身**：桥接器用 8 秒长轮询保持一条常驻 TCP，隧道一关 socket 立刻被重置，桥在毫秒级就知道，连续失败 2 次（约 1 秒）后自行退出，退出前尽力清空桌宠档位。

门控的 2 秒探测只是兜底：万一桥是被强杀的、来不及清理，门控会补一次 `DELETE`。实测从断开到完全收敛约 4 秒。

## 踩过的坑

记在这里，避免二次踩：

1. **`.cmd` 文件必须是纯 ASCII。** `cmd.exe` 按 OEM 代码页读批处理，UTF-8 中文注释会被截断成半行然后当成命令执行，报错信息看起来像乱码。
2. **hook 里不要用 Python。** `import socket` 单项就要 120ms，加解释器启动共 275ms；`curl` 一行 150ms。而 `PreToolUse` 一个任务触发上百次，差距会被放大成几十秒。
3. **长轮询会顶住定时器。** 把"每轮循环检查一次超时"写在长轮询外面，会让 4 秒的 TTL 实际延后 8 秒。修法是把轮询挂起时长夹到「距离下次自清还有多久」。
4. **判重不能用状态值，要用版本号。** 自清之后服务端状态没变，按值判重会把同一个状态反复重推，形成死循环。
5. **别用"端口是否有 HTTP 响应"判断是不是 DSH。** 任何 HTTP 服务对未知路径都回 404，会把别的服务误认成 DSH。必须先按进程名过滤。

## 已知限制

- UU 端口映射**仅 Windows 主控端、仅 TCP**，所以桌宠那侧必须是 Windows。
- DSH 桌面版的 Web 端口是动态的，每次重启会变；桥接器靠枚举本机监听端口自动发现（约 1.7 秒），不要把它写死。
- 外部状态槽与 DSH 自身的会话状态共享优先级（`waiting > error > working > thinking > result > success`），桌宠机器本地也在跑任务时，远端状态可能被压住。
- 状态延迟受限于隧道与轮询，通常在几百毫秒内；不适合同步逐帧动画。

## 后续可扩展方向

- **双向联动**：`dsh-pet` 有 `--poke` 之外的接口（`/anim`、`/broadcast`、`/chat`），可以做成"点宠物 → 让 WorkBuddy 执行预设指令"。
- **更细的粒度**：按工具类型区分动作（写代码 / 查资料 / 跑测试各一套动画），映射表已预留扩展点。
- **多机聚合**：多台被控端各开一个端口，桥接器按优先级合并成一个状态源。
- **免隧道方案**：若已有 Tailscale / WireGuard 组网，可令桥接器直连，省掉 UU 依赖。
- **打包成单文件**：用 PyInstaller 打成 exe，桌宠机器上连 Python 都不用装。
- **系统托盘小工具**：把门控和诊断做成一个托盘程序，替代 `.cmd` 黑窗口。

## 致谢与许可

- 桌宠：[dsh-pet](https://github.com/PC2005-cloud/dsh-pet)，动画素材与形象不随本仓库分发，遵循其原项目许可（形象为 CC BY-NC-SA 4.0，禁止商用）。
- 本仓库代码：MIT。
