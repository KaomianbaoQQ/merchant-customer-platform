# Dify 接入指南：把三个 yml 跑起来

这份文档只讲一件事：**拿到仓库里的三个工作流文件，怎么在自己的 Dify 里导入、绑知识库、发布、拿到 Key 填进本项目**。

项目的前端、后端、数据表都只是外壳，真正决定「客服会不会答、答得准不准」的是这三套工作流。所以哪怕你只想看看效果，也建议先按本文把工作流走通。

> 实测环境：Dify **1.17**（Docker Compose 自建）、模型 `deepseek-v4-flash`、向量模型 `qwen3-embedding`（Ollama）。
> 版本差异主要在菜单名字上，流程是一致的。

---

## 0. 全流程速览

```
装 Dify ──▶ 装 DeepSeek 插件 ──▶ 导入 3 个 yml ──▶ 建 1 个知识库
   │                                                    │
   └────────── 拿 4 个 Key ◀── 发布应用 ◀── 重新绑定知识库 ┘
                    │
                    ▼
        填进 config.json（或跑 工具/初始化配置.py）──▶ 启动后端 ──▶ 打开页面测试
```

你最终需要拿到 **4 个 Key**：

| # | 东西 | 长得像 | 填到哪 |
| --- | --- | --- | --- |
| 1 | 商品咨询应用 | `app-xxxxxxxx` | `dify.workflows.consult.api_key` |
| 2 | 售后客服应用 | `app-xxxxxxxx` | `dify.workflows.after_sales.api_key` |
| 3 | 订单定位应用 | `app-xxxxxxxx` | `dify.workflows.order_locate.api_key` |
| 4 | 知识库的 API Key | `dataset-xxxxxxxx` | `dify_knowledge.api_key` |

外加一个知识库 ID（`dataset_id`，UUID 形式），见第 5 节。

---

## 1. 先把 Dify 跑起来

如果用仓库自带的 `一键启动全部.command`，它会帮你 `docker compose up -d` 拉起 Dify（默认在 `~/dify/docker`，装在别处就先 `export DIFY_DIR=/你的/dify/docker`）。

手动的话就是：

```bash
cd ~/dify/docker
docker compose up -d
```

等容器全部 `healthy` 后打开 `http://localhost`，第一次会让你设置管理员账号。**记住这个账号，后面创建应用、发 Key 都要用它。**

检查是否就绪（返回 `{"status":"ok"}` 之类就算通了）：

```bash
curl -s http://localhost/console/api/setup | head -c 200
```

> `localhost` 没加端口，是因为 Dify 的 docker-compose 默认把 nginx 映射到 80。如果你改成了 `8080` 之类的端口，下文所有 `http://localhost` 都要跟着加端口，`config.json` 里的 `dify.base_url` 也要改。

---

## 2. 装模型插件（导入前做，省得后面一个个补）

三个工作流里所有 LLM 节点都用 **DeepSeek** 插件、模型名 `deepseek-v4-flash`；两个知识检索节点用的是 **Ollama 的 `qwen3-embedding`**。

1. 右上角头像 → **设置 → 模型供应商**
2. 找到 **DeepSeek** → 安装 → 填入你的 DeepSeek API Key（`sk-` 开头，在 platform.deepseek.com 申请）
3. 装完在供应商里能看到 `deepseek-v4-flash` 这个模型即算成功
   - 如果你的账号里没有这个模型名（比如只有 `deepseek-chat`），**先不用管**，导入后按第 6 节「模型不可用」的办法统一换掉
4. **Embedding**：如果你本机没有 Ollama，不用现在装。知识库建好时选一个有 embedding 能力的供应商即可（OpenAI、通义、Ollama 都行），导入时关于 embedding 的黄色警告可以直接忽略

> 为什么强调 embedding：知识检索节点保存的是「用哪个向量模型检索」。导入到你的环境后，模型 ID 对不上，节点会提示模型不可用；重新绑定知识库（第 5 节）后会自动跟着知识库的索引设置走。

---

## 3. 导入三个工作流（核心步骤）

仓库里 `客服/`、`售后/` 下有好几个 yml，**只导入这三个**，文件名就是导入后的应用名：

| 导入这个文件 | 应用名 | 作用 |
| --- | --- | --- |
| `售后/订单定位.yml` | 订单定位 | 顾客只说「我那个订单到哪了」，从订单列表里定位到具体哪一笔 |
| `售后/售后客服.yml` | 售后客服 | 售后主流程：数据库增删改查 + 退货政策 + 售后进度 + 转人工 |
| `客服/商品咨询.yml` | 商品咨询 | 售前：单品咨询 / 多品对比 / 闲聊 / 物流查询 四分支 |

**不要导入**这几个（它们是草稿或旧版，会占你一堆应用位置）：

- `客服/商品咨询.draft.yml`、`售后/售后客服.draft.yml`、`售后/订单定位.draft.yml` —— 与正式文件的草稿副本
- `售后/客服.yml` —— 早期版本的售后流程，已被 `售后客服.yml` 取代

### 3.1 操作路径

Dify 1.17：

1. 顶部切到 **工作室（Studio）**
2. 右上角 **创建空白应用** 旁边的下拉箭头 → **导入 DSL 文件**
   （有的版本是应用列表右上角直接一个「导入 DSL」按钮）
3. 选择 `售后/订单定位.yml` → 导入 → 等待解析
4. 重复 3 次，把三个文件都导进去

导入完成后你应该在工作室看到三个应用，类型都是 **工作流（Workflow）**。

### 3.2 导入时会看到的警告（都不致命）

| 警告 | 含义 | 怎么处理 |
| --- | --- | --- |
| 模型不可用 / 模型不存在 | 你的环境没有 `deepseek-v4-flash` | 见第 6 节，或先在模型供应商里补上 DeepSeek |
| 知识库不存在 / 数据集不存在 | yml 里记的是我本机的数据集 UUID（`dab2daf8-...`），你的环境当然没有 | **正常现象**，必须按第 5 节重新绑定 |
| 插件缺失（marketplace 依赖） | yml 声明依赖 `langgenius/deepseek:0.0.21` | 装好 DeepSeek 插件即可；节点仍报错就打开节点手动重选模型 |

> 导入只是把图搬过来，**变量名、节点连线、提示词都是完整的**，不用重画。

---

## 4. 三个工作流的入参出参（改之前先看这里）

后端按**变量名**传参，按**输出字段名**取结果。名字对不上不会报错，只会「工作流跑成功但页面没反应」——这是最容易踩的坑。

### 4.1 入参（开始节点必须保留这些变量名）

| 工作流 | 变量 | 必填 | 说明 |
| --- | --- | --- | --- |
| 商品咨询 | `user_input` | 是 | 后端已经把它拼成一大段：顾客原话 + 当前商品信息 + 该顾客在这件商品上的订单物流 |
| 订单定位 | `user_input` | 是 | 顾客原话 |
| 订单定位 | `user_orders` | 否 | 该顾客的订单列表（纯文本），供 LLM 定位 |
| 售后客服 | `user_input` | 是 | 顾客原话 |
| 售后客服 | `user_id` | 是 | 用户 ID |
| 售后客服 | `user_role` | 是 | `customer` / `merchant` / `platform` |
| 售后客服 | `user_name` | 是 | 用户显示名 |
| 售后客服 | `user_orders` | 否 | 该顾客的订单列表文本 |

改名要同步改代码：`客服/workflow.py:141`、`售后/workflow.py:82`、`售后/locator.py:51`。

> 商品咨询的后端还会多传 `merchant_id` / `merchant_name`（按商家隔离检索用）。当前仓库里的 `商品咨询.yml` 开始节点**没有**声明这两个变量，Dify 会忽略它们；如果你想让客服按商家过滤知识库，就在开始节点加上这两个变量，再在知识检索节点里用元数据过滤。

### 4.2 出参（结束节点必须保留这些变量名）

**商品咨询**：四个业务分支各一个输出，后端从上到下取第一个非空的（`客服/workflow.py` 的 `BRANCH_KEYS`）：

| 字段 | 含义 |
| --- | --- |
| `one` | 单商品咨询 |
| `two` | 多品对比 |
| `three` | 闲聊 / 无关 |
| `four` | 物流查询 |
| `error_message` | 分类器或节点异常时的兜底话术 |

**售后客服**：

| 字段 | 含义 |
| --- | --- |
| `json` | 业务操作结果（新增/删除/修改/查询），JSON |
| `ops_json` | 一句话含多个操作时的聚合结果 |
| `comfort` | 用户情绪激动时的安抚话术 |
| `error_message` / `error_type` | 错误与错误类型 |
| `talk_output` | 闲聊回复 |
| `policy_output` | 退货政策解答 |
| `return_pick` | 提出退换货申请时的定位结果 |
| `aftersale_output` | 售后进度解答 |

**订单定位**：`order_pick`（定位到的订单，或追问哪一单）。

### 4.3 每套在做什么

**商品咨询**（13 节点）：开始 → 问题分类器（4 类：单商品 / 多商品对比 / 无关闲聊 / 物流）→ 各自走知识检索 + LLM → 5 个结束节点按分支输出。

**售后客服**（26 节点）：开始 → 问题分类器（9 类，覆盖「增删改查」「多操作」「闲聊」「退货政策」「售后进度」「发起退换货」）→ 分流到对应的 LLM 节点 → 变量聚合器合并 → 结束。另有「用户情绪判断」分类器，判到生气就先走 `安抚节点`。

**订单定位**（3 节点）：开始 → LLM（拿 `user_orders` 和被问的商品名做匹配，匹配不到就反问哪一单）→ 输出 `order_pick`。它被 `售后客服` 和「发起退换货」流程复用。

---

## 5. 重新绑定知识库（第二次导入必做）

yml 里两个「知识检索」节点（商品咨询）、一个「知识检索（退货政策）」（售后客服）指向的是我本机的数据集，导入后是空的红色/黄色提示。必须换成你自己的。

### 5.1 先建一个知识库

1. 顶部 **知识库** → **创建知识库**
2. 数据源选 **导入已有文本**（或直接上传文件）
3. 索引方式：
   - **高质量（向量检索）**：需要 embedding 模型，检索效果明显更好，**推荐**
   - **经济（关键词）**：不需要模型，但同义词、口语问法容易检索不到
4. 分块设置保持默认（分段 512 / 重叠 50）就够用
5. 先塞一两份文档进去（下面 5.3 有说明放什么），**知识库必须至少有一份文档，否则检索节点会报空**

### 5.2 回到工作流绑定

打开 `商品咨询` → 点 **知识检索** 节点 → 「知识库」下拉里选你刚建的库 → 确认 → 对 **知识检索 2** 重复一次。
打开 `售后客服` → 点 **知识检索（退货政策）** → 同样换成你的库 → 保存。

> 如果节点里提示「嵌入模型不可用」，把它改成你建库时用的那个 embedding 模型（同一个库必须用同一个模型），或者直接忽略——重新选库后 Dify 一般会跟着库的设置走。

### 5.3 知识库里该放什么

| 用途 | 放什么 | 谁用 |
| --- | --- | --- |
| 商品咨询 | 每个商品一份 markdown：名称、类目、价格、参数、卖点、发货与保修说明 | `商品咨询` 的两个检索节点 |
| 退换货政策 | 退换货条件、时间窗口、运费谁承担、不支持退换的商品、仲裁规则 | `售后客服` 的「知识检索（退货政策）」 |

商品文档**不用手写**：商户后台每上架/修改一个商品，后端会通过 Dataset API 自动把商品资料写成一份文档同步进去（见 `公共/knowledge.py`，落盘副本在 `商品咨询/` 目录）。所以只需要：

1. 建库
2. 拿这个库的 ID 和 API Key 填进 `config.json`（见第 6 节）
3. 后台重新上架一次商品（或点一次「同步」），文档就进去了

知识库 ID 从浏览器地址栏拿：打开知识库，URL 形如 `/datasets/3f6a1c2e-....../documents`，中间那段 UUID 就是 `dataset_id`。

知识库 API Key：知识库页面左侧 **API** → **创建 API 密钥** → `dataset-` 开头。

---

## 6. 发布应用、拿 Key、填配置

### 6.1 每个工作流都要「发布」

工作流改完必须点右上角 **发布 → 发布更新**，否则 `/workflows/run` 调到的还是上一个版本（这块特别容易忘，改了半天没效果十有八九是没发布）。

### 6.2 拿 API Key

每个应用左侧菜单 **访问 API** → 右上角 **API 密钥** → **创建密钥** → 复制 `app-` 开头的串。

Dify 默认可能已经给了你一个 default key，直接用也行。

### 6.3 填进 config.json

在项目根目录：

```bash
cp config.example.json config.json
python 工具/初始化配置.py
```

脚本会逐项问（回车保留默认值、密钥不回显），其中 Dify 相关的就是这 5 项：

```
Dify 地址                        http://localhost/v1
Dify「订单定位」Key              app-...
Dify「售后客服」Key              app-...
Dify「商品咨询」Key              app-...
Dify 知识库 Key                  dataset-...
Dify 知识库 ID                   3f6a1c2e-...
```

也可以手改 `config.json`：

```json
{
  "dify": {
    "base_url": "http://localhost/v1",
    "workflows": {
      "order_locate": { "name": "订单定位", "api_key": "app-xxx" },
      "after_sales":  { "name": "售后客服", "api_key": "app-xxx" },
      "consult":      { "name": "商品咨询", "api_key": "app-xxx" }
    }
  },
  "dify_knowledge": {
    "api_key": "dataset-xxx",
    "dataset_id": "3f6a1c2e-...",
    "base_url": "http://localhost/v1"
  }
}
```

注意 `base_url` 结尾是 `/v1`，**不要写成网页地址**（`http://localhost/workflow/xxx` 是给人看的，不能 POST）。`config.json` 已在 `.gitignore` 里，不会被提交。

也可以用环境变量覆盖（优先级高于 `config.json`）：`KB_DIFY_CONSULT_KEY`、`KB_DIFY_AFTER_SALES_KEY`、`KB_DIFY_ORDER_LOCATE_KEY`、`KB_DIFY_DATASET_KEY`、`KB_DIFY_BASE_URL`。

检查有没有漏填：

```bash
python 工具/初始化配置.py --check
```

---

## 7. 验证：先 curl，再开页面

**永远先直接打接口**，这样能分清是「工作流的问题」还是「项目的问题」。

```bash
# 商品咨询
curl -s -X POST http://localhost/v1/workflows/run \
  -H "Authorization: Bearer app-你的商品咨询Key" \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"user_input":"这个键盘支持蓝牙吗"},"response_mode":"blocking","user":"test"}'
```

期望返回里有 `"status":"succeeded"`，`data.outputs.one`（或 two/three/four）是一段中文回答。

```bash
# 订单定位（user_orders 可以直接贴一段订单文本）
curl -s -X POST http://localhost/v1/workflows/run \
  -H "Authorization: Bearer app-你的订单定位Key" \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"user_input":"我那个键盘的订单到哪了","user_orders":"1. 订单 CS20261111001 罗技K380 键盘 ¥299 已发货"},"response_mode":"blocking","user":"test"}'
```

期望 `data.outputs.order_pick` 里有那笔订单。

两条都通之后，再启动后端：

```bash
python main.py
# 或双击 启动.command
```

打开客户商城 → 商品详情 → **咨询这件商品**，问一句试试；再进「我的订单」→ 售后，问「我要退货」。工作流答不上来的时候，先去 Dify 的 **日志与标注** 里看那一次运行卡在哪个节点——比在项目里瞎猜快得多。

---

## 8. 常见报错对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `404 app not found` / `invalid api key` | Key 填错，或应用被删了 | 重新发 Key；确认 `base_url` 是 `/v1` |
| `Connection refused` / 页面提示「客服系统暂时无法连接」 | Dify 没起，或 base_url 端口不对 | `docker compose ps` 看看；改 `dify.base_url` |
| 工作流成功但页面没反应 | 输出字段名被改了 | 对照 4.2 恢复字段名（`one/two/three/four`、`order_pick`、`json`…） |
| `inputs is required` / 参数校验失败 | 开始节点少了必填变量 | 对照 4.1 补上变量名 |
| 回答「没有找到相关商品信息」 | 知识库是空的 / 没绑定 / 检索不到 | 看知识库文档数；检查检索节点选的是不是你那个库 |
| 检索节点报「知识库不存在」 | 还在用 yml 里我本机的 UUID | 第 5 节重新绑定 |
| 节点报「模型不可用」 | 环境没有 `deepseek-v4-flash` | 在供应商里换成本机有的模型，或者把全部 LLM 节点批量改一遍（应用里逐个点） |
| 改了工作流但效果没变 | 没点发布 | 右上角 **发布 → 发布更新** |
| 退货政策问题答成闲聊 | 问题分类器把类目判错了 | 打开分类器节点，补类目描述里的例子，或调高模型温度以外的判别精度 |

---

## 9. 改完工作流怎么同步回仓库

1. 在 Dify 里调好、**发布**
2. 应用右上角 **···** → **导出 DSL**，覆盖仓库里对应文件（**文件名不要改**，README 和网关都按这个名字引用）
3. `git diff` 看一眼有没有把 Key 带进去（DSL 里不应该有 Key；有就说明你粘进了节点，要清掉）
4. 提交

`.draft.yml` 是你自己在 Dify 里另存的草稿副本，可以不管；但别把 `.draft.yml` 当正式文件提交上来，容易和 `.yml` 对不上。

---

## 10. 一句话总结

导入 3 个 yml → 全部重新绑知识库 → 每套都发布 → 拿 3 个 `app-` Key + 1 个 `dataset-` Key 和 dataset_id → 填 `config.json` → 先 curl 后开页面。

只要 curl 能返回 `succeeded`，项目这边基本就是通的；如果 curl 都不通，问题一定在 Dify，不在代码。
