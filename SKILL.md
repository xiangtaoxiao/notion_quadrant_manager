---
name: notion-time-management-matrix
description:
  待办事项管理技能，用于通过 exec 调用 Python 脚本完成指定notion数据库的连接，基于四象限法则进行时间管理，待办事项创建、查询、更新与分析总结。当用户提出"明天要做"或"最近有什么重要的事"或"25号前解决"等涉及时间管理内容时触发。
metadata:
  openclaw:
    requires:
      bins:
        - python3
    version: "0.1.1"
    author: "xt Shawn"
    license: "MIT"
---

# Notion 四象限任务管理

## 1. 触发条件

当用户输入包含以下意图时，触发本技能：

- **添加任务**：用户表达需要记录、创建或新增待办事项的意图，包含任务内容和时间相关信息
- **查询任务**：用户询问指定时间范围内的任务，如今天、最近几天、特定日期范围等
- **搜索任务**：用户询问特定任务的相关信息，提供关键词或描述
- **完成任务**：用户表示已经完成某项任务，或想要将任务标记为已完成状态
- **取消任务**：用户表示不再需要执行某项任务，或想要删除/取消待办事项
- **总结任务**：用户希望对任务进行统计分析、四象限分类、或了解当前任务的整体情况

## 2. 用户配置

### 2.1 API 密钥配置
用户需要提供 API 密钥，执行以下命令存储 API 密钥：
   ```bash
   mkdir -p ~/.config/notion
   echo "your_api_key_here" > ~/.config/notion/api_key
   ```

### 2.2 数据库配置
用户必须提供：
- `notion_database_name`：数据库名称

如果数据库名称不存在、Notion 连接失败、或缺少必需字段，则提示用户修正配置。

### 2.3 状态文件
- **存储位置**：脚本notion_quadrant_manager.py所在目录下的 `notion_quadrant_manager_state.json`
- **作用**：缓存数据库连接信息和字段映射，提高操作效率
- **注意事项**：确保脚本所在目录有写入权限

## 3. 必要字段

识别数据库中与下列语义对应的字段，不要求完全同名，但必须存在对应的 Notion 属性类型和可用枚举值。
- 待办事项（标题字段）
- 截止时间（日期字段）
- 四象限（select/status/multi_select 字段）
- 状态（status/select 字段）
- 备注（rich_text/title 字段）
- 分类（multi_select/select 字段）

## 4. 调用方式

本技能通过 exec 调用SKILL.md同目录scripts文件夹下的 Python 文件：

```bash
python3 ./scripts/notion_quadrant_manager.py <action> '<json_args>'
```

`json_args` 必须包含：
- `database_name`：数据库名称

API 密钥会自动从 `~/.config/notion/api_key` 文件读取。

## 5. 可调用动作

### 5.1 bootstrap
连接 Notion，定位数据库，读取 schema，并保存字段映射。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称

**返回**：
- 数据库连接信息
- 字段映射

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py bootstrap '{"database_name":"xxx"}'
```

### 5.2 add
创建任务。理解用户意图，归纳总结任务标题、备注、分类，识别对话中的日期、四象限、状态，生成结构化数据输出给脚本执行。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `title`：任务标题（Agent 归纳总结）
- `due_date`：截止日期（ISO 格式）
- `quadrant`：四象限分类（Agent推断）
- `status`：状态（默认：未开始）
- `category`：分类（Agent 归纳总结，可选）
- `note`：备注（Agent 归纳总结，可选）

**返回**：
- 创建的任务信息

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py add '{"database_name":"xxx","title":"去北京","due_date":"2026-03-28","quadrant":"重要紧急","status":"未开始","category":"工作","note":"商务出差"}'
```

### 5.3 query
查询指定时间范围内的任务，支持状态过滤。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `start_date`：开始日期（ISO 格式）
- `end_date`：结束日期（ISO 格式）
- `status`：任务状态列表（可选，默认：["未开始", "进行中"]）

**返回**：
- 指定时间范围内的任务列表（包含超时任务提醒）

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py query '{"database_name":"xxx","start_date":"2026-04-01","end_date":"2026-04-07","status":["未开始", "进行中"]}'
```

### 5.4 search
搜索指定任务，返回最相似的前 3 个任务。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `query`：查询关键词

**返回**：
- 最相似的前 3 个任务

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py search '{"database_name":"xxx","query":"北京出差"}'
```

### 5.5 update_status
更新任务状态。优先使用 `page_id`，否则使用最近一次任务上下文。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `page_id`：任务 ID（可选）
- `text`：任务描述（用于查找任务，可选）
- `status`：任务状态（如：未开始、进行中、完成等）

**返回**：
- 更新后的任务信息

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py update_status '{"database_name":"xxx","page_id":"任务ID","status":"进行中"}'
```

### 5.6 cancel
将任务标记为已取消。优先使用 `page_id`，否则使用最近一次任务上下文。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `page_id`：任务 ID（可选）
- `text`：任务描述（用于查找任务，可选）

**返回**：
- 更新后的任务信息

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py cancel '{"database_name":"xxx"}'
```

### 5.7 summary
按四象限统计待办任务数量，总结最近任务。

**参数**：
- `notion_api_key`：Notion API 密钥
- `database_name`：数据库名称
- `days`：天数（默认：15）

**返回**：
- 四象限统计
- 重要紧急任务列表
- 超时任务列表
- 基于四象限时间管理法则给出的建议

**示例**：
```bash
python3 ./scripts/notion_quadrant_manager.py summary '{"database_name":"xxx","days":7}'
```

## 6. 四象限处理

### 6.1 写入数据时四象限推测规则
- 是否重要：任务内容凡涉及领导、父母等重要关系人，涉及学习、健康、资金处理、晋升，涉及事先承诺的均为重要
- 是否紧急：任务完成日期距今天小于等于2天的均为紧急
- 如无法判定重要性或紧急性，直接询问用户四象限分类

### 6.2 总结分析流程
1. 检查数据库中是否有四象限字段，如有则直接使用该值判定
2. 基于四象限时间管理法则及用户不同象限的任务数量，生成四象限统计和建议

## 7. 超时任务处理

- 识别截止时间早于当前时间的任务
- 在查询和总结时单独列出这些任务
- 使用醒目的方式提醒用户这些任务已超时
- 超时任务优先显示在任务列表顶部

## 9. 输出要求

Python 脚本返回 JSON，至少包含：
- `ok`：操作是否成功
- `action`：执行的动作
- `message`：操作结果消息
- `data`：操作结果数据

Agent 读取 JSON 后再组织自然语言回复给用户。

