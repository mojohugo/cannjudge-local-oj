# CannJudge Local OJ

Ascend C 评测服务的代码仓库，包含网页、API、带认证的 MCP、SSH worker、官方提交集成和可选维护服务。项目与 CANNJudge、华为或 OpenAI 无隶属关系。

**本仓库不包含题目数据。** 题目包、生成器与参考答案、测试规格、分布资料、成绩快照、知识资料、skill、用户提交、账户和运行数据库均须由部署者在私有环境中另行配置。默认题目目录为空，官方提交和评测 worker 关闭。

## 启动

需要 Python 3.11+。网页可在 Windows 或 Linux 运行；NPU worker 和 RSI 维护控制器需要 Linux。

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn oj.app:create_app --factory --host 127.0.0.1 --port 8765 --workers 1 --no-proxy-headers
```

首次启动生成私有的 `var/config.json` 和 `var/admin-access.txt`。后者保存随机登录密码和 API key。网页入口为 `http://127.0.0.1:8765`，MCP 路径为 `/mcp`，支持 OAuth 和 `Authorization: Bearer <API_KEY>`。

接入自己的服务器时，先配置 SSH 别名及 `config/backends.json`，安装经过审核的私有题目包，再启用 worker。官方集成还需 Node.js 22+、Playwright、自己的登录状态和私有题目配置。仓库中的官方目标、规格和计分配置是空占位，提交额度也未配置。

题目数据及知识分别位于部署环境的 `problems/`、`knowledge/`、`official_problems/`、`official_templates/`；这些目录全部禁止进入本仓库。技能目录 `skills/` 同样禁止发布。RSI 是可选的高权限维护服务，不会随网页自动启动。

## 验证与发布

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
npm ci --ignore-scripts
npm test
git add .
python scripts/audit_public.py
```

随仓库提供的检查只验证无数据安装、认证、静态资源、发布边界和网页轮询，不宣称验证某个题目的数值正确性或性能。真实评测必须在部署者的私有题目包上另行验证。

不得把线上完整目录直接同步到 Git。发布审计会拦截题目、知识、技能、归档和非空成绩/规格配置；真实记录不能因匿名化而改为允许公开。

本服务面向可信用户，不是任意不可信代码的安全沙箱，详见 [安全边界](SECURITY.md)。项目自有代码采用 [MIT](LICENSE)；CANN SDK、驱动和依赖遵循各自许可。
