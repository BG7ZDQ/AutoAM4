# AM4 机队中心

一个面向 Airline Manager 4 Web 版的自托管机队仪表盘与运营辅助工具。

它可以采集机队、市场、枢纽、需求、改装和检修信息，并通过持久化待办辅助完成低价补货、营销续期、航线建设、改装和逐架起飞接管。项目针对 Realism 模式开发，目前版本为 **2.3.1**。

> [!WARNING]
> 本项目与 Airline Manager 4 及其开发商无关。自动化访问可能受到游戏规则、页面变化或频率限制影响。请使用独立测试账号评估风险，并自行控制访问频率。

## 适用范围与人工干预

> [!IMPORTANT]
> 本项目并非覆盖游戏全流程的完全自动化工具，只适用于作者当前验证过的特定运营阶段和账号状态。自动补货、营销续期、航线建设及起飞接管等规则基于固定策略，不能自动适应不同发展阶段、资金结构、机队规划或临时活动。

使用者仍需持续检查账号并手动处理或调整以下事项：

- 广告方案、环保营销及其他营销投入是否适合当前阶段
- 休息室、枢纽扩建和新枢纽建设
- 员工培训及其他长期发展项目
- 航线需求变化、票价和舱位或货舱配置
- 资金分配、机队规划，以及系统未能确认或异常暂停的待办

请勿将程序长时间无人值守视为默认安全用法。启用任何自动购买或运营功能前，应先核对配置、现金储备、当前阶段和待办内容；游戏规则、网页结构或账号状态变化后也应重新人工验证。

## 主要功能

- 客机与货机机队采集，包含航线、舱位或货舱、票价、需求、改装和检修状态
- 燃油与 CO₂ 低价自动补货，支持现金安全垫、单轮预算和库存容量限制
- 按飞机维护持久化起飞待办，落地或返场结束后再检查需求并尝试起飞
- 广告与环保营销按实际到期时间自动续期
- 客机与货机航线规划，支持多发动机、经停、收益排序和 `Maximise` 班次
- 新购、交付、建线、改装、首航的幂等恢复流程
- SSE 实时仪表盘、移动端布局、机队筛选和运行日志
- 多账号运行数据、待办、缓存与在线会话隔离

## 运行方式

需要 Python 3.10+、可用的 `curl`，以及 AM4 Web 版账号。

```bash
git clone <repository-url> am4-dashboard
cd am4-dashboard
python -m venv .venv

# Linux / macOS
. .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
python src/server.py
```

在 `.env` 中填入账号凭据后，访问 <http://127.0.0.1:5000>，点击“循环运行”。

`.env`、Cookie、CSRF 令牌和 `outputs/` 运行数据均已被 Git 忽略。不要把这些文件手工加入版本库。

## 调度策略

所有正式槽位均使用北京时间（UTC+8）：

| 时机 | 动作 |
|---|---|
| 每小时 00/30 分 | 轻量刷新市场、主页状态、枢纽和新机，并执行补货判断 |
| 每小时 29/59 分 | 低价资源周期收尾；高价时不增加在线请求 |
| 起飞待办到期 | 刷新对应飞机，检查返场状态、检修保护、燃油和需求后决定是否起飞 |
| 每日 06:00 | 全量刷新航线详情、改装、检修与需求，并补齐待办 |
| 营销即将到期 | 确认剩余时间，到期后续期并登记下一次待办 |

当天已成功完成全量采集时，服务重启会续接轻量循环；否则立即补做全量。systemd 自动接续及“续接循环”模式会继续写入现有 `run_log.txt`；普通循环/全量启动则先备份旧日志并开启新日志。在线异常采用有限退避，未知状态不会继续执行购机、建线、改装或起飞等写操作。

## 配置

复制 [.env.example](.env.example) 后按需调整：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AM4_EMAIL` | — | AM4 登录账号 |
| `AM4_PASSWORD` | — | AM4 登录密码 |
| `AM4_COST_INDEX` | `200` | 自动起飞成本指数 |
| `AM4_MIN_FUEL` | `200000` | 低于此库存时暂停自动起飞 |
| `AM4_CASH_RESERVE` | `5000000` | 自动采购后保留的现金 |
| `AM4_MAX_RESOURCE_SPEND` | `25000000` | 单轮资源采购上限 |
| `AM4_FUEL_BUY_BELOW` | `500` | 燃油采购价格阈值（严格小于） |
| `AM4_CO2_BUY_BELOW` | `125` | CO₂ 采购价格阈值（严格小于） |
| `AM4_MIN_A_CHECK_HOURS` | `5` | 自动起飞要求的最少 A-Check 剩余小时 |
| `AM4_MAX_WEAR_FOR_TAKEOFF` | `80` | 自动起飞允许的最高损坏率 |

可用 `AM4_PORT` 修改本地端口。开发或只读检查时可设置 `AM4_DISABLE_SCHEDULER=1`，阻止后台待办执行。

## 面板账号与登录

面板已内置登录体系（替代 nginx Basic Auth）：

- **首次启动**：访问 `/setup` 创建管理员账户（纯管理，不绑定游戏账号）；可选项把现有 `.env` 中的游戏账号接入为普通用户。也可在 `.env` 中配置 `AM4_ADMIN_USERNAME` / `AM4_ADMIN_PASSWORD`，启动时自动创建管理员，免去访问 `/setup`。
- **注册**：`/register` 注册后状态为 `pending`，需管理员在管理面板审核通过才能登录。
- **账号设置**：登录后主页「设置」可修改 AM4 凭据与自动化参数（成本指数、采购阈值、现金垫、A-Check/磨损保护），并独立开关自动营销、自动买油、自动买 CO₂、自动起飞。
- **多账号**：每个网站账户绑定唯一的游戏账号，运行数据按账号哈希目录隔离；管理员可审核用户、停用/删除账号，并可「以该账号身份」进入与账号主人一致的主面板。
- **并发循环**：每个账号可各自启动循环（采集子进程按账号隔离 Cookie 与会话），系统全局限制并发循环数量（`AM4_MAX_CONCURRENT_LOOPS`，默认 3）。

## 命令行调试

```bash
python src/collector.py                  # 单次全量采集
python src/collector.py --light          # 单次轻量采集
python src/collector.py --loop           # 正式循环
python src/collector.py --loop --interval 1800
```

单次模式仅用于调试；日常运行建议由仪表盘或服务管理器启动循环。

## 数据目录

每个账号的数据保存在独立的 `outputs/<账号键>/` 目录，包括：

- `fleet.csv`：机队主表
- `market_data.json`：余额、燃油和 CO₂ 快照
- `hub_list.json`：枢纽列表
- `maintenance_checks.csv`：当前检修记录
- `pending_tasks.json`：持久化待办
- `builds.csv`：航线建设状态
- `run_log.txt`：当前运行日志

`data/` 中的机场、机型和离线需求矩阵用于本地航线筛选与收益估算。

## 生产部署

仓库提供了参考配置：

- [deploy/am4.service](deploy/am4.service)：单进程 Gunicorn + 自动启动循环
- [deploy/nginx-am4.conf](deploy/nginx-am4.conf)：Nginx 反向代理与 HTTPS
- [deploy/start_loop.py](deploy/start_loop.py)：systemd 启动后的循环接续助手

参考配置假定项目位于 `/opt/am4/app`、虚拟环境位于 `/opt/am4/app/.venv`。部署时应修改域名、创建 `.env`、配置 HTTPS（certbot），并设置 `AM4_COOKIE_SECURE=1`。登录认证由应用自身提供（`/setup` 创建管理员），无需再配置 nginx Basic Auth；`start_loop.py` 通过 `src/.service_token` 中的服务令牌恢复循环，不依赖浏览器会话。

面板用户、绑定账号与设置保存在 `data/panel.db`（SQLite，已在 .gitignore 中）。请确保该目录仅服务用户可读写（如 `chmod 700 data`）。

## 测试

```bash
python -m unittest discover -s tests
```

测试环境使用无效账号并禁用后台调度，不会访问游戏或执行真实操作。

## 许可证与项目说明

- 本项目使用 Codex 协助开发。
- 除 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中另行声明的第三方内容外，本仓库采用 [CC BY-NC-SA 4.0](LICENSE)：二次开发须保留署名、不得用于商业目的，并以相同许可公开分享衍生作品。
- 二次发布时应以合理方式保留原作者署名、原项目仓库链接和本许可证链接；如有修改，应同时说明修改内容，不限定具体署名格式。
- 该许可证包含非商业限制，因此本项目属于 source-available，不是 OSI 定义下的开源软件。商业授权需另行取得作者许可。
- 航线收益模型和 `data/` 中的离线基础数据参考 [AM4Help](https://am4.pages.dev/)，第三方版权与 MIT 许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 版本变化见 [CHANGELOG.md](CHANGELOG.md)。
