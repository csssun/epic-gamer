# Epic Awesome Gamer · GitHub Actions + New API

基于 csssun/epic-awesome-gamer1 main 提交 437e9b8 修改，继承原项目 GPL-3.0-or-later 许可。完整源码，非局部补丁。

## 运行方式

**直接在 GitHub Actions 配置 Secrets 后运行。无需本机登录，无需导出或导入 Cookie。**

保留 Camoufox 与 hcaptcha-challenger 调用链，模型请求改走 New API 的 OpenAI 兼容接口。此版本不使用 AiHubMix、`/gemini` 路由或 Google 文件上传 API。

1. 把压缩包内文件按原路径覆盖到仓库，提交全部修改，尤其不能漏掉 `uv.lock`、`app/services/new_api_adapter.py`、`app/check_new_api.py` 和 `.github/workflows/epic-gamer.yml`。
2. 在 Settings → Secrets and variables → Actions → New repository secret 添加下表五项。
3. 在 Actions 中选择 **Epic Awesome Gamer (New API)** → Run workflow。

| Secret 名称 | 内容 |
|---|---|
| `EPIC_EMAIL` | Epic 账号邮箱 |
| `EPIC_PASSWORD` | Epic 账号密码 |
| `NEW_API_BASE_URL` | New API 地址，例如 `https://你的域名/v1` |
| `NEW_API_API_KEY` | New API 创建的令牌，而非上游厂商的密钥 |
| `NEW_API_MODEL` | 此令牌和渠道实际支持的视觉模型名称，必须精确匹配 |

地址可以填写站点根地址、以 `/v1` 结尾的地址或完整 `/v1/chat/completions` 地址，程序会规范化，避免重复 `/v1`。必须能被 GitHub runner 访问，不能填写仅本机可达的 localhost 地址。

旧的 `GEMINI_API_KEY`、`GEMINI_BASE_URL`、`GEMINI_MODEL` 不再使用。验证码库内部仍有一个名为 GEMINI_API_KEY 的配置字段，由代码自动适配；你不需要添加这个 Secret，也不会向 Google 发请求。

可选仓库 Variables：

| Variable | 默认值 | 用途 |
|---|---|---|
| `EPIC_COUNTRY` | `CN` | 改成 Epic 账户实际地区，例如 US |
| `NEW_API_RESPONSE_FORMAT` | `json_object` | 如渠道不支持 JSON mode，设为 `prompt`；支持 JSON Schema 的渠道可设 `json_schema` |

`prompt` 只取消接口的 response_format 参数，仍在提示词中提供 JSON Schema，并对返回值做本地校验。切换模式不能修复无视觉能力、错误模型名、无权限或余额不足。

## Actions 执行步骤

1. 校验五个 Secret 是否存在（不会打印值）。
2. `uv sync --locked` 安装一致的依赖。
3. 用一张代码生成的绿色小图调用 New API，检查图片输入和结构化返回。这会产生一次小额模型调用；它验证接口能力，不证明验证码识别准确率。
4. 安装 Firefox 系统依赖及 Camoufox。
5. 尝试账号密码登录，当前可见 hCaptcha 使用 New API 模型处理。
6. 获取有效周免、提交可核验的零金额订单，并在对应商品页检查已拥有状态。
7. 上传 `epic-result` 中的 result.json；失败返回非零退出码。

默认每周五、周一北京时间 11:30 运行，同一仓库禁止任务并发。每次 hosted runner 运行从账号密码开始，不依赖会话导入或持久化。

## New API 适配实现

请求地址 `POST /v1/chat/completions`，认证 `Authorization: Bearer ...`，请求体使用 `model`、`messages`、`stream:false`。图片以内嵌 Base64 `image_url` 发送，支持多张图和文本。原库的本地“上传”转换为内存中的图片数据，不调用 Google Files API。

适配器只替换验证码库五个模型模块的 Client 工厂，不全局修改 google.genai.Client。返回的 Chat Completions 文本会转换为原库期望的 `.text`、`.parsed` 和 `.model_dump()`，支持 Pydantic JSON Schema 和枚举分类输出。所有模型阶段统一使用 `NEW_API_MODEL`，不误用库内默认模型名。

## 登录和领取修复

- 根据当前输入框所在表单选取可用提交按钮，不无条件排除 Continue，也不强制启用按钮。
- 登录仅接受商城导航 isloggedin=true，不能把跳转当作成功。
- 认证失败中止，不继续领取；不吞掉异常制造 Actions 假绿。
- 有效周免检查起止时间与实际零价，不按 namespace 错误跳过其他 offer。
- 下单前检查商品标题和总计为零；下单后核验对应商品主按钮显示已拥有。
- 验证码处理有外层超时和尝试上限；处理器返回失败时不会记作成功。

## 当前验证边界

已安装依赖、通过 49 个离线测试，并验证 CLI 入口、静态检查与编译。其中一个测试实际加载 hcaptcha-challenger 的 ImageClassifier，在 HTTP 模拟响应下验证整条 New API 适配链路。没有使用真实验证码做测试。

**尚未连接你的真实 New API 网关，没有远程执行你的 Actions，也没有证明真实 Epic 账号领取成功。** 本次之前的云浏览器访问 Epic 时显示“请完成安全检查以继续”。本包可直接由 Actions 启动，但无人值守能否完成取决于实际账户验证、模型识别、地区和商城页面。

当前结账实现支持商品页 Get → 即时结账。只有 Add to Cart、特殊年龄提示、EULA 提示、不同总计布局或不同 CTA 的页面会停止并报告，未声称已覆盖所有商品路径。邮箱验证码、2FA、通行密钥或 hCaptcha 以外的安全检查不由此适配器完成。

## 判断失败位置

- `Missing Actions Secrets`：补齐提示的 Secret。
- `NEW_API_CHECK_FAILED`：先检查 New API 地址、令牌权限、模型名及视觉支持。HTTP 400 还可能是渠道不支持 response_format，此时可尝试 Variable `NEW_API_RESPONSE_FORMAT=prompt`。
- `NEW_API_CHECK_OK`：接口图片请求和 JSON 响应已验证，继续观察登录阶段。
- `AUTHENTICATED`：已确认商城登录。
- `ALREADY_OWNED`：商品页确认已拥有。
- `CLAIMED_VERIFIED`：本轮下单后确认已拥有。
- `NO_ACTIVE_PROMOTIONS`：当前地区 API 没有有效零价周免，不等于账号已拥有所有游戏。
- 验证码超时、表单一直禁用、结账金额不可确认：任务失败，不显示虚假的领取成功。

模型请求错误不会打印密钥或上游响应原文。Actions 只上传 result.json，不上传 Cookie、模型图片、完整日志或浏览器资料。可分享失败阶段和 result.json 排查，不要分享账号密码、API 密钥或 Cookie。

## 本地开发（可选）

```bash
uv sync --locked
uv run --locked pytest tests/test_verified_flow.py tests/test_new_api_adapter.py -q
uv run --locked app/deploy.py --help
```

本地调试可把 `.env.example` 复制为 `.env` 并填写同样五项配置，再执行 `uv run --locked camoufox fetch` 和 `uv run --locked app/deploy.py`。这不是 GitHub Actions 运行的前置条件。

## 文件修改说明

完整替换了 settings、deploy、两个 service、utils、Actions 工作流、依赖和锁文件；新增 New API 适配器、接口预检查及测试；同步修正了 Celery 入口和 Docker 构建配置。没有推送你的远程仓库。

New API 官方接口文档：
https://docs.newapi.pro/en/docs/api/ai-model/chat/openai/createchatcompletion
