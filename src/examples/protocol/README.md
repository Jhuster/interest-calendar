# 协议设计样例与预期

这些文件是接口设计工件，不是已运行的服务端测试。来源、事件和UUID均为虚构，不能导入真实日历。所有样例使用 `schemas/batch-1.0.schema.json`。

## 校验边界

已使用 JSON Schema Draft 2020-12 验证器并启用 format 检查，核对 cases.json 中11个请求样例的 schema_valid 预期。semantic_valid 是开发阶段的验收目标，本轮未执行服务端语义校验。

基础测试上下文：

- 服务时间：2026-09-27T10:05:00+08:00。
- state_epoch：7a824bda-e591-48d8-a3d9-d92ab39cc281；config_version：1。
- 存在有效兴趣：d096a9c4-59ae-41c4-958d-f66b11945e2e。
- 更新和延期案例另预置事件：9625f205-44d0-43d5-a6c0-5ce60f32c662，version=1，来源键与valid-create相同。
- 各案例独立执行，不能把所有“有效创建”顺序导入同一测试库；否则身份与版本前置条件会变化。
- 跨午夜案例改用2026-09-28T00:15:00+08:00作为服务时间，仍保留9月27日采集窗口。

## 样例矩阵

| 文件 | Schema | 语义预期 |
| --- | --- | --- |
| valid-create.json | 通过 | 新增并等待发布 |
| valid-update.json | 通过 | 全天补全时刻，保留UID |
| valid-empty.json | 通过 | 不改动已有日历 |
| valid-candidate.json | 通过 | 日期不明，仅保存候选 |
| valid-postponed-candidate.json | 通过 | 明确延期，仅候选；人工撤下旧条目不等于取消 |
| valid-midnight.json | 通过 | 跨午夜仍使用开始采集时窗口 |
| invalid-unknown-field.json | 拒绝 | 未声明字段，整批拒绝 |
| invalid-empty-with-event.json | 拒绝 | empty不能含事件 |
| invalid-update-version.json | 拒绝 | 更新事件不能使用创建版本0 |
| invalid-time-order.json | 通过 | 服务端拒绝结束早于开始 |
| invalid-state-epoch.json | 通过 | 服务端拒绝恢复前旧状态请求 |

Schema不检查：时间先后/日期交集、24小时时效、IANA时区是否存在、当前配置与对象版本、事件和候选合计数量、跨条目来源键重复、候选与提案目标一致性、事实可信度。上述必须由语义层和真实来源核验分别承担。

## 成功与重试场景

1. 上传valid-create.json，若导入成功但尚未发布，返回202与receipt-imported-pending.json形状的回执。
2. 假设响应丢失，Agent先GET同batch_id回执；查不到再原样重传文件，不修改ID或generated_at。
3. 服务端对已成功批次返回200，不增加事件、版本或SEQUENCE；若已发布，返回receipt-replayed-published.json形状的回执。
4. 即使此时距离采集超过24小时、config_version已改变，相同成功批次仍回放结果；state_epoch因数据库恢复改变时例外，返回STATE_RESET。
5. 同batch_id改title返回BATCH_ID_CONFLICT；不能覆盖原成功记录。
6. 如果原请求因配置/字段拒绝，原样重传仍返回同一4xx；修正后用新batch_id。
7. 429/503不占用批次ID；按Retry-After或有限退避后重传原文件。认证失败停止自动重试。

回执文件是响应示例，不属于批次Schema验证对象；request_id每次请求更新，原导入结果保持不变，publication_status可以随网站完成发布而变化。
