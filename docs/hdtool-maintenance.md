# HD Tool 集成 fork：维护、升级和回退

本仓库是 [SimpliAj/twitchdropsminer](https://github.com/SimpliAj/twitchdropsminer) 的正式 GitHub fork。HD Tool 继续在 [AthlonHD/hdtool](https://github.com/AthlonHD/hdtool) 独立维护；两者是运行时依赖，不是合并仓库、子模块或源码拷贝。

## 仓库与分支

| 项目 | 归属 |
| --- | --- |
| 掉宝引擎与原 Dashboard | 上游维护，尽量保持原结构 |
| `src/config/server_mode.py` | fork 的小型服务器模式适配 |
| `src/__main__.py`、`src/auth/browser_login.py` | 两个最小接入点：绑定地址、远程登录 |
| `integrations/hdtool/` | fork 拥有的多人入口、账号隔离、页面适配、测试、集成协议 |
| `lang/简体中文.json` | fork 补齐的中文登录助手提示 |
| HD Tool 的 `TdmHost.cs` / `WindowsChildJob.cs` | Agent 启动、健康检查、退出与进程树清理 |
| `tdm-platform` | 部署运行数据，不属于任何仓库 |

`origin` 指向 `https://github.com/AthlonHD/twitchdropsminer.git`；`upstream` 指向 `https://github.com/SimpliAj/twitchdropsminer.git`。`hdtool` 是本 fork 的默认集成分支，部署从该分支经过验证的提交。`main` 保留上游基线，日常 fork 修改不要推到上游 `main`，也不要强制覆盖 `hdtool` 历史。

初始验证基线为上游 `5c4668d`（v1.8.7）。`integrations/hdtool/integration.json` 记录协议与上次验证的上游基线。它不是自动更新机制，也不代替真实测试结果。

## 首次部署

```powershell
git clone --branch hdtool https://github.com/AthlonHD/twitchdropsminer.git
cd twitchdropsminer
git remote add upstream https://github.com/SimpliAj/twitchdropsminer.git
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe integrations/hdtool/check.py
```

正常运行仍由 `HdTool.Agent` 启动；不用单独运行 `main.py` 或网关。Agent 的 `Tdm.Directory` 指向本 fork，`PythonPath` 指向其中虚拟环境。完整配置见 [集成协议与部署](../integrations/hdtool/README.md)。HD Tool 不自动执行 `git pull`、安装依赖、修改 fork 源码或在线更新共享代码。

首次从旧单实例升级时，网关把旧 `data` 复制给管理员，保留原目录；已有 `tdm-platform/users.json` 时直接复用平台状态，不重新创建管理员。迁移部署目录时将 `PlatformDataDirectory` 指向原绝对路径，禁止把它换成空目录，否则会被当成全新安装。

## 合并上游更新

**在独立开发 checkout 操作，不在正在运行的部署目录直接切换分支或合并。** 以下以 `sync/upstream-20261029` 为示例，每次用新的分支名：

```powershell
git status --short                     # 必须干净；不要自动 stash 或丢弃本地工作
git fetch origin
git fetch upstream
git switch hdtool
git merge --ff-only origin/hdtool
git switch -c sync/upstream-20261029
git merge --no-ff upstream/main
```

发生冲突时先解决并检查差异，不能用整文件覆盖来“保留本地”或“全部采用上游”。重点复核两个服务器模式接入点、中文翻译，以及上游 HTTP API / Socket.IO / 登录助手的接口变化。大部分多人定制在独立目录；这降低冲突范围，但不能保证未来上游变更零冲突或完全兼容。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe integrations/hdtool/check.py
.\.venv\Scripts\python.exe -m unittest tests.test_server_mode tests.test_browser_login -v
.\.venv\Scripts\python.exe -m unittest discover -s integrations/hdtool/tests -v
git diff --check
```

更新 `integration.json` 中 `tested_upstream_commit` 为实际测试过的上游 SHA，记录兼容性变化。将同步分支推到 **自己的 fork**，创建合入 `hdtool` 的 PR，CI 通过并复核后合入：

```powershell
git push -u origin sync/upstream-20261029
```

此流程不会给 SimpliAj 上游提交 PR；向上游贡献是另一项明确操作。不要对已共享的 `hdtool` 做 rebase + force push。

## 双仓库版本记录

每次部署记录以下值：

```powershell
git -C <TDM目录> rev-parse HEAD
git -C <HDTool目录> rev-parse HEAD
git -C <HDTool目录> status --short
```

再记录 Agent 构建版本、协议版本、Python 版本和部署时间。如果 HD Tool 有未提交改动，HEAD 无法完整代表运行产物，必须标注 dirty 并保留本次构建差异/备份；不能把 HEAD 冒充成已发布版本。协议兼容时升级 TDM 不需要重编译 Agent；协议主版本改变时，两个仓库必须协调升级。

## 正式升级与回退

1. 在开发副本合并、测试后准备一个新的部署 checkout，固定到已验证提交；在新目录创建虚拟环境、安装依赖和浏览器组件。
2. 停止可见的 Agent 控制台，确认其网关和 TDM 子进程退出。备份原 Agent 本地配置及 `PlatformDataDirectory`。运行状态中的目录不能视作一致的数据备份。
3. 仅切换 Agent 的 `Tdm.Directory` / `PythonPath`；`PlatformDataDirectory` 保持原绝对路径。
4. 启动 Agent，验证 8765/8766 未受影响，8767 健康；普通用户直达中文 Dashboard，管理员进入 `/portal`，WebSocket 和账号运行状态正常；内部端口仅监听 `127.0.0.1`。
5. 回退时停止 Agent，恢复旧源码/虚拟环境路径和旧 Agent（如有升级）。仅在新版确实修改了数据格式时，按记录恢复对应离线备份；不要把旧数据盲目覆盖到仍运行的实例。

平台用户目录、Twitch cookies、管理员密码、日志和临时文件均已忽略。提交前检查 `git diff --cached --name-only`，禁止 `git add -f` 把这些数据加入 fork。

## 登录助手说明

现在还提供 `/portal/twitch-login` 的免安装网页远程登录试用入口：由服务器临时浏览器显示 Twitch 登录页，用户从网页操作；无需用户本机安装软件。此模式的画面和输入已实测，真实 Twitch 登录是否通过仍需账号本人测试，服务器 IP 或浏览器验证可能失败。它归 fork 的 `integrations/hdtool/remote_login.py`，保持 Agent 协议 v1 不变。资源限制、生命周期与验证边界见集成文档。本机助手继续作为备用。

服务器不弹 Twitch 登录窗口。用户在自己的电脑运行 Dashboard 提供的助手，需要 Python 3 与 Chrome/Edge；普通用户输入管理员创建的用户名，管理员还需平台密码。本人在浏览器登录 Twitch，助手只向该用户的服务器实例发送会话。公网从 HTTPS 反代域名进入 Dashboard 后再复制命令，不能把局域网 IP 命令发给外网用户。

普通用户按当前需求不设密码：知道用户名即可进入对应空间，这不是强身份认证。用户中心无开放注册，管理员账号仍使用密码；这是应用级分离，不是 OS/容器级安全沙箱。
