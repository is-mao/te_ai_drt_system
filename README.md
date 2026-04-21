# TE AI DRT System (Defect Report Tracking)

TE 工厂缺陷报告追踪系统，基于 Flask + Google Gemini AI，支持智能日志分析、报告美化、多语言翻译和远程数据库同步。

---

## 功能特性

| 模块 | 功能说明 |
|------|----------|
| **Defect Reports** | 缺陷报告 CRUD，支持排序、多条件筛选（BU/Station/Owner/Defect Class/日期等）、分页、Excel 导出/导入 |
| **Pending** | Cesium 导入的草稿记录，待补全 Defect Class/Value 等字段后正式入库 |
| **Import** | 导入 DRT Excel 报告或 Cesium 测试数据（.xlsx），自动解析字段 |
| **Dashboard** | KPI 概览（总数、各 BU 统计、本周数据）+ 图表（Defect Class 分布、周趋势、Top 10 Station/Server/PCAP/Failure） |
| **Database Sync** | 通过 SFTP 将本地 SQLite 数据库推送到远程服务器，或从远程拉取；支持选择性 Pull & Merge 其他用户数据 |
| **Settings** | Gemini API Key + CIRCUIT API 配置、系统信息查看 |
| **修改密码** | 导航栏用户下拉菜单 → 弹窗修改密码（旧密码验证 + 新密码最少 8 位） |

### AI 功能（编辑页面内置）

| AI 工具 | 说明 |
|---------|------|
| **AI Diagnosis** | 优先使用 CIRCUIT API（可回退 Gemini）分析 Sequence/Buffer Log，自动生成 Root Cause 和 Action |
| **History Query** | 查询同 BU + 同 Failure 的历史记录，复用已有的 Root Cause & Action |
| **AI Beautification** | 优先使用 CIRCUIT API（可回退 Gemini）润色 Root Cause & Action 的语法和表达，Before/After 对比预览 |
| **AI Translation** | 优先使用 CIRCUIT API（可回退 Gemini）将 Root Cause & Action 翻译为中文或越南语，原文不变，翻译结果可一键复制 |

---

## 快速开始

### 1. 安装

```bash
# 克隆
git clone https://github.com/is-mao/te_ai_drt_system.git
cd te_ai_drt_system

# 创建虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env` 文件，设置以下关键项：

```dotenv
# Gemini AI API Key（从 https://aistudio.google.com/apikey 获取）
GEMINI_API_KEY=AIzaSy...

# 生产环境改为随机字符串
DRT_SECRET_KEY=your-random-secret-key

# 可选：CIRCUIT API（若不在 Settings 页面配置）
CIRCUIT_API_ENDPOINT=https://chat-ai.cisco.com/openai/deployments/gemini-3.1-flash-lite/chat/completions?api-version=2025-04-01-preview
CIRCUIT_APP_KEY=egai-prd-supplychain-262013805-summarize-1776759998924
CIRCUIT_ACCESS_TOKEN=your-circuit-oauth-token
CIRCUIT_MODEL=gemini-3.1-flash-lite
```

### 3. 启动

```bash
python app.py
```

打开 http://127.0.0.1:5001

---

## Windows 后台部署

```powershell
powershell -ExecutionPolicy Bypass -File .\start_drt.ps1          # 启动
powershell -ExecutionPolicy Bypass -File .\start_drt.ps1 -Stop    # 停止
powershell -ExecutionPolicy Bypass -File .\start_drt.ps1 -Status  # 查看状态
```

日志保存在 `logs/` 目录。

---

## 数据库

使用 SQLite，零配置，数据库文件自动创建在项目根目录 `drt_system.db`。

---

## 数据库同步（Sync）

通过 SFTP 在本地和远程服务器之间同步 SQLite 数据库：

| 操作 | 说明 |
|------|------|
| **Push** | 将本地 `drt_system.db` 上传到远程服务器（自动排除其他用户的合并数据） |
| **Pull** | 从远程服务器下载数据库到本地（自动备份旧数据） |
| **Pull & Merge** | 选择性拉取其他用户的数据，合并到本地，在 Defects/Pending 页面展示 |
| **Clear Local Data** | 清空本地数据库所有记录（远程不受影响），用于切换账户前清理 |
| **Test Connection** | 测试 SFTP 连接是否正常 |

### 权限控制

| 角色 | 权限 |
|------|------|
| **Admin** | 可编辑/删除所有记录（包括其他用户的合并数据） |
| **普通用户** | 只能编辑/删除自己的记录，其他用户合并进来的数据为**只读** |

### 多用户数据流程

1. 每个用户在本地工作，数据存储在 `drt_system.db`
2. **Push** 将自己的数据上传到远程 `/root/drt_db_data/<username>.db`
3. 其他用户可通过 **Pull & Merge** 选择性拉取，数据以 `owner` 标记显示在 Defects/Pending 页面
4. 切换电脑或账户时：**Clear Local Data → Pull** 即可恢复自己的数据

> **注意**：Push 时自动过滤合并数据，确保远程只保存自己的记录。

---

## 页面说明

### Dashboard
KPI 卡片 + 多维图表，支持按 BU 和日期范围筛选。

### Defects
缺陷报告列表页面，支持：
- 多条件筛选：BU、Defect Class、Defect Value、Station、Owner（下拉框）、日期范围、关键词搜索
- 自适应筛选栏：flex 布局自动适配不同屏幕宽度，小屏自动换行
- Owner 列：显示记录来源（local = 本地数据，用户名 = 合并数据）
- 排序：点击表头切换升序/降序
- 导出 Excel（可选是否包含 Log）
- 权限控制：非 admin 用户对他人记录无 Edit/Delete 按钮
- 点击记录查看详情或编辑

### Edit / New
创建或编辑缺陷报告：
- 上半部分：基础字段（BU、Station、SN、Failure 等）
- Log 区域：Sequence Log + Buffer Log 文本框，支持拖拽上传 .txt/.log/.gz/.gzm 文件
- AI Tools 区域（4 个卡片）：
  - **AI Diagnosis** — 输入关键词后点击分析，AI 自动生成 Root Cause 和 Action
  - **History Query** — 查同 BU + 同 Failure 的历史记录
  - **AI Beautification** — 润色已有的 Root Cause & Action
  - **AI Translation** — 翻译为中文/越南语
- Root Cause & Action 文本框：可手动填写或由 AI 填充

### Import
- **DRT Excel Import** — 导入标准 DRT Excel 文件
- **Cesium Import** — 导入 Cesium 测试数据，自动创建为 Pending 草稿

### Sync
- **Remote Databases** — 显示远程用户列表和 Push/Pull 操作
- **Pull & Merge** — 勾选其他用户，拉取数据合并到本地（带 owner 标记）
- **Currently Merged Data** — 查看已合并的用户数据和记录数，可单独移除
- **Clear Local Data** — 一键清空本地数据库，用于切换账户前清理
- 进度条动画显示操作进度
- Pull / Merge / Clear 操作后自动刷新导航栏 Pending 数量角标

### 导航栏
- 用户名下拉菜单：修改密码（弹窗）、退出登录
- Pending 数量角标：实时显示待处理草稿数量，Sync 操作后自动刷新

### Settings
- 配置 Gemini API Key
- 配置 CIRCUIT API（Endpoint、AppKey、Access Token、Model）
- CIRCUIT 认证使用 `api-key` 请求头 + `user={"appkey":...}` 负载字段
- Access Token 约 1 小时过期，过期后在 Settings 更新
- 查看系统版本信息

---

## 项目结构

```
te_ai_drt_system/
├── app.py                  # Flask 应用入口
├── config.py               # 配置（DB、Defect Classes/Values、BU 列表）
├── .env / .env.example     # 环境变量
├── requirements.txt        # Python 依赖
├── start_drt.ps1           # Windows 后台部署脚本
├── drt_system.db           # SQLite 数据库文件（自动创建）
├── models/                 # SQLAlchemy 数据模型
│   ├── defect_report.py    # DefectReport 表
│   ├── user.py             # User 表
│   └── system_config.py    # SystemConfig 表（存储 API Key 等）
├── routes/                 # Flask 蓝图（路由）
│   ├── auth.py             # 登录/登出/修改密码/权限
│   ├── defect_reports.py   # 缺陷报告 CRUD + API
│   ├── dashboard.py        # Dashboard 页面 + 统计 API
│   ├── import_export.py    # Excel 导入/导出
│   ├── ai_analysis.py      # AI 分析/美化/翻译 API
│   ├── sync.py             # 数据库同步 Push/Pull API
│   └── settings.py         # 设置页面
├── services/               # 业务逻辑
│   ├── ai_service.py       # Gemini AI 集成（分析、美化、翻译）
│   ├── db_sync.py          # SFTP 数据库同步
│   ├── failure_dict.py     # Failure 字典查询
│   └── historical_search.py # 历史记录搜索
├── templates/              # Jinja2 HTML 模板
├── static/                 # 静态资源（CSS、JS、图片）
│   └── css/style.css       # Cisco 品牌色设计系统
└── docs/                   # 文档
    ├── DRT_System_User_Guide.md
    └── GEMINI_API_KEY_Guide.md
```

---

## 技术栈

| 组件 | 版本/说明 |
|------|-----------|
| Python | 3.12+ |
| Flask | 3.1.0 |
| Flask-SQLAlchemy | 3.1.1 |
| Google Gemini AI | google-genai 1.0+ |
| Cisco CIRCUIT API | OpenAI-compatible chat completions |
| Paramiko | 3.5.0（SFTP 同步） |
| Bootstrap | 5.3（CDN） |
| Bootstrap Icons | 1.11（CDN） |
| Chart.js | 4.x（Dashboard 图表） |

## License

Private use only.
