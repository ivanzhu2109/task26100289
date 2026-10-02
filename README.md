# docmigr — 可恢复的本地文档迁移组件

只复制文件、不删除源文件。进程在任意时刻被杀后，重新执行 `run()` 会收敛到
与从未中断的顺序执行完全相同的最终字节、元数据和逐项结果。

## API

```python
import docmigr

docmigr.create(workdir, destination, manifest, policy)  # 每个 workdir 一个任务
docmigr.run(workdir)        # 执行或恢复，直到全部条目终结；可安全重复调用
docmigr.inspect(workdir)    # 逐项状态、终态条目源内容 SHA-256、各目标路径元数据
```

- `manifest`: 有序列表，每项 `{"item_id", "source", "dest", "metadata"}`，
  其中 `dest` 为相对路径，`metadata` 为可 JSON 序列化的字典。
- `policy`: `"skip"` 或 `"overwrite"`。
- 条目结果：`COPIED` / `DUPLICATE_SKIP` / `CONFLICT_SKIP` / `OVERWRITTEN`。

## 持久化与崩溃恢复

- 账本：`<workdir>/docmigr.sqlite3`（`synchronous=FULL`），记录任务、逐项
  状态机（`PENDING -> DECIDED -> TERMINAL`）和各目标路径当前保存的元数据。
- 发布：源文件先完整写入目标目录下的隐藏临时文件并 fsync，再用原子
  `rename(2)` 落位——已有目标绝不被原地截断，也不会在中断时消失，
  新旧目标都不会暴露部分内容。
- 决策提交前记录暂存文件的 `(st_dev, st_ino)`；恢复时据此区分"本次执行
  自己发布的文件"与"执行前已存在的文件"，只补完成中断的账本提交，
  不重读源文件、不重放已终结条目、不回滚任务。
- 元数据与终态结果在同一个 SQLite 事务中提交，恢复后字节与元数据必然收敛。

## 测试

```bash
python3 -m pytest tests -q
```

中断测试通过环境变量 `DOCMIG_CRASH_AT=<point>[@N]` 在子进程中以真实
SIGKILL 强制退出（`after_stage` / `after_decide` / `after_rename` /
`after_commit`），随后恢复并与不中断执行逐项比较。
