# ctlphone agent guide

本文件供 Kimi、Codex 和其他代码代理接手通用 Android 自动化代码。

## 先读

1. `README.md`
2. `docs/DEVELOPMENT.md`
3. `pyproject.toml`

开始修改前执行 `git status --short`，保留已有改动。

## 模块导航

- ADB 原语和 UI 节点：`phone_ctl/adb.py`
- 单设备租约网关：`phone_ctl/gateway.py`、`gateway_client.py`
- CLI：`phone_ctl/cli.py`
- MCP 工具：`phone_ctl/mcp_server.py`
- 授权设备解锁：`phone_ctl/device_unlock.py`
- 应用日志脱敏：`phone_ctl/log_capture.py`
- 游戏/长跑宏：`phone_ctl/game_*.py`、`plans/`

## 安全边界

- 只操作设备所有者明确授权的 Android 设备和 App。
- 不提交设备序列号、截图、UI XML、日志、解锁图案、账号、密码、Token 或本机路径。
- 不加入招商证券、东方财富或任何真实券商登录、凭据注入、账户查询和下单代码。
- 解锁信息只能隐藏输入，不能进入 argv、环境示例、审计或日志；失败不得自动重试。
- 默认通过网关取得租约，避免多个代理同时操作同一设备。
- 页面识别失败时停止，不进行盲点坐标操作。

## 修改和验证

优先扩展已有 ADB/网关接口，保持 CLI 与 MCP 语义一致。新增操作必须有参数校验、超时、错误枚举和测试替身，不在测试中接触真机。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
git diff --check
```

涉及真机的验证必须由设备所有者明确执行，并在提交前清除所有生成产物。
