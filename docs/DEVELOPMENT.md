# Development guide

## 本地安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp deploy/gateway.env.example .env
```

`.env` 只在本机保存。`PHONE_SERIAL` 应由每位使用者从自己的 `adb devices` 输出中选择，不要提交真实序列号。

## 基本检查

```bash
python -m phone_ctl.cli gateway start
python -m phone_ctl.cli gateway doctor
python -m phone_ctl.cli --project example current
pytest -q
```

## 修改原则

- `adb.py` 负责低层命令和解析，`gateway.py` 负责租约/审计，调用方使用 `GatewayPhone`。
- CLI 和 MCP 应复用同一个实现，不复制 ADB 调用逻辑。
- 所有外部输入限制长度、格式和超时；日志不得记录操作参数中的敏感内容。
- 测试使用 fake phone 或临时 Unix socket，不连接真实设备。
- 新增截图、UI dump 或日志功能时，默认输出路径必须被 `.gitignore` 覆盖。

## 与 FoundF 联调

FoundF 通过 `PHONE_CTL_HOME` 指向本仓库，并通过 `FOUNDF_ADB_SERIAL` 在本地钉定授权设备。这两个值都不得进入任一仓库。

本公开版本只提供通用 Android 自动化能力，不包含真实券商登录或交易接口。
