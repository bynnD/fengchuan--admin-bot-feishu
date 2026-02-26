# 飞书行政助理机器人

基于飞书开放平台与 DeepSeek AI 的行政审批助手，支持用印、采购、开票、外出报备等工单的智能创建与自动审批。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **智能识别** | 用户用自然语言描述需求，AI 自动识别审批类型并提取字段 |
| **工单创建** | 信息齐全时通过飞书 API 自动创建审批实例 |
| **附件识别** | 用印、开票支持上传 Word/PDF/图片，AI 自动识别内容 |
| **自动审批** | 指定审批人的待办可自动审批（采购/开票默认通过，用印需 AI 合规分析） |

---

## 用户使用流程

1. **开始对话**：在飞书中向机器人发送一句话，如「我要采购一台笔记本」或「帮我用印一个合同」
2. **补充信息**：若字段不完整，机器人会提示补充；可用自然语言继续说明
3. **信息核实**：提交前请核实填报信息无误
4. **自动提交**：信息齐全时，机器人直接创建审批并返回确认
5. **后续流程**：等待主管审批（或由自动审批处理）

---

## 支持的审批类型

| 类型 | 提交方式 | 说明 |
|------|----------|------|
| 用印申请单 | API 自动提交 | 上传盖章文件，AI 识别用印公司、印章类型、事由 |
| 开票申请单 | API 自动提交 | 需上传结算单+合同，AI 自动识别金额、购方、税号等 |
| 采购申请 | API 自动提交 | 固定流程，支持 API 创建 |
| 招待/团建物资领用 | API 自动提交 | 物品用途、领用日期、物品明细（名称和数量） |
| 外出报备 | 链接手动发起 | 报备单（无审批节点），飞书 API 不支持，需点击链接填写 |

> 飞书 API 仅支持有审批节点的固定/自由流程；报备单需通过链接手动发起。

---

## 自动审批

针对配置的审批人，可启用自动审批，由机器人代为处理待办。

### 开关控制（仅配置的审批人有效）

| 操作 | 固定指令 |
|------|----------|
| 开启 | 「开启自动审批」「打开自动审批」 |
| 关闭 | 「关闭自动审批」 |
| 查询 | 「自动审批状态」「自动审批开没开」 |

### 规则说明

- **采购申请**：默认自动通过
- **开票申请单**：默认自动通过
- **用印申请单**：由 AI 分析附件，判断 ① 合法合规 ② 风险点 ③ 用印类型与文件类型是否匹配，符合则自动通过
- **外出报备**：不参与自动审批

规则配置见 `approval_rules.yaml`。

---

## 项目结构

```
feishu--admin-bot-main/
├── main.py                 # 主程序：消息处理、工单创建、WebSocket 连接
├── approval_auto.py        # 自动审批：轮询、审批 API、用印 AI 分析
├── approval_rules_loader.py # 规则加载：YAML 解析、开关指令匹配
├── approval_rules.yaml     # 自动审批规则配置
├── approval_types/         # 工单类型定义
│   ├── __init__.py
│   ├── seal.py             # 用印申请单
│   ├── purchase.py         # 采购申请
│   ├── invoice.py          # 开票申请单
│   ├── reception_supplies.py # 招待/团建物资领用
│   └── outbound.py         # 外出报备
├── field_cache.py          # 审批表单字段缓存
├── file_extraction.py      # 文件内容提取（Word/PDF/OCR）
├── deepseek_client.py      # DeepSeek API 调用
├── requirements.txt
└── Dockerfile
```

---

## 部署配置

### 必需环境变量

| 变量 | 说明 |
|------|------|
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用密钥 |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥，用于 AI 识别 |

### 可选环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_APPROVAL_APP_ID` | 飞书审批应用 ID，用于打开审批详情页 | cli_9cb844403dbb9108 |
| `SECRET_TOKEN` | 访问 `/debug-form` 的校验 token | - |
| `MAX_FILE_SIZE` | 文件大小限制（字节） | 52428800（50MB） |
| `PORT` | 健康检查服务端口 | 8080 |
| `APPROVAL_RULES_FILE` | 自动审批规则文件路径 | approval_rules.yaml |
| `AUTO_APPROVAL_POLL_INTERVAL` | 自动审批轮询间隔（秒） | 300（5 分钟） |

### 飞书应用权限

- 获取与发送单聊、群组消息
- 以应用身份读取通讯录
- 审批：查看、创建、更新、删除审批应用信息
- 审批：审批任务操作、审批实例评论
- 云文档：查看、编辑、下载云空间文件（用于用印附件下载）

---

## 快速部署

### 本地运行

```bash
pip install -r requirements.txt
export FEISHU_APP_ID=xxx FEISHU_APP_SECRET=xxx DEEPSEEK_API_KEY=xxx
python main.py
```

### Docker

```bash
docker build -t feishu-admin-bot .
docker run -e FEISHU_APP_ID=xxx -e FEISHU_APP_SECRET=xxx -e DEEPSEEK_API_KEY=xxx -p 8080:8080 feishu-admin-bot
```

### Railway

将项目部署到 Railway，配置上述环境变量即可。程序使用 WebSocket 长连接，无需额外公网回调地址。

---

## 多公司部署

若需在另一家公司使用相同功能，建议**独立部署**：

1. 在另一家公司的飞书开放平台创建应用，获取新的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`
2. 在飞书管理后台创建同名审批（用印、采购、开票、外出），记录各 `approval_code`
3. 修改 `approval_types/*.py` 中的 `APPROVAL_CODE` 为新公司的值
4. 修改 `approval_rules.yaml` 中的 `auto_approve_user_ids` 为新公司的审批人 user_id
5. 单独部署一份，配置新应用的环境变量

---

## 新增审批类型

参见 `approval_types/README.md`。简要步骤：

1. 在 `approval_types/` 新建 `xxx.py`，定义 `NAME`、`APPROVAL_CODE`、`FIELD_LABELS` 等
2. 在 `__init__.py` 的 `_TYPES` 中注册
3. 若需附件识别，设置 `HAS_FILE_EXTRACTION = True` 并实现 `extract_fields_from_file`

---

## 调试接口

- `GET /`：健康检查，返回 `ok`
- `GET /debug-form?type=采购申请`：查看指定审批类型的表单字段结构（若配置 `SECRET_TOKEN`，需带 `?token=xxx`）
