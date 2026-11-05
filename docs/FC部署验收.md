# OXR FC 1.1.0 部署验收

验收日期：2026-11-05。本报告记录 OXR 独立服务；Router 后续由用户恢复部署，其验收单独记录在 Router 项目的 docs/FC部署验收.md，不混入本报告的 OXR 统计。

Router 发布过程中再次复查 OXR：就绪接口 200，真实 table.png 同步解析 200、业务 code=0，耗时 2.469 秒。OXR 镜像和配置未因 Router 发布而改变。

## 当前线上版本

| 项目 | 实际值 |
|---|---|
| 函数 | rethos-oxr-demo / LATEST，华东1杭州 |
| HTTPS Base URL | https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run |
| API 版本 | 1.1.0 |
| 源码构建提交 | a272c41，deploy/aliyun-fc 分支 |
| 镜像标签 | fc-v2-a272c41 |
| 镜像 Digest | sha256:be89fb3ee6cf2df3d6bcc189aebcc4fa812e0f966255c66a05eb67d17f4970df |
| 镜像大小 | 9,013,314,182 bytes，约 8.394 GiB |
| ACR 构建 ID | 01A1ABD3-CE6B-52A5-8714-5B6A7010E50E |
| 构建结果 | SUCCESS，构建耗时约 273 秒 |
| GPU / 计算 | Ampere / NVIDIA A10 24 GB，8 vCPU，32 GB 内存 |
| 并发 / 弹性 | 单实例并发 1；无最小实例策略，按量弹性最小实例数 0 |
| 入口 / 超时 | 默认镜像启动命令，监听 9000，函数超时 660 秒 |
| 预热 | FC 调用代码 Initializer 300 秒，内部启动期限 240 秒 |
| 权重 | NAS `/mnt/oxr-models/OXR-1.0` |
| 异步与 Demo 结果 | NAS `/mnt/oxr-models/.oxr-jobs`，默认保留 7 天 |

已核对 CPU、内存、GPU、并发、磁盘、NAS、VPC、Initializer、互联网访问、函数超时、角色和日志配置与发布前一致。仅更新容器镜像与所需环境变量；旧 `fc-v1` 和受保护配置备份保留用于回退。

新代码推送到原始仓库 `Rethos-ai/Resource-OXR` 及个人仓库 `nfnx/oxr-fc` 的 `deploy/aliyun-fc` 分支；没有覆盖 main。构建使用原有个人仓库绑定。

## 实际提供的业务 API

1. `POST /v1/parse/sync`：同步解析图片或 PDF，返回 Markdown、结构化 JSON。
2. `POST /v1/parse/async`：输入持久化至 NAS，提交 FC 原生异步调用。
3. `POST /v1/parse/async_check`：按 task_id 查询排队、处理、成功或失败及结果。
4. `POST /v1/demo/jobs`：提交带预览资源的 Demo 文档任务。
5. `GET /v1/demo/jobs/{job_id}`：获取状态及页面预览地址。
6. `GET /v1/demo/jobs/{job_id}/result`：获取 Demo 完整解析结果。
7. `GET /v1/demo/jobs/{job_id}/assets/{asset_id}`：下载页面预览或提取图片。

另有 `/`、`/health/live`、`/health/ready`、`/docs`、`/redoc`、`/openapi.json`。`/initialize` 与带私密任务票据的 `/internal/tasks/run` 属于内部生命周期/执行接口。原 `/demo` 网页没有对外转发；其四个业务 API 已提供。

## 验收结果

| 组别 | 结果 | 证明的内容 |
|---|---|---|
| 本地回归 | 39/39 通过 | 生命周期、超时后停止接单、上传校验、持久化日志、重复投递只执行一次、原子领取、过期清理范围、图片 base64/none、契约和参数边界；模型后端为模拟实现 |
| 云端业务主回归 | 最终一轮 43/43 检查通过 | 同步图片/PDF、格式选择、图片内嵌解码、表格、真实异步完成、Demo 状态/结果/图片下载、无 Token 拒绝、无效参数、3 客户端并发文件对应关系 |
| 云端文件边界 | 3/3 通过 | 21 页 PDF、超过 2500 万像素图片、多帧 TIFF 均返回 413 |
| 输出语义复核 | 6/6 通过 | 两种表格表示的块类型/格式正确；两次均识别中文“文档解析测试”；公式块返回 LaTeX `\\mathrm{E=mc^{2}}` |
| 图片格式追加 | JPG、WebP、BMP、压缩 TIFF 通过 | 核对发票号 7319、金额 42.00；PNG 在主回归中通过 |
| 完整选项与警告 | 2/2 通过 | extra、feature_config、element_formats 组合可用；未实现特性只产生 ignored 警告，不伪称实现 |
| 独立 NAS 读回 | 5/5 通过 | 临时 CPU 函数的独立实例读取原 GPU 异步结果、Demo 状态/结果和图片；图片字节可解码 |
| Postman 完整业务集合 | 11 请求 / 26 断言，通过，退出码 0 | 覆盖全部七个业务 API，加健康和 OpenAPI |
| Postman 未压缩 TIFF | 1 请求 / 3 断言，通过，退出码 0 | 5,760,140 bytes TIFF 上传并解析成功 |
| Postman 安全/大小边界 | 2 请求 / 2 断言，通过，退出码 0 | 错误 Bearer 返回 403；21 MiB 请求正文返回 413 |

Postman 共 14 个请求、31 条断言通过，均为本地 CLI 调用真实 FC，关闭云上报，没有登录或上传工作区。主回归中的轮询检查数随任务完成时间变化；首轮 45 项通过，最终保留回执的一轮为 43 项，不把重复轮询当作独立业务能力。

真实返回中，中文、表格内容、页号、坐标、阅读顺序和公式块均可见。图片模式 `base64` 已解码验证为 PNG，返回值不再引用 `/tmp` 本地路径。Demo 下载链接需要同样的 Bearer Header；不会把服务器文件路径交给调用者自行访问。

## 耗时与性能边界

- 首次请求包含镜像准备与冷启动，实测约 **209.672 秒**；该数字是本轮单次观察，不能作为固定启动时长。
- 真实图片样例热调用约 0.36–2.52 秒，两页 PDF 约 0.55–1.98 秒；重复相同内容可能命中内部缓存。
- 主回归 3 个并发客户端请求均通过，文件名与各自结果对应。最终轮客户端耗时约 0.875、1.172、1.485 秒。
- 这是小样本功能及短并发验证，没有完成长期压测、容量/SLA 验证；不表示三个请求同时占用三张 GPU。
- 查询请求可能等待平台调度或同实例串行执行：最终轮一次异步查询约 10 秒、一次 Demo 查询约 2.56 秒。不能把所有查询都承诺为几十毫秒。

## 保留的失败与未覆盖边界

1. 独立 GPU 发布版本的探针首轮返回 `HTTPTriggerNotFound`；第二轮等待路由后收到 HTTP 429，未获取足够信息进一步归因为库存或账户限制。临时触发器和版本均已删除。现有 LATEST 的业务测试通过；**没有完成 GPU 实例回收后重新分配的完整验收**。
2. 更改函数环境变量后查询仍观察到同一个实例 ID，故该检查不能当作新 GPU 实例验证。随后用独立 CPU 函数读回同一 NAS，确认跨实例结果共享；该验证没有执行 GPU 推理。
3. Windows Python requests 首次上传未压缩 TIFF 遇到 TLS 传输异常，未收到 HTTP 响应。压缩 TIFF 随后通过；同一未压缩文件改用 Postman 真实调用，HTTP 200 且 3 条断言通过。原传输失败回执保留。
4. Windows 本地环境缺少 `sopdf`，未将原项目全部测试套件的收集失败算作功能测试执行；本轮 39 项专项回归与真实 Linux/GPU 调用分别计数。
5. 未逐项构造全部版面类别、化学结构、所有公式、全部超大结果及灾难恢复场景。模型识别质量应结合真实业务样本持续验收。
6. 当前限制为请求正文 20 MiB、PDF 20 页、图片 2500 万像素且单帧、单个结果响应预算 28 MiB。Word/Excel/PPT 须先转 PDF；ZIP、MathML/ASCII 公式及表格图片输出未实现。

异步采用 FC 原生投递及 NAS 原子领取，不依赖响应结束后的进程内 BackgroundTasks。中断任务超出执行期限会明确报告失败，不能恢复推理进程，也不会在未知结果时自动重放模型调用。结果属于共享 Token 下的服务数据，不提供多租户用户级隔离或公开删除接口。

## 回执、交付与清理

公开调用文档为 `docs/FC接口文档.md` 和根目录 `接口文档.html`；根目录 `接口文档.private.html` 是 ACL 受限的本地预填 Token 副本，不提交 Git、不上传共享服务。OpenAPI 与 Postman 集合位于 `deploy/fc/`。

详细脱敏响应、样本、报告和日志保留在 Git 忽略的 `output/fc-v2-live/`，主要包括 `report.json`、`boundaries-report.json`、`formats-report.json`、`tiff-followup-report.json`、`nas-readback-proof.json` 和 `postman*.log`。构建与配置核对在 `output/fc-v2-build.json`、`output/fc-v2-config-check.json`。失败探针单独保留，不混入通过数量。

临时 CPU 函数 `oxr-nas-readback-20261105`、其触发器、两轮临时 GPU 版本/触发器已删除。21 MiB 临时上传文件已从本机清理。测试生成的少量异步/Demo NAS 结果遵循默认 7 天保留策略；后续提交触发清理，只处理本服务任务目录，不清理模型或其他业务文件。原函数、NAS、ACR 与旧镜像继续保留。

本轮没有操作无关生产 Runtime、网关、业务数据库或 110/124 服务器，没有推送或部署 Router。
