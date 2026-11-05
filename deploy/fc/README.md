# OXR 阿里云 FC 部署

当前线上为 **1.1.0**，提供原服务的 3 个核心解析 API 和 4 个 Demo API。Router 本轮未部署。

- 调用文档：[FC接口文档](../../docs/FC接口文档.md)，本地 HTML 为根目录 `接口文档.html`。
- 真实验收：[FC部署验收](../../docs/FC部署验收.md)。
- 可导入集合：`oxr-fc.postman_collection.json`；契约：`openapi.json`。
- 历史构建失败和初版过程：[FC部署历史](../../docs/FC部署历史.md)，其中旧的待上线状态不代表当前状态。

## 当前部署

函数 `rethos-oxr-demo`，杭州，Ampere/A10 24 GB、8 vCPU、32 GB 内存、单实例并发 1、最小实例数 0。
监听 9000；使用镜像默认 Entrypoint，无额外启动命令；函数超时 660 秒；调用代码 Initializer 300 秒，镜像内部启动期限 240 秒。

镜像 `rethos-gpu-registry-registry-vpc.cn-hangzhou.cr.aliyuncs.com/rethos/oxr:fc-v2-a272c41`。
Digest：`sha256:be89fb3ee6cf2df3d6bcc189aebcc4fa812e0f966255c66a05eb67d17f4970df`。
ACR 构建规则：`deploy/aliyun-fc` 分支、上下文 `/`、Dockerfile `docker/Dockerfile.fc`、`linux/amd64`，自动构建关闭。
基础镜像为已复制到同一 ACR 的官方 vLLM 0.25.1 固定 amd64 digest，没有更换推理版本。

NAS 根目录挂到 `/mnt/oxr-models`；权重目录 `/mnt/oxr-models/OXR-1.0`，任务目录 `/mnt/oxr-models/.oxr-jobs`。
权重来源为 ModelScope `SoMark/OXR-1.0` master，已下载并建立本地 SHA-256 基线；该自校验不等于与官方发布哈希比较。
共享模型目录只读使用，输入和结果写到独立任务目录；任务默认保留 7 天，后续提交触发过期目录清理。

函数环境变量：

| 名称 | 用途 |
|---|---|
| OXR_FC_ASYNC_BASE_URL | 本函数公网 HTTPS Base URL |
| OXR_FC_ASYNC_TOKEN | HTTP 触发器当前有效 Bearer Token；仅配置在云端环境变量 |
| OXR_JOB_ROOT | `/mnt/oxr-models/.oxr-jobs` |
| OXR_DEPLOYMENT_REVISION | 当前代码构建提交 `a272c41` |

VPC、交换机、安全组、NAS 挂载和 ACR VPC 入口须可互通。入口 Bearer 验证由 FC HTTP 触发器执行；不要设为匿名。
原服务的 `api_key` 仅兼容参数，不替代 FC Bearer。所有私密凭据均不得进入 Git、Dockerfile、构建参数和公开文档。

## 更新与回退

1. 从原始 `origin` 获取业务更新，检查差异并运行本地测试。保留 `personal` 作为个人副本。
2. 本轮用户已授权同时推送两个仓库的 `deploy/aliyun-fc`；不改写 main、不 force push。
3. 每次构建使用新标签，记录 Git 提交、构建 ID、digest。禁止覆盖已经用于 FC 的标签。
4. 先备份现有 FC 配置和触发器到本地受保护目录；更新镜像和必要环境变量，保留网络、NAS、GPU、Initializer 配置。
5. 等待镜像准备完成，验证版本、ready、同步、异步、Demo 图片和错误处理，再确认上线。
6. 回退时切换至保留的 `fc-v1`，恢复对应受保护配置。该版本仅有同步 API，不能声称保留新版异步/Demo 能力。

## 本地检查

```powershell
python -m pytest tests/unit/test_fc_gateway.py tests/unit/test_fc_durable_jobs.py tests/unit/test_fc_picture_outputs.py -q --basetemp output/fc-unit
python deploy/fc/build_docs.py
```

Postman 集合使用 `base_url`、`fc_token`、`document_path`。本地运行可添加 `--no-report-events --delay-request 3000 --timeout-request 700000`，不需要登录或上传到云工作区。
