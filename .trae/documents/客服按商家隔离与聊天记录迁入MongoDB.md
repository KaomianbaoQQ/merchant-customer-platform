# 客服按商家隔离 + 聊天记录迁入 MongoDB

## 一、Context（为什么做）

需求两点：**商品页的售前客服必须每个商家完全独立**、**聊天记录改存 NoSQL**。

现状与真实缺口（已逐一核对过代码）：

| 项 | 现状 |
| --- | --- |
| 会话 key | `merchant_id::user_id`（[store.py](file:///Users/kaomianbao/Downloads/模块/公共/store.py#L288-L290)），**已按商家隔离** |
| 售前聊天记录 | 存 JSON 文件 `商户界面/chat.json`（4 个线程 / 46 条） |
| 售后聊天记录 | 存 MySQL `cs.chat_log`（220 行 / 19 个 session），**不读 chat.json** |
| 平台「全部对话」 | 读 `chat.json`（`merchant_id=platform` 的售后转人工会话） |
| ❌ 知识库没隔离 | [客服/workflow.py](file:///Users/kaomianbao/Downloads/模块/客服/workflow.py#L126-L131) 调 Dify 时**不传商家**，知识库是单一 dataset、`knowledge_map.json` 无商家分组 → A 商家商品页可能召回 B 商家资料 |
| ❌ 后端越权 | [merchant.py](file:///Users/kaomianbao/Downloads/模块/后端/routes/merchant.py#L391-L422) 会话详情/回复直接用请求传来的 `merchant_id`，未校验归属 → 商家能读/回别人的会话 |
| ❌ 前端串号隐患 | [客户前端.html](file:///Users/kaomianbao/Downloads/模块/前端/客户前端.html#L2078-L2087) 所有商家共用同一个 `csHistory.consult`，仅靠"检测到商家变化就清空"规避 |

已确认的决策：**MongoDB（Docker 容器）**、**售前+售后全部聊天记录都迁**、**用户可改 Dify 工作流图做真隔离**、**Mongo 连不上时降级：只警告不报错**（与现有"保存失败不影响主流程"一致）。

## 二、方案总览

1. 新建 `公共/chat_store.py`：MongoDB 封装，**对外函数签名与返回值与现状完全一致**，调用点零改动。
2. 配置放 `公共/db_config.py` 新增 `MONGO_CONFIG`（**不放 config.json**：`工具/set_time_config.py:13-15` 会用只含 `default_delay` 的 dict 整体覆盖 config.json，会冲掉 mongo 段）。
3. 集合：`presale_threads`（售前会话，一条会话一个文档，含 `messages[]` + 未读/转人工状态）、`after_sales_messages`（售后，一条消息一个文档）。
4. 商家隔离三处修复：后端归属校验、前端按商家分桶、Dify 知识库按商家过滤。
5. 一次性迁移脚本 + 启动脚本加 Mongo 段。

### 集合与索引

```
presale_threads   {key, merchant_id, user_id, merchant_name, user_name, messages[{id,role,text,time}],
                   unread_merchant, unread_user, updated_at, needs_human, human_since, human_handled,
                   unclear_count, asking_human}
  索引: {merchant_id:1,user_id:1} unique | {merchant_id:1,updated_at:-1} | {updated_at:-1}
after_sales_messages  {session_id, user_id, user_name, role, content, create_time(datetime),
                       created_at(str), legacy_id(MySQL 原自增 id，迁移幂等用)}
  索引: {session_id:1,_id:-1} | {user_id:1} | {create_time:-1} | {legacy_id:1} sparse unique
```
Mongo 容器：`docker run -d --name kaomianbao-mongo -p 127.0.0.1:27017:27017 -v kaomianbao_mongo_data:/data/db --restart unless-stopped mongo:7`

## 三、实施步骤

| 步 | 文件 | 改什么 |
| --- | --- | --- |
| 1 | — | 起 Mongo 容器（首次会 pull `mongo:7`），确认 27017 可用 |
| 2 | [requirements.txt](file:///Users/kaomianbao/Downloads/模块/requirements.txt) | 加 `pymongo>=4.7`，`.venv/bin/pip install pymongo` |
| 3 | `公共/db_config.py` | 加 `MONGO_CONFIG = {"uri": "mongodb://127.0.0.1:27017", "database": "kaomianbao_chat"}` |
| 4 | `公共/chat_store.py`（新建） | MongoClient 单例（`serverSelectionTimeoutMS=800`，`connect=False`）、`ensure_indexes()`；售前 `append_message / mark_needs_human / set_thread_meta / get_thread / list_conversations / mark_read`，售后 `new_session_id / save_message / save_turn / count_recent_consecutive / get_recent_messages`。全部写操作 try/except → 打印中文告警并返回 False/None |
| 5 | [store.py](file:///Users/kaomianbao/Downloads/模块/公共/store.py#L276-L443) | 删掉 276-443 行 JSON 版 chat 实现，改为从 `公共/chat_store` 转发 import；商品/图片与 `_locked()` 保留；`CHAT_FILE` 常量保留给迁移脚本用 |
| 6 | [售后/chat.py](file:///Users/kaomianbao/Downloads/模块/售后/chat.py) | 函数签名不变、实现改调 `公共/chat_store`；`ensure_chat_table()` 变 `ensure_indexes()` 别名并返回 `(True, "聊天记录集合已就绪")` → [app.py:42](file:///Users/kaomianbao/Downloads/模块/后端/app.py#L42)、[health.py:15](file:///Users/kaomianbao/Downloads/模块/后端/routes/health.py#L15)、[after_sales.py:22](file:///Users/kaomianbao/Downloads/模块/后端/routes/after_sales.py#L22)、[cli.py:198](file:///Users/kaomianbao/Downloads/模块/售后/cli.py#L198) 都不用改 |
| 7 | [merchant.py](file:///Users/kaomianbao/Downloads/模块/后端/routes/merchant.py#L391-L422) | 会话详情/回复两处：`merchant_id` **强制取 `m["id"]`**（忽略请求传参），堵住越权 |
| 8 | [客户前端.html](file:///Users/kaomianbao/Downloads/模块/前端/客户前端.html#L2078-L2087) | 加 `const consultCache = {}`；切商家时 `consultCache[old] = csHistory.consult; csHistory.consult = consultCache[new] || []` → 每个商家各存一份历史 |
| 9 | [客服/workflow.py](file:///Users/kaomianbao/Downloads/模块/客服/workflow.py#L113-L131) | `ask_product_service(..., merchant_id=None, merchant_name=None)`（可选参数，向后兼容）；payload 加 `merchant_id / merchant_name`；prompt 加【当前商家】段；[auto_reply_service.py:213](file:///Users/kaomianbao/Downloads/模块/客服/auto_reply_service.py#L213) 补传 |
| 10 | [knowledge.py](file:///Users/kaomianbao/Downloads/模块/公共/knowledge.py#L253-L299) | 新增 `_doc_set_metadata(...)`：`create-by-text` 拿到 document_id 后、以及 `update-by-text` 后，给文档写 `merchant_id / merchant_name` 元数据（取 `product["owner_id"]/["owner_name"]`，无需改调用点）。**接口路径与请求体需在 Dify 1.17 实测确认**，失败只告警不阻断 |
| 11 | `工具/迁移聊天记录到Mongo.py`（新建） | 一次性导入：chat.json 按 `(merchant_id,user_id)` upsert、`cs.chat_log` 按 `legacy_id` upsert（**幂等，可重复跑**）；结束打印 JSON/MySQL 源条数 vs Mongo 计数对照；支持 `--dry-run`；不做任何删除 |
| 12 | [一键启动全部.command](file:///Users/kaomianbao/Downloads/模块/一键启动全部.command) | 在 Dify 段后插入 Mongo 段：容器不存在则 `docker run`，已停止则 `docker start`，轮询 27017；失败只警告不退出 |
| 13 | [README.md](file:///Users/kaomianbao/Downloads/模块/README.md) | 补「聊天记录存 MongoDB」与 Dify 元数据隔离说明、目录结构 |

## 四、需要你手动配合（Dify 侧）

1. 知识库（dataset `dab2daf8-…`）里新建自定义元数据字段：`merchant_id`、`merchant_name`。
2. 「商品咨询」工作流**开始节点**加 inputs：`merchant_id`、`merchant_name`（文本，可空）——后端就按这两个名字传。
3. **检索节点**开"元数据过滤"：`merchant_id 等于 {{#开始.merchant_id#}}`，并设为严格匹配。
   > 1.17 里元数据字段的创建入口、过滤条件是否支持引用变量，需要你在界面确认；若这条路走不通，退路是"每商家一个 dataset"（`config.json` 加 `dataset_map`，我改 `knowledge.py` 按 `owner_id` 选 dataset）。
4. 历史商品文档需要**重新打标**：在商户后台对每个商品点一次"同步知识库"。

## 五、验证

1. **商家隔离**：A 商家商品页问"你家的键盘多少钱"，回复只能来自 A 的资料；B 商家商品页同样问，不得出现 A 的商品名。
2. **越权**：带 B 的 `merchant_id` 调 `GET /api/merchant/chats/thread?user_id=<B的顾客>`，返回的必须是 A 自己的线程（空），不得是 B 的对话。
3. **落库**：发一条售前消息后 `docker exec kaomianbao-mongo mongosh kaomianbao_chat --eval "db.presale_threads.countDocuments()"`；售后发一条后 `after_sales_messages` 计数 +1，同时 `cs.chat_log` 行数**不再增长**。
4. **平台「全部对话」**：售后连说 3 次"人工"后，平台界面仍能看到该会话并回复。
5. **转人工逻辑**：连说 3 次"人工"转人工；中间插一句别的则计数归零（`count_recent_consecutive` 语义不变）。
6. **降级**：`docker stop kaomianbao-mongo` 后后端仍能启动、下单/客服不报错，只打印告警。
7. **迁移**：跑迁移脚本后再跑一次，计数不变（幂等）。

## 六、风险与回滚

- 改动需要**重启一次后端**才生效（会提前告诉你，避让你正在测的时间点）；前端改动刷新即可。
- `cs.chat_log` 表结构与数据**全部保留不删**，只是停写；迁移脚本不动源数据。
- 回滚：`git revert` `公共/store.py`、`售后/chat.py` 两处改动即可回到 JSON+MySQL 实现，Mongo 容器 `docker stop` 停掉；代价是 Mongo 期间的新消息不在旧存储里。