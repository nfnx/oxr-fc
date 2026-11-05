# OXR 阿里云 FC 部署

## 1.1.0 当前改造（尚未云端发布）

新增 NAS 持久化异步提交/查询、FC 原生异步 worker、持久 Demo 的四个业务接口；修复 Picture 的 base64/none 输出。完整调用合同见 `docs/FC接口文档.md` 和根目录 `接口文档.html`。权重与密钥不进入镜像或 Git。

部署必须注入 `OXR_FC_ASYNC_BASE_URL`（本函数 HTTP 触发器 HTTPS Base URL）和 `OXR_FC_ASYNC_TOKEN`（该触发器有效 Bearer Token）；默认 `OXR_JOB_ROOT=/mnt/oxr-models/.oxr-jobs` 复用已有 NAS 挂载。FC 原生异步投递只携带任务引用，文件/结果在 NAS；不能改为响应返回后的进程内 BackgroundTasks。

代码更新默认从组织 `origin` 获取；个人副本保留为 `personal`。用户本轮已明确授权 OXR 部署代码推送两边，无需重复确认。Router 本轮暂停。

以下为初版部署过程记录，其中“仅同步”“原 Demo 不覆盖”“upstream”及待构建的阶段描述不代表 1.1.0 的最终部署状态。真实状态必须以新的发布回执为准。

当前阶段：FC 适配源码已准备，需经过 ACR 实际构建、NAS 模型准备、FC 初始化和真实文档验收，才能认定上线成功。Windows 本地契约测试不代表 GPU 或 Linux 容器验收。

本轮本地结果：FC 网关15项通过；监督器可移植子集27项通过、5项POSIX集成跳过、5项因Windows路径/进程组差异未纳入该子集。首次完整尝试的Windows路径/killpg失败及临时父目录缺失没有计为功能通过。完整Linux进程回归、容器构建、真实PDF/GPU测试仍待云端进行。Postman集合只准备了3个真实验收请求，尚未运行；CLI未登录，未上传任何工作区或凭据。

## 已有资源

- 地域：杭州 `cn-hangzhou`。
- ACR 实例：`rethos-gpu-registry`（基础版）。
- 镜像仓库：`rethos/oxr`，ID `crr-t801o0u488tgobft`。
- 公网镜像名：`rethos-gpu-registry-registry.cn-hangzhou.cr.aliyuncs.com/rethos/oxr`。
- VPC 镜像名：`rethos-gpu-registry-registry-vpc.cn-hangzhou.cr.aliyuncs.com/rethos/oxr`。
- 代码源：`nfnx/oxr-fc`；原组织仓库仅作为 upstream 保留。
- 原 `rethos-oxr-demo` 仍是官方示例，不应误报为 OXR；先不覆盖。

## 1. ACR 构建规则

| 字段 | 值 |
|---|---|
| 类型 | Branch |
| 分支 | deploy/aliyun-fc |
| 构建上下文 | /（仓库根目录） |
| Dockerfile 文件名 | docker/Dockerfile.fc |
| 镜像版本 | fc-v1（首次用；后续修改使用新版本，不能覆盖发布标签） |
| 架构 | linux/amd64，如页面提供此项 |
| 构建参数 | 首次保持空 |
| 自动构建 | 关闭，初期手动运行 |
| 海外源智能加速 | 开启 |
| 不使用缓存 | 关闭 |
| 镜像加速转换 | 不开启；基础版的新仅索引加速镜像有 FC 兼容限制 |

构建成功后记录实际 Git 提交、镜像 digest、压缩大小。现有固定 vLLM 基础镜像的可拉取性、Python 包冲突和镜像大小尚需 ACR 实际确认，不能仅凭 Dockerfile 语法承诺成功。不要把任何密码放进构建参数。

首次构建 `01A1AAC2-F482-56CB-9F2A-F84E97D6088F` 已失败：源码8a93229获取及Dockerfile解析成功，但阿里云Docker Hub加速地址对基础镜像manifest连续返回500，未进入依赖安装。随后只读核验官方Registry：v0.25.1索引digest仍为e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089，amd64子镜像为f0b9a0dc75a9fca3b6811e3279367b2d6a448055a000bfd13859587d74cef268，manifest/config均200，33层压缩合计约8.213GiB。FC Dockerfile改用显式registry-1.docker.io及该子镜像，未升级推理版本。此核验只证明本机代理出口可读取元数据，不证明ACR构建机已能下载全部层；需要重新构建确认。由于fc-v1此前未成功发布，本次可继续用该标签；成功后不得覆盖它。

第二次构建 `01A1AAC9-746C-5C85-BE7A-2F19A237220D` 使用57c0198，直连Docker Hub两次超时，仍未进入依赖安装。随后通过ACR制品订阅任务 `crast-m69pp5qj225u043t` 同步官方基础镜像，2026-11-05 16:27:13至16:35:19（北京时间）完成，控制台显示linux/amd64、8.213GB，digest与上述官方amd64子镜像完全一致。当前Dockerfile默认基础镜像已改为同实例VPC地址 `rethos-gpu-registry-registry-vpc.cn-hangzhou.cr.aliyuncs.com/rethos/vllm-base:v0.25.1@sha256:f0b9a0dc75a9fca3b6811e3279367b2d6a448055a000bfd13859587d74cef268`。这证明基础镜像已同步到ACR，不代表OXR构建或部署完成；需再次构建核验私有基础镜像拉取权限及后续依赖安装。

## 2. 模型与 NAS

创建或复用同地域的 NAS、VPC、交换机和挂载点；先确认计费，再操作。运行函数要有访问该挂载点的网络和权限。模型目录只读使用，上传文件不进入共享模型目录。

在一次性的、已挂载该 NAS 的准备环境执行 `prepare_model.py`，不会在每次 FC 冷启动重新下载。准备环境需要 huggingface_hub，可通过本机上传或 Git 获取本项目；确认 HF 官方模型完整 commit SHA 后使用该 SHA 下载：

```bash
python deploy/fc/prepare_model.py \
  --revision <40位模型提交SHA> \
  --target /mnt/oxr-models/OXR-1.0-<短SHA>
```

目标目录必须是新目录。下载失败的半成品不可以挂载上线；只有产生 `model-manifest.json` 且文件校验通过才能使用。保留模型完整目录，包括 `plugins/vllm` 和 `layout_analysis`。权重不进入 Git，不复制到 Windows 仓库。

`MODEL_PATH` 填写上述实际版本目录；Dockerfile 的 `/mnt/oxr-models/OXR-1.0` 只是默认路径，不会自动生成模型。

## 3. 新建 FC 函数

- 建议名称 `rethos-oxr`，保留原 Demo。
- 弹性实例：Ampere A10，24GB×1卡、8vCPU、32GB内存。
- 最小实例数 0；函数弹性实例上限先限制为 1；单实例并发 1。
- 镜像选 ACR 中实际构建成功的版本。FC 必须有镜像拉取权限和网络；优先同地域 VPC 地址，先验证实际拉取成功。
- 启动命令使用镜像默认 ENTRYPOINT；若必须显式填写：`/opt/oxr-venv/bin/python /app/deploy/fc/gateway.py`。
- 监听端口 9000；普通执行超时初测设 660 秒；HTTP 客户端读取超时大于 660 秒。
- 实例预热/Initializer 开启，类型“调用代码”，由平台调用 `POST /initialize`；超时先设 300 秒。内部总初始化等待 240 秒，失败查看日志，不盲目增加等待。
- PreStop 初期不启用。Uvicorn 终止信号触发生命周期清理，复用原监督器关闭所有子进程。
- NAS 挂载到 `/mnt/oxr-models`；环境变量 `MODEL_PATH` 指向实际已准备目录。
- 原模型环境严格检查 vLLM 0.25.1 / torch 2.11.0+cu130 / Transformers 5.13.1。用户已在示例环境实测 A10 驱动 580.95.05，不能以此替代新函数镜像的兼容性验收。
- 24GB 显存采用 vLLM 70%预算、4条序列，D-FINE 仍占GPU；实际能否容纳必须实测，不能保证长文档和高并发。

## 4. 认证和接口

HTTP 触发器必须启用 Bearer 认证，使用新的随机 Token，通过 HTTPS 调用。该镜像将认证交给 FC 入口，不应裸露到未经鉴权的公网。生命周期 `/initialize` 不接收任意命令或配置。

| 方法/路径 | 含义 |
|---|---|
| GET / | 服务名与版本，不证明模型就绪 |
| GET /health/live | HTTP 入口存活 |
| GET /health/ready | 三个后端健康才返回 200，否则 503 |
| POST /initialize | FC 初始化回调；不是每次解析前调用 |
| POST /v1/parse/sync | multipart 文件同步解析 |
| GET /docs | 入口文档，multipart 细节以本文件为准 |

不转发 `/demo`、原内存异步任务、任意后端URL、vLLM直连接口或本地文件下载。

Postman：Authorization 选 Bearer Token。解析使用 `POST https://<新函数域名>/v1/parse/sync`；Body 选 form-data，`file` 类型 File，选择真实 PNG/JPEG/PDF；重复文本字段 `output_formats` 分别填 `markdown`、`json`。不要手动写 Content-Type，Postman 自动生成 boundary。

其余表单字段：`feature_config`、`extra`、`element_formats` 是 JSON 字符串，沿用 OXR 支持范围。`api_key` 是兼容字段，可省略（内部补 dummy），不是认证。`element_formats` 不填时默认 `{"image":"base64"}`；若显式填写，image 必须为 base64 或 none，避免结果引用已回收实例的文件路径。

总 multipart 请求默认上限 20MiB（包含字段和文件），PDF最多20页，单帧图片最多2500万像素；大型/复杂PDF先做受控小样本，页数限制不构成内存占用保证。并发冲突429；超限413；文件错误422；模型未就绪503；后端错误502；后端超时504并关闭模型，需更换实例后再测，不自动重放有成本的解析。

解析响应沿用原 OXR：顶层 code=0 才是业务成功；非零 code 可能仍是HTTP200。检查 `data.result.outputs`、`data.metadata` 和 `warnings`。FC 初始化失败也可能在外层HTTP200返回，须检查 `X-Fc-Error-Type` 或 `errorMessage`，不能只看状态码。

## 5. 验收与回退

先确认无 Token 被拒绝、live/ready健康、PNG/一页PDF真实解析正确，再测试表格公式、多页PDF、错误文件、超限、两请求并发、回收后新建实例重载。记录原始响应、输出、冷启动、初始化、热调用、显存、费用；不把示例Hello World或本地模拟测试写成真实OXR成功。

第一版只有同步服务。长任务、多实例异步与持久化结果需要额外队列/存储改造，不能直接开放原有内存异步接口。

记录镜像digest、源码提交、模型SHA和FC配置。更新先发布新版本再切别名；回退镜像、模型路径和配置，不覆盖共享NAS模型或其他业务数据。模型加载失败或推理超时后检查日志，使用FC实例管理/发布新配置回收故障实例。

官方参考：
- https://help.aliyun.com/zh/functioncompute/lifecycle-hooks-for-gpu-function
- https://help.aliyun.com/zh/functioncompute/gpu-instance-model-storage-best-practices/
- https://help.aliyun.com/zh/functioncompute/custom-container/
