# OXR GPU 云函数调用文档

本文对应 FC 适配版本 **1.1.0**。当前源码待部署与云端验收；不要把旧 `fc-v1` 的同步验收结果当成新版全部能力已上线。

## 1. 地址、认证和调用约定

目标 Base URL：`https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run`。

每个外部请求都需要 HTTP Header：

```http
Authorization: Bearer <FC_TOKEN>
```

FC_TOKEN 是 HTTP 触发器配置的 Bearer Token，不是阿里云 AccessKey，也不是模型供应商 Key。调用者不需要阿里云 SDK 或云账号。公开文档不包含真实 Token；受限本地私密版本可预填 Token。

调用解析服务可处理 PDF、PNG、JPG/JPEG、WebP、BMP、单帧 TIF/TIFF。输出可包含文字、标题、阅读顺序、表格、公式、图片区域及坐标。Word/Excel/PPT 应先转换成 PDF。模型识别质量须对照原件检查。

首次请求可能等待 GPU 分配、镜像准备和模型初始化。初始客户端读取超时建议 700 秒，连接超时 15 秒。HTTP 200 不等于业务成功：始终检查 `code == 0`、`data.error` 和实际输出。

## 2. 接口总览

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /v1/parse/sync | 同步解析并返回结果 |
| POST | /v1/parse/async | 持久化文件，提交 FC 原生异步任务 |
| POST | /v1/parse/async_check | 查询共享 NAS 中的任务状态及结果 |
| POST | /v1/demo/jobs | 提交带页面预览和图片资源的 Demo 任务 |
| GET | /v1/demo/jobs/{job_id} | 查询 Demo 状态、页面预览地址 |
| GET | /v1/demo/jobs/{job_id}/result | 获取 Demo 完整结果 |
| GET | /v1/demo/jobs/{job_id}/assets/{asset_id} | 下载页面或提取图片 |
| GET | / | 服务名、适配版本、同步解析路径 |
| GET | /health/live | HTTP 服务存活 |
| GET | /health/ready | 模型及后端就绪 |
| GET | /docs | Swagger UI HTML |
| GET | /redoc | ReDoc HTML |
| GET | /openapi.json | OpenAPI 描述 |

`POST /initialize` 是 FC 生命周期回调，平台自动调用。`POST /internal/tasks/run` 是内部异步执行入口，还需每任务私密票据，不是业务客户端接口。原 `/demo` 网页入口没有对外转发；Demo 的四个业务接口可由自己的前端调用，每次请求均携带认证 Header。

## 3. 同步解析

完整请求：

```bash
curl --request POST \
  'https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/parse/sync' \
  --header 'Authorization: Bearer <FC_TOKEN>' \
  --form 'file=@/absolute/path/document.pdf' \
  --form 'output_formats=markdown' \
  --form 'output_formats=json' \
  --form 'feature_config={"keep_header_footer":true}' \
  --form 'extra={"temperature":0,"top_p":1,"repetition_penalty":1}' \
  --form 'element_formats={"image":"base64","formula":"latex","table":"html","cs":"smiles"}'
```

Windows 请使用 `curl.exe`，并把文件路径换成本机真实路径。Postman：POST → URL → Authorization/Bearer Token → Body/form-data；`file` 类型为 File，其余为 Text。两行同名 `output_formats` 分别填写 markdown、json。不要手填 Content-Type，客户端负责生成 multipart boundary。

### 所有上传参数

| 参数 | 必填 | 类型 | 默认/允许值及解释 |
|---|---|---|---|
| file | 是 | 文件 | 一次一个 PDF 或单帧图片；传真实字节，不是文件名或 URL |
| output_formats | 否 | 重复文本字段 | 默认 markdown 和 json；仅返回 Markdown 时只传一行 markdown |
| feature_config | 否 | JSON 字符串 | `{"keep_header_footer":false}`；是否保留页眉页脚。其他布尔特性可能只产生“忽略”警告，不代表实现了对应功能 |
| extra | 否 | JSON 字符串 | 可选 temperature、top_p、repetition_penalty；不传则沿用后端默认 |
| extra.temperature | 否 | 数字 | 0 到 2（含），模型采样温度 |
| extra.top_p | 否 | 数字 | 0 到 1（含），核采样参数 |
| extra.repetition_penalty | 否 | 数字 | 大于 0，重复惩罚系数 |
| element_formats | 否 | JSON 字符串 | 图片默认 base64；其他默认 formula=latex、table=html、cs=smiles |
| element_formats.image | 否 | 字符串 | base64 或 none；base64 返回 Markdown 内嵌 data URI；none 不输出图片内容。FC 不接受 url/file |
| element_formats.formula | 否 | 字符串 | 仅 latex 实际受支持 |
| element_formats.table | 否 | 字符串 | html 或 markdown |
| element_formats.cs | 否 | 字符串 | smiles 或 image，化学结构输出形式 |
| api_key | 否 | 文本 | 原协议兼容字段，FC 自动补齐；它不是认证，不用填写真实密钥 |

限制：完整上传请求体默认不超过 20 MiB；PDF 1–20 页；图片最多 2500 万像素，且仅一帧；一个实例同时执行一个文档。结果 JSON 超过 28 MiB 时拒绝返回，应拆分文档或选择 image=none。该结果预算为 FC 传输预留余量，不是模型能力限制。

### 成功响应示例

```json
{
  "code": 0,
  "message": "任务成功",
  "warnings": [],
  "data": {
    "task_id": "example-task-id",
    "error": null,
    "result": {
      "file_name": "document.pdf",
      "outputs": {
        "markdown": "# 标题\n\n正文",
        "json": {"pages": [{
          "page_num": 0,
          "page_size": {"w": 1241, "h": 1754},
          "merge_content_from_pre_page": false,
          "blocks": [{"idx": 0, "type": "Title", "bbox": [20, 30, 500, 80], "content": "标题", "format": "text"}]
        }]}
      }
    },
    "metadata": {"page_num": 1, "file_type": ".pdf", "processing_time_ms": 1800}
  }
}
```

### 所有主要返回字段

| 字段 | 类型 | 解释 |
|---|---|---|
| code | 整数 | 0 表示业务成功；1133 表示业务参数错误；1137 表示解析失败 |
| message | 字符串 | 人类可读状态或错误说明，不要依赖完整文字做程序分支 |
| warnings | 字符串数组 | 被忽略或受限选项等提示；没有警告时为空 |
| data | 对象/null | 成功载荷；业务错误时可能为 null |
| data.task_id | 字符串 | 本次任务标识；同步任务不承诺可用异步查询接口追查 |
| data.error | 字符串/null | 错误信息；正常为 null |
| data.result.file_name | 字符串 | 上传文件名 |
| data.result.outputs | 对象 | 按 output_formats 选择的输出，不传的格式可能不存在 |
| outputs.markdown | 字符串 | Markdown；表格可能为 HTML，图片可能为 data URI |
| outputs.json.pages | 数组 | 按文档顺序排列的页面 |
| pages[].page_num | 整数 | 从 0 开始的页面编号 |
| pages[].page_size.w/h | 整数 | 渲染页面的宽/高，单位像素，不是 PDF 点数 |
| pages[].merge_content_from_pre_page | 布尔 | 当前实现的跨页合并标记；不能据此承诺已完成跨页表格拼接 |
| pages[].blocks | 数组 | 识别区块及阅读顺序 |
| blocks[].idx | 整数 | 页内区块索引，清理后可能有间隔 |
| blocks[].type | 字符串 | Text、Title、Caption、Footnote、Formula、Table、Picture、Page-header、Page-footer、Stamp、Chemical Structure、Code Block 等 |
| blocks[].bbox | 4 个数字的数组 | `[左,上,右,下]`，相对于该渲染页的像素坐标 |
| blocks[].content | 字符串 | 区块的文本、HTML、LaTeX 或 Markdown |
| blocks[].format | 字符串 | text、html、latex 或 markdown |
| pages[].unattached_formula_numbering | 可选 | 未能绑定到公式区块的编号信息；仅部分文档产生 |
| data.metadata.page_num | 整数 | 总页数，区别于 pages[].page_num 的从零编号 |
| data.metadata.file_type | 字符串 | 识别出的文件扩展名 |
| data.metadata.processing_time_ms | 整数 | 后端处理毫秒数，不包含完整公网请求、排队和全部冷启动时间 |

开启调试时 metadata 可能包含额外诊断字段。调用者应允许新增字段，不能假设所有 PDF 都有表格或所有图片都能识别为文字。

## 4. 异步提交与查询

提交：URL 换成 `/v1/parse/async`，上传参数与同步完全相同。客户端本身不要添加 `X-Fc-Invocation-Type: Async`，否则拿不到业务 task_id。

```bash
curl --request POST 'https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/parse/async' \
  --header 'Authorization: Bearer <FC_TOKEN>' \
  --form 'file=@/absolute/path/document.pdf' \
  --form 'output_formats=markdown' --form 'output_formats=json'
```

成功响应为 HTTP 200：

```json
{"code":0,"message":"Task submitted","warnings":[],"data":{"task_id":"32位十六进制ID","status":"queuing"}}
```

文件先写入共享 NAS，再由服务提交小体积任务引用到 FC 原生异步队列。模型推理不依赖返回 HTTP 响应后的进程内后台线程。查询可以命中另一个实例。

查询完整请求：

```bash
curl --request POST 'https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/parse/async_check' \
  --header 'Authorization: Bearer <FC_TOKEN>' \
  --form 'task_id=替换成提交返回的任务ID'
```

| 查询参数 | 必填 | 类型 | 解释 |
|---|---|---|---|
| task_id | 是 | 文本 | 异步提交返回的 ID；不是 RequestId 或同步解析内部 ID |
| api_key | 否 | 文本 | 兼容字段，可省略 |

```json
{"code":0,"message":"Task status","warnings":[],"data":{
  "record_id":null,"task_id":"32位十六进制ID","status":"success","file_name":"document.pdf",
  "result":{"file_name":"document.pdf","outputs":{"markdown":"正文","json":{"pages":[]}}},
  "error":null,"metadata":{"page_num":1,"file_type":".pdf","processing_time_ms":1800}
}}
```

查询的 envelope、result、metadata 解释与同步相同。record_id 是兼容字段，当前为 null。状态：

| status | 含义 | 客户端动作 |
|---|---|---|
| queuing | 等待 FC 异步投递/执行 | 每 2–5 秒查询，避免高频轮询启动更多 GPU 实例 |
| processing | 某个 worker 已原子认领 | 继续有界等待 |
| success | 成功，result/metadata 可用 | 保存结果，停止轮询 |
| failed | 失败，error 给出原因 | 停止轮询，检查原因，不无限自动重放 |

默认结果保留 7 天；过期查询 404，后续新任务提交时清理过期文件。输入文件在完成/失败后删除；未确认投递的输入保留至过期，便于核查。意外终止的 processing 任务在执行期限超过 720 秒后查询显示 failed；queued 超过 24 小时显示 failed。任务重投不会重复认领；该策略不承诺崩溃中的推理能原地续算。

如果提交响应为 HTTP 503，且 detail 中含 `async_dispatch_not_confirmed` 和 task_id，表示平台接收结果不确定。先按这个 task_id 查询，不要立即重复上传造成重复收费。

## 5. Demo 的四个业务接口

### 5.1 提交 Demo 任务

```bash
curl --request POST 'https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/demo/jobs' \
  --header 'Authorization: Bearer <FC_TOKEN>' \
  --form 'file=@/absolute/path/document.pdf' \
  --form 'temperature=0' --form 'top_p=1' --form 'repetition_penalty=1' \
  --form 'keep_header_footer=true'
```

file 必填，其余为可选文本字段。temperature/top_p/repetition_penalty 范围见第 3 节，keep_header_footer 默认 true。上传大小、页数、像素限制与核心 FC 解析相同，未沿用原本地 Demo 的 100 MiB 上传上限。

```json
{"code":0,"message":"success","data":{"id":"32位十六进制ID","status":"queued"}}
```

Demo 使用 `id`，核心异步协议使用 `task_id`。

### 5.2 查状态与页面预览

```http
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/demo/jobs/<job_id>
Authorization: Bearer <FC_TOKEN>
```

无 Body。data 包含 id、status、file_name、pages、metadata、error、result_url。queued/processing/success/failed 含义与异步相同。`pages[]` 包含从 0 开始的 page_num、page_size 和 image_url。image_url/result_url 是相对路径，拼接 Base URL 后访问，每次均携带 Token。生成前 pages 为空。

### 5.3 取结果

```http
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/demo/jobs/<job_id>/result
Authorization: Bearer <FC_TOKEN>
```

无 Body。成功返回 `{"code":0,"message":"success","data":{...}}`，data 为原 Demo 的解析结果对象（file_name、outputs、metadata）。未完成返回 409；不存在/过期返回 404。

### 5.4 下载图片

```http
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/v1/demo/jobs/<job_id>/assets/<asset_id>
Authorization: Bearer <FC_TOKEN>
```

无 Body。job_id 取提交响应，asset_id 从预览或解析结果返回的 URL 取得，不能随意指定实例路径。成功返回图片二进制及对应 Content-Type，而非 JSON。Postman 可 Save Response 保存。不存在/过期/非法路径返回 404。

## 6. 健康检查、文档和初始化

以下请求无 Body，都带同一 Authorization Header：

```http
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/health/live
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/health/ready
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/docs
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/redoc
GET https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run/openapi.json
```

根路径返回 service=`oxr-fc`、version、parse_path。live 返回 `{"status":"alive"}`，只证明入口存活；ready 返回 `{"status":"ready"}` 或 503。docs/redoc 返回 HTML，openapi.json 返回描述 JSON。HTML 页面在浏览器进一步加载描述/脚本仍需正确认证，Postman 请求 HTML 成功不等于完整浏览器交互已验收。

FC 自动 POST /initialize，不要求客户端每次调用。平台预热超时 300 秒，程序内部模型启动等待 240 秒。最小实例数为 0 时平台可回收空闲实例；NAS 权重和持久任务记录仍保留，下一实例需要重新加载模型。

## 7. 错误与重试

| HTTP | 典型内容 | 处理 |
|---|---|---|
| 200，code=1133 | 参数不支持/格式错误，data=null | 修改参数，不当成成功 |
| 200，code=1137 | 后端解析失败 | 检查文档和日志 |
| 403 | FC AccessDenied，无/错误/禁用 Token | 检查触发器和 Authorization；不要换成云 AK |
| 404 | 不存在、过期的任务/资源/路径 | 核对任务种类、ID、保留时间 |
| 409 | Demo 结果未完成 | 继续查询状态 |
| 413 | 请求体、页数、像素、帧数或结果超限 | 拆分/缩小文件，减少内嵌图片 |
| 415 | 非 multipart 或不支持的文件扩展名 | 使用正确上传方式 |
| 422 | 缺文件、损坏文件、非法 JSON 配置 | 修正请求 |
| 429 | instance_busy | 有界退避，不无限重试；不代表本轮已成功扩容 |
| 502 | 后端不可用或 worker 失败 | 检查健康及任务状态；不要自动重放未知结果 |
| 503 | 模型未就绪/投递未确认 | 检查初始化日志；有 task_id 先查询 |
| 504 | 后端推理超时 | 实例会停止模型以避免重叠执行，需替换实例后重试 |

FC 自身还可能返回 ResourceThrottled、ResourceExhausted、初始化错误等平台响应。平台错误常带 RequestId、Code、Message；保留 RequestId 以便定位。初始化失败有时外层 HTTP 200，检查 X-Fc-Error-Type 和 errorMessage。

## 8. Python 完整同步示例

```python
import os
import requests

base = "https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run"
headers = {"Authorization": "Bearer " + os.environ["OXR_FC_TOKEN"]}
with open("document.pdf", "rb") as file:
    response = requests.post(
        base + "/v1/parse/sync", headers=headers,
        files={"file": ("document.pdf", file, "application/pdf")},
        data=[("output_formats", "markdown"), ("output_formats", "json")],
        timeout=(15, 700),
    )
response.raise_for_status()
body = response.json()
if body.get("code") != 0:
    raise RuntimeError(body.get("message", "OXR failed"))
print(body["data"]["result"]["outputs"]["markdown"])
```

## 9. Python 完整异步示例

```python
import os
import time
import requests

base = "https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run"
headers = {"Authorization": "Bearer " + os.environ["OXR_FC_TOKEN"]}
with open("document.pdf", "rb") as file:
    r = requests.post(base + "/v1/parse/async", headers=headers,
        files={"file": ("document.pdf", file, "application/pdf")}, timeout=(15, 700))
r.raise_for_status()
submitted = r.json()
if submitted.get("code") != 0:
    raise RuntimeError(submitted.get("message"))
task_id = submitted["data"]["task_id"]
deadline = time.monotonic() + 900
while time.monotonic() < deadline:
    r = requests.post(base + "/v1/parse/async_check", headers=headers,
                      data={"task_id": task_id}, timeout=(15, 700))
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(body.get("message"))
    task = body["data"]
    if task["status"] == "success":
        print(task["result"]["outputs"])
        break
    if task["status"] == "failed":
        raise RuntimeError(task["error"])
    time.sleep(3)
else:
    raise TimeoutError("Polling deadline exceeded; keep task_id for later query")
```

## 10. 部署与验收状态

旧 fc-v1 曾通过真实同步图片/PDF测试。本文的新异步、Demo、图片修复属于 1.1.0，必须以本轮发布回执和真实验收结果为准。未完成真实压力、实例回收或故障注入测试前，不承诺吞吐量、SLA 或自动恢复推理进程。
