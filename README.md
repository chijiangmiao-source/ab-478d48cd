# 星载计算载荷 · 乱序执行轨迹复核器

在上传微码补丁前，复核一段乱序执行（OoO）轨迹，确认**错误预测分支**或**精确异常**
不会把错误路径上的寄存器结果写入已提交（体系结构）状态。

纯 Python 3.11 标准库实现，无第三方依赖。

## 复核的体系结构规则

- 初始 8 个体系结构寄存器 `r0–r7` 固定映射物理寄存器 `p0–p7`，其余物理寄存器可分配。
- **dispatch**：源寄存器标签必须对应当前重命名映射；为目的寄存器分配新物理标签，
  并在映射改写**之前**为分支建立检查点（映射表 / 空闲表 / 分配指针）。
- **writeback**：仅标记 ROB 项完成，不改体系结构状态；针对已被错误预测回滚清除指令
  的迟到写回一律**拒绝**（合法事件，单独留痕）。
- **resolve**：预测正确则保留检查点；误预测则立即截断该分支之后的全部更年轻 ROB 项、
  恢复检查点映射、隔离错误路径标签，保证它们不能进入已提交状态。
- **commit**：只能提交**已完成的 ROB 队首**；按序提交时才推进已提交映射并回收旧标签。
- **exception**：仅当异常指令到达 ROB 队首时交付；冲刷异常指令自身及全部更年轻指令，
  已写回但未提交的结果全部丢弃，映射恢复到异常前一条指令的精确状态。

发现首个违规事件即停止重放，并给出该事件**前后**的映射表、ROB、分支检查点与
物理标签（空闲 / 新回收 / 回滚隔离）证据。

输入规模：至多 **24** 条指令、**96** 个事件。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 复核页面 |
| GET | `/healthz` | 健康检查 JSON |
| GET | `/api/sample` | 内置示例轨迹 |
| POST | `/api/analyze` | 提交 `{instructions, events, rob_depth?, physical_registers?}` |

事件对象示例：

```json
{ "instructions": [ {"seq": 1, "op": "add", "dst": 1, "src1": 2, "src2": 3},
                    {"seq": 2, "op": "beq", "src1": 1, "src2": 2, "predicted": false} ],
  "events": [ {"type": "dispatch", "seq": 1},
              {"type": "writeback", "seq": 1, "value": 10},
              {"type": "resolve", "seq": 2, "target": "taken"},
              {"type": "commit", "seq": 1} ] }
```

- 分支方向字段：`target` / `taken`，取值 `taken|not-taken|true|false|1|0`。
- `resolve` 可用事件级 `predicted` 覆盖指令上的预测值。

## 本地运行

```bash
python3 app/server.py            # 默认 0.0.0.0:8080，可用 PORT/HOST 覆盖
```

## Docker / Compose

宿主端口可配置（默认 8080）：

```bash
HOST_PORT=9090 docker compose up -d --build app
curl -s http://localhost:9090/healthz
```

一次性校验服务（单元测试 + 构建检查 + API/HTTP 冒烟），运行后退出并返回结果码：

```bash
docker compose run --rm verify   # 全部通过退出码 0，任一失败非 0
```

## 本地校验

```bash
scripts/verify.sh                # 单元测试 + compileall + 起服务 + 冒烟
python3 -m unittest discover -s tests -v
```
