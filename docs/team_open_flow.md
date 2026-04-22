# Team 母号开通流程

本文档说明当前项目里的 Team 母号自动化开通链路，目标是：

1. 自动注册一个新的 ChatGPT 账号作为母号
2. 执行 Pro 预检两次
3. 生成 Team 支付链接并继续自动绑卡
4. 校验 Team 工作区是否开通成功

## 当前链路

1. Cloud Mail 创建邮箱
2. `register_user` 提交邮箱 + 密码注册
3. 邮箱 OTP 验证
4. `create_account` 提交姓名和生日，完成母号创建
5. 复用注册阶段会话，从 ChatGPT 当前会话中提取 `accessToken`
6. 创建 Pro checkout
7. Pro 预检两次
8. 生成 Team 支付链接
9. 自动提交 Stripe 绑卡
10. 校验 Team workspace

## 关键实现文件

- `backend/app/team_open_manager.py`
- `backend/platforms/chatgpt/chatgpt_client.py`
- `backend/platforms/chatgpt/refresh_token_registration_engine.py`
- `backend/platforms/chatgpt/team_open_payment.py`
- `frontend/src/TeamOpenPage.tsx`

## 注册阶段说明

注册阶段只负责母号创建和当前会话令牌提取，不应进入 Codex OAuth、passwordless 子流程或 add_phone 流程。

当前约束：

- `register_user`：使用 auth browser 预热 + sentinel，最终仍走 HTTP `session.post`
- `create_account`：沿用旧版稳定逻辑，不额外发 browser POST，避免 `invalid_state`
- 注册完成后直接从当前会话提取 `accessToken`

## Pro 预检说明

Pro 预检阶段会：

1. 创建 `chatgpt.com/backend-api/payments/checkout`
2. 打开 `chatgpt.com/checkout/openai_llc/<checkout_id>`
3. 等待 Cloudflare challenge 放行
4. 等待 Stripe Payment Element / iframe 加载
5. 自动填写账单信息与卡片信息
6. 提交后按“预期失败”继续下一次预检

当前为降低 checkout 风控，预检阶段会：

- 强制使用 `headed` 浏览器
- 使用注册阶段得到的会话 cookies 进入 checkout
- 识别 `__cf_chl_rt_tk` / `challenges.cloudflare.com` 并等待放行

## Team 支付链接阶段

预检完成后：

1. 调用支付链接服务生成 Team 支付链接
2. 自动打开支付页面
3. 自动填写卡信息并提交
4. 校验是否出现 Team 组织工作区

支付链接服务默认配置为：

- `https://team.aimizy.com`

## 前端页面

Team 母号开通页面位于：

- `frontend/src/TeamOpenPage.tsx`

页面职责：

- 配置 Pro 预检和支付参数
- 启动/停止 Team 母号任务
- 查看结果列表
- 查看事件日志

## 日志约束

运行日志只记录实际执行状态，不写流程解释性注释，不混入 Codex OAuth 之类的说明性文案。

## 已知风险点

1. ChatGPT checkout 页面可能触发 Cloudflare challenge
2. Stripe Payment Element 可能因 challenge 或风控延迟渲染
3. 住宅代理、headless/headed、浏览器上下文连续性都会影响 checkout 成功率

## 排查顺序

当 Team 母号开通失败时，优先按下面顺序看：

1. 注册是否成功
2. 是否成功提取 `accessToken`
3. Pro checkout 是否创建成功
4. checkout 是否真正进入 `/checkout/openai_llc/...`
5. 是否命中 Cloudflare challenge
6. Stripe Payment Element 是否渲染
7. Team 支付链接是否生成成功
8. 绑卡提交后是否出现 Team workspace
