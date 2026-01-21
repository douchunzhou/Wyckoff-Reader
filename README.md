# 📈 Wyckoff-M1-Sentinel (威科夫 M1 哨兵)

![Python](https://img.shields.io/badge/Python-3.11-blue.svg)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-green.svg)
![Strategy](https://img.shields.io/badge/Strategy-Wyckoff-orange.svg)
![AI Engine](https://img.shields.io/badge/AI-Gemini%20%7C%20GPT--4o-purple.svg)

> **拒绝情绪化交易，用代码还原市场真相。**
> 
> 一个基于 **GitHub Actions** 的全自动量化分析系统。它利用 **A股 1分钟微观数据**，结合 **AI 大模型 (Gemini/GPT-4o)** 进行 **威科夫 (Wyckoff)** 结构分析，并通过 **Telegram** 实现交互式监控与研报推送。

## 📅 本周更新日志 (Weekly Update Changelog)

> **版本摘要**：本周重点重构了数据获取引擎，解决了历史数据不足的问题；同时构建了“三级 AI 熔断兜底”机制，显著提升了 GitHub Actions 运行的稳定性与抗干扰能力。

## 🚀 核心功能升级 (Core Features)

### 1. 双源数据引擎 (Hybrid Data Engine)
为了解决 AkShare 历史数据长度不足的问题，我们引入了 **BaoStock** 作为历史数据源。

- **混合模式**：自动合并 `BaoStock` (历史长周期) + `AkShare` (实时/近期补全) 的数据。
- **1分钟级特判**：针对 1 分钟级别数据，自动切换为 AkShare 全量抓取模式（因 BaoStock 不支持 1 分钟）。
- **智能清洗与对齐**：
    - **时间戳修复**：自动解析 BaoStock 的非标准时间格式。
    - **单位自动对齐**：智能检测并修复“手”与“股”之间的 100 倍数量级差异，防止量能指标（Volume）失真。
    - **索引冲突修复**：修复了合并数据时出现的 `Reindexing only valid with uniquely valued Index objects` 错误。

### 2. 三级 AI 兜底策略 (Triple-Tier AI Fallback)
为了应对 Google Gemini 官方接口频繁的 `429` (限流) 和 `503` (过载) 错误，构建了多级容错链：

1.  **第一优先级**：Google 官方 Gemini API (`gemini-3-flash-preview`)。
2.  **第二优先级**：自定义中转 API (`api2.qiandao.mom` / `gemini-3-pro-preview-h`)。
3.  **最终防线**：OpenAI / DeepSeek (`gpt-4o` 兼容接口)。

> **策略优化**：一旦某一级 API 报错，程序采用“快速失败”策略（仅重试 1 次），立即切换到下一级，确保分析报告 100% 生成。

### 3. 连接稳定性增强 (Connectivity)
针对 GitHub Actions 环境下的 `RemoteDisconnected` 和超时问题进行了底层优化：

- **防断连**：在 HTTP Header 中添加了伪装 `User-Agent`，并强制设置 `Connection: close`，防止复用已失效的 TCP 连接。
- **致命错误熔断**：遇到 `400` (Key 无效/参数错) 等不可恢复错误时，直接抛出异常跳过重试，避免无效等待。
- **超时调整**：单次请求超时时间延长至 **120秒**，以适应 Gemini 3.0 模型较长的思考时间。

### 4. 高级自定义配置 (Customization)
支持在 Google Sheets 中针对每一只股票进行个性化设置，实现“千股千策”：

- **自定义周期**：支持 `1`, `5`, `15`, `30`, `60` 分钟级别。
- **自定义长度**：可指定抓取 500 根、1000 根甚至更多 K 线用于长周期分析。

---

## 📖 配置指南 (Configuration Guide)

### 1. Google Sheets 表格设置
请在您的表格中新增 **第 5 列 (E)** 和 **第 6 列 (F)**：

| A (代码) | B (日期) | C (成本) | D (股数) | **E (周期)** | **F (K线数量)** |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `600519` | 2023-01-01 | 1500 | 100 | **60** | **1000** |
| `000001` | 2023-02-01 | 12.5 | 5000 | **5** | **500** |
| `300059` | 2023-03-05 | 15.2 | 2000 | **1** | **2000** |

- **E 列 (Timeframe)**: 填 `1`, `5`, `15`, `30`, `60` (留空默认 5)。
- **F 列 (Bars)**: 填希望 AI 分析的 K 线根数，如 `600`, `1000` (留空默认 500)。

### 2. GitHub Secrets 配置
请确保仓库的 `Settings` -> `Secrets` 中包含以下 Key：

- `GEMINI_API_KEY`: Google 官方 API Key。
- `CUSTOM_API_KEY`: **[新增]** 第三方中转 API Key (Qiandao)。
- `OPENAI_API_KEY`: OpenAI 或 DeepSeek Key。
- `GCP_SA_KEY`: Google Sheet 服务账号 JSON。

### 3. Workflow 策略调整
- **强制冷却**：每只股票分析间隔 **30秒**。
- **模型版本**：默认使用 `gemini-3-flash-preview`。

---




---

## ✨ 核心功能 (Key Features)

* **🕵️‍♂️ 1分钟微观哨兵**：自动抓取 A 股 **1分钟 K 线**数据，捕捉肉眼难以察觉的主力吸筹/派发痕迹。
* **🧠 双引擎 AI 分析**：
    * **主引擎**：Google Gemini Pro (高速、免费)
    * **副引擎**：OpenAI GPT-4o (精准、兜底)
    * 深度分析供求关系，自动识别 Spring (弹簧效应)、UT (上冲回落)、LPS (最后支撑点) 等威科夫关键行为。
* **🤖 交互式 Telegram 机器人**：
    * **指令管理**：直接在电报群发送代码即可添加/删除监控，无需接触代码。
    * **研报推送**：自动生成包含红绿高对比 K 线图的 **PDF 研报**，推送到手机。
* **☁️ Serverless 架构**：完全运行在 GitHub Actions 上，**无需服务器，零成本维护**。
* **⏰ 智能调度**：
    * **午盘 (12:00)** & **收盘 (15:15)**：自动运行分析并推送报告。
    * **每 30 分钟**：自动同步 Telegram 指令，更新监控列表。


<img width="731" height="825" alt="image" src="https://github.com/user-attachments/assets/5af1f8fc-cc67-4c02-b34d-e1749180ce2c" />

---
## 🏗️ 系统架构

```mermaid
graph TD
    User(("👨‍💻 用户")) <-->|"指令交互 / 接收 PDF"| TG["Telegram Bot"]
    TG <-->|"每30分钟同步"| GH["GitHub Actions (Monitor)"]
    GH <-->|"读写"| LIST["stock_list.txt"]
    
    LIST -->|"读取列表"| JOB["GitHub Actions (Daily Report)"]
    JOB -->|"1. 获取数据"| API["AkShare 财经接口"]
    JOB -->|"2. 绘制图表"| PLOT["Mplfinance"]
    JOB -->|"3. AI推理"| AI["Gemini / GPT-4o"]
    JOB -->|"4. 生成PDF"| PDF["Report.pdf"]
    PDF -->|"推送"| TG
