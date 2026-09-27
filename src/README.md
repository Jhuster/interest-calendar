# 开发目录

`src/` 保存兴趣日历的网站源码和开发契约。它与 `bin/` 分工明确：`src/` 用于修改、测试和构建，`bin/` 保存运行脚本；源码方式部署需同时保留 `src/`。

## 目录

- `app/`：FastAPI 服务、页面模板、静态资源、SQLite 迁移、协议校验、领域服务和 ICS 发布。
- `tests/`：接口、协议、权限、版本并发、发布恢复和 ICS 解析测试。
- `schemas/`：Agent 上传批次的 JSON Schema。
- `examples/`：协议有效、错误和回执样例，全部使用虚构数据。
- `pyproject.toml`、`uv.lock`：Python 依赖和锁定版本。

## 开发命令

在项目根目录执行：

```sh
uv run --project src pytest src/tests -q
cd src
uv run python -m app --help
```

应用运行数据不放在 `src/`，而放在 `bin/data/`。测试应使用临时目录，不能连接正式数据库。
