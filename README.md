# AM4 机队中心

一个面向 Airline Manager 4 Web 版的自托管机队仪表盘与运营辅助工具。

它可以采集机队、市场、枢纽、需求、改装和检修信息，并通过持久化待办辅助完成低价补货、营销续期、航线建设、改装和逐架起飞接管。项目针对 Realism 模式开发，目前版本为 **3.0.0**。

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
- 内置面板登录体系：注册/登录、管理员审核、管理面板、审计日志与多账号模拟
- 循环开关状态按账号持久化，服务重启自动恢复；停止即全面停运
- Linux 一键安装脚本（Nginx HTTPS + systemd 开机自启）

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

每个账号的循环开关状态会持久化保存：启动循环写入、手动停止移除。服务或系统重启后，系统按该状态自动恢复原本在运行的账号；停止即全面停运（循环、待办调度与市场抓取同步暂停），只有手动再次启动循环才会恢复。单次全量/轻量运行不改变循环开关状态。在线异常采用有限退避，未知状态不会继续执行购机、建线、改装或起飞等写操作。

## 配置

复制 [.env.example](.env.example) 后按需调整：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AM4_EMAIL` / `AM4_PASSWORD` | — | 面板引导游戏账号（可留空：面板与 `/setup` 照常启动，只有启动该账号的采集循环时才要求凭据） |
| `AM4_COST_INDEX` | `200` | 自动起飞成本指数 |
| `AM4_MIN_FUEL` | `200000` | 低于此库存时暂停自动起飞 |
| `AM4_CASH_RESERVE` | `5000000` | 自动采购后保留的现金 |
| `AM4_MAX_RESOURCE_SPEND` | `25000000` | 单轮资源采购上限 |
| `AM4_FUEL_BUY_BELOW` | `500` | 燃油采购价格阈值（严格小于） |
| `AM4_CO2_BUY_BELOW` | `125` | CO₂ 采购价格阈值（严格小于） |
| `AM4_MIN_A_CHECK_HOURS` | `5` | 自动起飞要求的最少 A-Check 剩余小时 |
| `AM4_MAX_WEAR_FOR_TAKEOFF` | `80` | 自动起飞允许的最高损坏率 |
| `AM4_MAX_CONCURRENT_LOOPS` | `3` | 全局并发循环数上限 |
| `AM4_MAX_SSE_CLIENTS` | `8` | 实时推送连接数上限（每个连接占用一个 gunicorn 工作线程，应留出普通请求余量） |
| `AM4_SETUP_TOKEN` | — | 网页初始化令牌：`/setup` 创建管理员时必须提供；留空则网页初始化禁用 |
| `AM4_TRUST_PROXY` | `0` | 部署在单一可信 nginx 反代后置 `1`，按 `X-Forwarded-For` 还原真实客户端 IP |
| `AM4_DEBUG_TEMPLATES` | `0` | 调试：置 `1` 时模板改动即时生效（生产保持 `0`） |
| `AM4_PANEL_DB` | `data/panel.db` | 面板账号库路径 |
| `AM4_COOKIE_SECURE` | `0` | HTTPS 部署时置 1（会话 Cookie 仅走 HTTPS，并附加 HSTS） |
| `AM4_ADMIN_USERNAME` / `AM4_ADMIN_PASSWORD` | — | 首次启动自动创建管理员（纯管理身份；用户名 2~8 位英文字母/数字，可含空格、`_`、`/`，头尾不允许空格） |
| `AM4_PROTECTED_ACCOUNTS` | — | 受保护账号（逗号分隔）：启动循环、待办执行、市场抓取、在线建设与实时精算一律拒绝，防止与线上运营双开同一账号 |

可用 `AM4_PORT` 修改本地端口。开发或只读检查时可设置 `AM4_DISABLE_SCHEDULER=1`，阻止后台待办执行。

## 面板账号与登录

面板已内置登录体系（替代 nginx Basic Auth）：

- **首次启动**：在 `.env` 中设置 `AM4_SETUP_TOKEN` 后访问 `/setup` 创建管理员（需输入该令牌；纯管理身份，不绑定任何游戏账号）。也可在 `.env` 中配置 `AM4_ADMIN_USERNAME` / `AM4_ADMIN_PASSWORD`，启动时自动创建管理员，免去访问 `/setup`。`AM4_ADMIN_USERNAME` 遵循与注册一致的 2~8 位用户名规则（字母数字、空格、`_`、`/`），不合规时启动会记录警告并跳过创建。未配置 `AM4_SETUP_TOKEN` 时网页初始化会被禁用，防止服务在初始化前暴露到公网时被抢先接管。即使 `.env` 未配置 `AM4_EMAIL`/`AM4_PASSWORD`，面板与 `/setup` 也能正常启动，只有启动某个账号的采集循环时才要求该账号有凭据。
- **注册**：`/register` 注册后状态为 `pending`，需管理员在管理面板审核通过才能登录。用户名仅允许 2~8 位英文字母或数字，可包含空格、下划线（`_`）或斜杠（`/`），不含其他符号，头尾不允许空格。
- **账号设置**：登录后主页「设置」可修改自动化参数（成本指数、采购阈值、现金垫、A-Check/磨损保护），并独立开关自动营销、自动买油、自动买 CO₂、自动起飞。AM4 游戏账号在注册时绑定、不可修改；管理员可在管理面板重置任意用户的面板登录密码。
- **多账号**：每个网站账户绑定唯一的游戏账号，运行数据按账号哈希目录隔离；管理员可审核用户、停用/删除账号，并可「以该账号身份」进入与账号主人一致的主面板。停用或删除用户会立即停止其正在运行的循环，并把该游戏账号视为受保护（等同于 `AM4_PROTECTED_ACCOUNTS`），重新启用后自动恢复。
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

### 一键安装（推荐）

在 Ubuntu / Debian 服务器上以 root 运行：

```bash
sudo bash deploy/install.sh
```

> [!WARNING]
> 安装脚本尚未经过完整的实机测试，请在测试服务器上谨慎使用，部署前务必备份数据；如有问题欢迎反馈。

脚本会交互询问安装目录、服务用户、域名、HTTPS 方式（Let's Encrypt / 自签名 / 仅 HTTP）、游戏账号（可选）与管理员账号（可选），随后自动生成 `.env`、初始化令牌、会话密钥与服务令牌，创建虚拟环境并安装依赖，写入 Nginx 反向代理配置并签发/生成证书，注册 `am4.service` 并开机自启。安装完成后按提示通过 `/setup`（初始化令牌在服务器 `.env` 中）或已自动创建的管理员登录。

仓库同时保留了供手动部署的参考配置：

- [deploy/am4.service](deploy/am4.service)：单进程 Gunicorn + 自动启动循环
- [deploy/nginx-am4.conf](deploy/nginx-am4.conf)：Nginx 反向代理与 HTTPS
- [deploy/start_loop.py](deploy/start_loop.py)：systemd 启动后的循环接续助手

参考配置假定项目位于 `/opt/am4/app`、虚拟环境位于 `/opt/am4/app/.venv`。部署时应修改域名、创建 `.env`、配置 HTTPS（certbot），并设置 `AM4_COOKIE_SECURE=1`。systemd 单元已内置 `AM4_TRUST_PROXY=1`（与 nginx 的 `X-Forwarded-For` 配套，限流按真实 IP 计数）和 `AM4_MAX_SSE_CLIENTS=6`（为普通请求预留线程）。登录认证由应用自身提供（`/setup` 创建管理员），无需再配置 nginx Basic Auth；`start_loop.py` 通过 `src/.service_token` 中的服务令牌恢复循环，不依赖浏览器会话。

面板用户、绑定账号与设置保存在 `data/panel.db`（SQLite，已在 .gitignore 中）。请确保该目录仅服务用户可读写（如 `chmod 700 data`）。

## 测试

```bash
python -m unittest discover -s tests
```

测试环境使用无效账号并禁用后台调度，不会访问游戏或执行真实操作。

## 许可证与项目说明

- 本项目使用 Codex 协助开发。
- 除 [THIRD_PARTY_NOTICES.md](doc/THIRD_PARTY_NOTICES.md) 中另行声明的第三方内容外，本仓库采用 [CC BY-NC-SA 4.0](LICENSE)：二次开发须保留署名、不得用于商业目的，并以相同许可公开分享衍生作品。
- 二次发布时应以合理方式保留原作者署名、原项目仓库链接和本许可证链接；如有修改，应同时说明修改内容，不限定具体署名格式。
- 该许可证包含非商业限制，因此本项目属于 source-available，不是 OSI 定义下的开源软件。商业授权需另行取得作者许可。
- 航线收益模型和 `data/` 中的离线基础数据参考 [AM4Help](https://am4.pages.dev/)，第三方版权与 MIT 许可见 [THIRD_PARTY_NOTICES.md](doc/THIRD_PARTY_NOTICES.md)。
- 版本变化见 [CHANGELOG.md](doc/CHANGELOG.md)。
