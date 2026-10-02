# task26100289

可恢复的本地文档迁移组件。持久状态使用 SQLite（workdir 内的 `task.db`）与本地文件，
只复制文件、从不删除源文件。进程在任意时刻被杀后，重新执行 `run` 会收敛到与
从未中断的顺序执行完全相同的最终字节、元数据和逐项结果。

## API

```python
import docmigrate

docmigrate.create(workdir, destination, manifest, policy)  # 持久化任务；每个 workdir 一个任务
docmigrate.run(workdir)                                    # 执行或恢复，直到全部条目终结；可重复调用
docmigrate.inspect(workdir)                                # 逐项状态 + 终态条目源 SHA-256 + 各目标当前元数据
```

- `manifest`：有序列表，每项为
  `{"item_id": ..., "src_path": ..., "dst_rel": ..., "metadata": {...}}`。
  多个条目可指向同一 `dst_rel`。
- `policy`：`"skip"` 或 `"overwrite"`。
- `run` 返回逐项结果列表；`inspect` 返回
  `{"policy", "destination", "items": [...], "destinations": {...}}`。

## 结果语义（按清单顺序，与执行时目标内容比较）

- 目标不存在 → 复制并保存元数据：`COPIED`
- 目标存在且 `skip` → 哈希相同 `DUPLICATE_SKIP`，不同 `CONFLICT_SKIP`（保留原字节与元数据）
- 目标存在且 `overwrite` → 采用本条目字节与元数据：`OVERWRITTEN`

## 崩溃恢复设计

每个条目按 `pending → decided → terminal` 状态机推进，决策（动作、结果、源 SHA-256、
发布令牌）在发布任何字节之前先持久化到 SQLite；恢复时按已持久化的决策继续，
绝不重新根据目标内容推断已中断条目的结果。文件发布用「临时文件 + `os.replace` 原子改名」，
元数据与终态在同一个 SQLite 事务中落盘，因此中断只会造成暂时的字节/元数据错位，
恢复后必然收敛。

## 测试

```bash
python3 -m pytest tests -q
```

中断测试通过环境变量 `MIGRATE_CRASHPOINT`（`after_decide` / `after_temp_write` /
`after_rename` / `after_finalize`）与 `MIGRATE_CRASHPOINT_ITEM` 在子进程中触发
真实 `SIGKILL`，并与不中断的参照运行逐项比较。
