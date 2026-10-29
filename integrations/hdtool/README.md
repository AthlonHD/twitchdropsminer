# HD Tool ↔ TDM 集成协议 v1

本目录随 **TDM fork** 版本化。HD Tool 不打包本目录的副本；它从配置的 fork checkout 启动 `gateway.py`。不得把 Python 网关重新复制回 HD Tool，也不得在 Agent 启动时给原上游打补丁。

## 启动协议

HD Tool 验证 `integration.json` 的 `name=hdtool-tdm`、`contract_version=1`，然后使用该 fork 的 Python 虚拟环境启动本目录网关，工作目录为 fork 根目录。

| 环境变量 | 含义 |
| --- | --- |
| `TDM_SOURCE_DIR` | 当前 fork checkout 的绝对路径，须与实际入口所属目录一致 |
| `TDM_DATA_DIR` | 平台数据目录（Agent 的 `PlatformDataDirectory`），独立于源码和单用户 `data` |
| `TDM_PORT` | 唯一对外平台端口，默认 8767 |
| `PYTHONUTF8` | Agent 设置为 `1` |

网关为每位用户启动 `main.py`，另设 `TDM_HOST=127.0.0.1`、`TDM_PORT=<内部随机端口>`、`TDM_REMOTE_LOGIN=1`、`TDM_DATA_DIR=<用户专属目录>`。网关内部密码是每实例的随机值，前端不持有，也不接受客户端指定上游地址。

`GET /healthz` 返回 HTTP 200 与 `{"status":"ok","mode":"multi-user"}` 代表网关已启动，不代表全部用户已完成 Twitch 登录；各用户实例异步恢复。Agent 退出时关闭所属进程树，Windows Job Object 覆盖强制结束。

## Agent 配置

写入 Agent 程序旁的 `appsettings.local.json`（示例路径按实际安装位置修改）：

```json
{
  "Agent": {
    "Tdm": {
      "Enabled": true,
      "MultiUser": true,
      "Port": 8767,
      "Directory": "C:\\Services\\twitchdropsminer",
      "PythonPath": ".venv\\Scripts\\python.exe",
      "PlatformDataDirectory": "C:\\ServiceData\\hdtool\\tdm-platform"
    }
  }
}
```

`Directory` 相对路径以 Agent 程序目录为基准；`PythonPath` 相对 fork 根目录。建议平台数据始终填绝对路径。空 `PlatformDataDirectory` 由 Agent 解析为其数据目录下的 `tdm-platform`；`DataDirectory` 仅用于 `MultiUser=false` 的旧单实例模式，不是多人平台状态路径。

## HTTP 与用户行为

- `/portal/login`：平台登录；普通用户只填已创建的用户名，管理员需要密码。
- `/portal`：管理员用户管理。普通用户访问会跳回 `/`。
- `/`：会话对应的独立 Dashboard，固定简体中文。管理员可从顶栏返回用户中心；普通用户顶栏只有正常布局的退出按钮，无语言切换 UI。
- `/portal/api/users`：仅管理员创建/列出用户；禁用账号立即撤销登录并停止该实例，无公开注册。
- `/api/accounts/overview` 和批量操作：平台实时实例清单。管理员可管理全部，普通用户仅能访问自己；不使用上游的默认 8080/8082 注册表。
- `/socket.io/`：HTTP polling 和 WebSocket 均按平台会话选择实例；禁用、退出或会话失效后断开。
- `/login-helper.py`：公开下载不包含凭证的助手，提交 Twitch 会话前必须取得平台登录会话；普通用户不要求密码，管理员要求密码。

上游的源码更新、账号目录切换、原密码修改、任意实例注册、代理/通知 URL 与共享 Discord 绑定不透传。Dashboard 通过一份 API 白名单访问自己的实例。上游新增功能需要在 fork 复核后加入白名单，不能盲目放通所有路径。

## 反代

公网只转发 8767，保留原始 `Host` 和 `X-Forwarded-Proto`，支持 WebSocket Upgrade，关闭实时流缓冲，放宽 Socket.IO 读取超时。使用独立域名根路径；子路径部署尚未验证。反代在同机时上游为 `http://127.0.0.1:8767`，反代在别处时使用服务器内网 IP，并只允许可信反代访问平台端口。

登录助手命令从浏览器当前地址生成：通过公网 HTTPS 域名访问页面，就会生成该域名的命令。不要缓存登录、Dashboard 或助手响应。普通用户免密码的身份限制见维护文档。

## 验证与兼容性

```powershell
.\.venv\Scripts\python.exe integrations/hdtool/check.py
.\.venv\Scripts\python.exe -m unittest tests.test_server_mode tests.test_browser_login -v
.\.venv\Scripts\python.exe -m unittest discover -s integrations/hdtool/tests -v
```

测试覆盖角色跳转、普通用户免密码、管理员权限、HTTP/Socket.IO 按用户分流、撤权、跨站请求、批量操作范围、遗留 8080、固定中文、真实上游登录助手结构与顶栏模板适配。测试不登录 Twitch，不要求凭证。

改变环境变量含义、入口路径、健康响应或会话接口时必须评估协议主版本，并协调 HD Tool；普通 UI 或上游矿工更新不应无故改变协议。完整上游合并流程见 [维护文档](../../docs/hdtool-maintenance.md)。
