"""乱序执行轨迹复核引擎。

维护 ROB 顺序、寄存器重命名映射、空闲物理寄存器表与分支检查点，
逐条重放 dispatch / writeback / resolve / commit / exception 事件，
定位首个违反“错误预测分支 / 精确异常不得把错误路径结果写入已提交状态”的事件。

设计要点：
- 初始体系结构寄存器 r0..r7 固定映射物理寄存器 p0..p7，不可释放。
- dispatch 为目的寄存器分配新物理标签（优先取空闲链表，其次向高地址增长）。
- 每个条件/无条件分支在“映射改写之前”建立检查点（映射表 / 空闲表 / 分配指针）。
- 分支误预测：立即截断该分支之后的全部更年轻 ROB 项并恢复检查点；
  随后到达的、针对被清除指令的写回一律拒绝（记录为合法拒绝，不是违规）。
- committed_map 只随按序提交前进；异常到达 ROB 队首时整体冲刷，
  恢复异常前一条指令的已提交映射（精确状态），异常指令自身与更年轻指令都不提交。
- 任一事件违规即记录证据并停止重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

ARCH_REGS = 8
BRANCH_OPS = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "branch"}
UNCOND_OPS = {"j", "jal", "jump"}
NO_DST_OPS = (
    {"sw", "sb", "sh", "sd", "store", "nop"} | BRANCH_OPS | UNCOND_OPS
)
VALID_OPS = {
    "add", "sub", "mul", "div", "and", "or", "xor", "slt", "sltu",
    "addi", "ori", "andi", "xori", "slti", "li", "lui",
    "lw", "lb", "lh", "ld", "load",
    "sw", "sb", "sh", "sd", "store", "nop",
} | BRANCH_OPS | UNCOND_OPS
VALID_EVENTS = {"dispatch", "writeback", "resolve", "commit", "exception"}
MAX_INSTRS = 24
MAX_EVENTS = 96
DEFAULT_ROB_DEPTH = 16


class InputError(ValueError):
    """轨迹输入本身不合法（区别于执行违规）。"""


class Violation(Exception):
    def __init__(self, message: str, code: str, evidence: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.evidence = evidence or {}


@dataclass
class Entry:
    rob_seq: int
    instr: dict
    tag: int = -1                          # 为本指令目的寄存器分配的物理标签
    released_tag: Optional[int] = None     # dispatch 前目的寄存器的旧标签
    src_tags: dict = field(default_factory=dict)
    completed: bool = False
    value: Any = None
    resolved: bool = False
    actual_taken: Optional[bool] = None
    is_branch: bool = False


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"taken", "t", "1", "true", "yes", "y"}
    raise InputError(f"无法解析布尔方向: {v!r}")


def _is_branch(instr: dict) -> bool:
    if instr.get("branch") is True:
        return True
    return instr["op"] in BRANCH_OPS or instr["op"] in UNCOND_OPS


def _predicted(instr: dict) -> bool:
    if "predicted" in instr:
        return _as_bool(instr["predicted"])
    return instr["op"] in UNCOND_OPS  # 无条件跳转预测 taken，条件分支默认 not-taken


def normalize_trace(trace: Any) -> dict:
    """校验并规范化用户输入。"""
    if not isinstance(trace, dict):
        raise InputError("请求体必须是 JSON 对象")
    raw_instrs = trace.get("instructions", [])
    raw_events = trace.get("events", [])
    if not isinstance(raw_instrs, list) or not isinstance(raw_events, list):
        raise InputError("instructions 与 events 必须是数组")
    if len(raw_instrs) == 0:
        raise InputError("至少需要一条指令")
    if len(raw_instrs) > MAX_INSTRS:
        raise InputError(f"指令至多 {MAX_INSTRS} 条，收到 {len(raw_instrs)}")
    if len(raw_events) > MAX_EVENTS:
        raise InputError(f"事件至多 {MAX_EVENTS} 项，收到 {len(raw_events)}")

    instructions = []
    seen_seq = set()
    for i, ins in enumerate(raw_instrs):
        if not isinstance(ins, dict):
            raise InputError(f"指令 #{i + 1} 必须是对象")
        seq = ins.get("seq", i + 1)
        if not isinstance(seq, int) or seq <= 0:
            raise InputError(f"指令 #{i + 1} 的 seq 必须为正整数")
        if seq in seen_seq:
            raise InputError(f"指令 seq={seq} 重复")
        seen_seq.add(seq)
        op = str(ins.get("op", "nop")).strip().lower()
        if op not in VALID_OPS:
            raise InputError(f"指令 seq={seq} 的操作码 {op!r} 不支持")
        norm: dict = {"seq": seq, "op": op}
        for key in ("dst", "src1", "src2"):
            if key in ins and ins[key] is not None:
                r = ins[key]
                if not isinstance(r, int) or not (0 <= r < ARCH_REGS):
                    raise InputError(
                        f"指令 seq={seq} 的 {key} 必须是 0..{ARCH_REGS - 1} 之间的寄存器号"
                    )
                norm[key] = r
        if "predicted" in ins:
            norm["predicted"] = _as_bool(ins["predicted"])
        instructions.append(norm)

    events = []
    for i, ev in enumerate(raw_events):
        if not isinstance(ev, dict):
            raise InputError(f"事件 #{i + 1} 必须是对象")
        etype = str(ev.get("type", "")).strip().lower()
        if etype not in VALID_EVENTS:
            raise InputError(
                f"事件 #{i + 1} 类型 {etype!r} 非法，允许: {', '.join(sorted(VALID_EVENTS))}"
            )
        ne = {"type": etype}
        for key in ("seq", "tag", "value", "target", "taken",
                    "src1_tag", "src2_tag", "cause", "predicted"):
            if key in ev:
                ne[key] = ev[key]
        events.append(ne)

    rob_depth = trace.get("rob_depth", DEFAULT_ROB_DEPTH)
    if not isinstance(rob_depth, int) or rob_depth <= 0:
        raise InputError("rob_depth 必须为正整数")

    physical = trace.get("physical_registers")
    if physical is not None:
        if not isinstance(physical, int) or physical < ARCH_REGS:
            raise InputError(f"physical_registers 必须 >= {ARCH_REGS}")
    return {
        "instructions": instructions,
        "events": events,
        "rob_depth": rob_depth,
        "physical_registers": physical,
    }


class Simulator:
    def __init__(self, trace: dict):
        self.instructions = {ins["seq"]: ins for ins in trace["instructions"]}
        self.events = trace["events"]
        self.rob_depth = trace["rob_depth"]
        physical = trace["physical_registers"]
        # 未指定物理寄存器总数时采用可增长模式，给一个充裕上限。
        self.cap = physical if physical is not None else max(
            64, ARCH_REGS + len(trace["instructions"]) * 2 + len(self.events) + 8
        )
        self.grow_mode = physical is None

        self.map_table = {r: r for r in range(ARCH_REGS)}
        self.committed_map = dict(self.map_table)
        self.free: set[int] = set(range(ARCH_REGS, self.cap))
        self.alloc_ptr = ARCH_REGS
        self.rob: list[Entry] = []
        self.completed: dict[int, Any] = {}   # tag -> value（存活指令的写回值）
        self.checkpoints: dict[int, dict] = {}
        self.dispatched: set[int] = set()
        self.committed: set[int] = set()   # 已提交指令（异常后也不允许再次分派）
        self.squashed_seqs: set[int] = set()  # 已被回滚、尚未重新分派的指令
        # 随回滚失效的标签 -> 原属指令序号；对应指令重新分派前保持隔离不可分配
        self.dead_tags: dict[int, int] = {}
        self.next_rob_seq = 1
        self.log: list[dict] = []
        self.first_violation: Optional[dict] = None

    # ---------------- 工具 ----------------

    def _alloc_tag(self) -> int:
        usable = self.free - set(self.dead_tags)
        if usable:
            tag = min(usable)
            self.free.remove(tag)
            self.dead_tags.pop(tag, None)
            return tag
        referenced = set(self.map_table.values()) | {
            e.tag for e in self.rob if e.tag >= 0
        }
        while self.alloc_ptr < self.cap:
            tag = self.alloc_ptr
            self.alloc_ptr += 1
            if tag not in referenced and tag not in self.dead_tags:
                self.dead_tags.pop(tag, None)
                return tag
        raise Violation(
            "没有可分配的物理寄存器：重命名资源耗尽，dispatch 不能继续",
            "no_physical_register",
        )

    def _entry_by_seq(self, seq: int) -> Optional[Entry]:
        """优先返回存活 ROB 项（重新分派后旧记录已随截断移除）。"""
        for e in reversed(self.rob):
            if e.instr["seq"] == seq:
                return e
        return None

    def _snapshot(self) -> dict:
        # free 展示“可立即分配”集合：物理未占用且未处于回滚隔离
        allocatable = self.free - set(self.dead_tags)
        return {
            "map_table": dict(self.map_table),
            "committed_map": dict(self.committed_map),
            "free": sorted(allocatable),
            "dead_tags": [
                {"tag": t, "instr_seq": s} for t, s in sorted(self.dead_tags.items())
            ],
            "alloc_ptr": self.alloc_ptr,
            "checkpoints": [
                {
                    "rob_seq": k,
                    "instr_seq": v["instr_seq"],
                    "map_table": dict(v["map_table"]),
                    "free": sorted(v["free"]),
                    "alloc_ptr": v["alloc_ptr"],
                }
                for k, v in sorted(self.checkpoints.items())
            ],
            "rob": [
                {
                    "rob_seq": e.rob_seq,
                    "seq": e.instr["seq"],
                    "op": e.instr["op"],
                    "dst": e.instr.get("dst"),
                    "tag": e.tag,
                    "released_tag": e.released_tag,
                    "src_tags": dict(e.src_tags),
                    "completed": e.completed,
                    "resolved": e.resolved,
                    "is_branch": e.is_branch,
                    "actual_taken": e.actual_taken,
                    "at_head": (i == 0),
                }
                for i, e in enumerate(self.rob)
            ],
            "completed_tags": sorted(self.completed),
            "squashed_seqs": sorted(self.squashed_seqs),
        }

    # ---------------- 事件处理 ----------------

    def _do_dispatch(self, ev: dict) -> tuple[str, str, dict]:
        seq = ev.get("seq")
        if not isinstance(seq, int):
            raise Violation("dispatch 事件缺少指令序号 seq", "bad_reference")
        instr = self.instructions.get(seq)
        if instr is None:
            raise Violation(f"dispatch 引用了不存在的指令 seq={seq}", "unknown_instr")
        if seq in self.dispatched:
            raise Violation(f"指令 seq={seq} 被重复 dispatch", "double_dispatch")
        if seq in self.committed:
            raise Violation(f"指令 seq={seq} 已提交，不能再次 dispatch",
                            "redispatch_committed")
        if len(self.rob) >= self.rob_depth:
            raise Violation(
                f"ROB 已满（深度 {self.rob_depth}），seq={seq} 无法 dispatch",
                "rob_full",
                {"rob": self._snapshot()["rob"]},
            )

        # 源标签必须对应 dispatch 时刻的重命名映射
        src_tags = {}
        for arch_key, tag_key in (("src1", "src1_tag"), ("src2", "src2_tag")):
            if arch_key in instr:
                arch = instr[arch_key]
                expected = self.map_table[arch]
                src_tags[arch_key] = expected
                provided = ev.get(tag_key)
                if provided is not None and provided != expected:
                    raise Violation(
                        f"seq={seq} 的源 {arch_key}=r{arch} 标签为 p{provided}，"
                        f"但当前映射要求 p{expected}",
                        "src_mapping_mismatch",
                        {
                            "arch_register": arch,
                            "event_tag": provided,
                            "required_tag": expected,
                            "map_table": dict(self.map_table),
                        },
                    )

        dst = instr.get("dst")
        tag = -1
        released = None
        is_branch = _is_branch(instr)
        cp_before = None
        # 重新分派被回滚指令时，解除其旧标签隔离，使其立即可复用
        for t, s in list(self.dead_tags.items()):
            if s == seq:
                self.dead_tags.pop(t)
        if is_branch:
            # 检查点必须在任何映射改写之前建立
            cp_before = {
                "map_table": dict(self.map_table),
                "free": set(self.free),
                "alloc_ptr": self.alloc_ptr,
                "instr_seq": seq,
            }
        if dst is not None:
            tag = self._alloc_tag()
            released = self.map_table.get(dst)
            self.map_table[dst] = tag

        entry = Entry(
            rob_seq=self.next_rob_seq,
            instr=instr,
            tag=tag,
            released_tag=released,
            src_tags=src_tags,
            is_branch=is_branch,
        )
        self.next_rob_seq += 1
        self.rob.append(entry)
        if is_branch:
            self.checkpoints[entry.rob_seq] = cp_before
        self.dispatched.add(seq)
        self.squashed_seqs.discard(seq)
        desc = (
            f"r{dst} 重命名 p{released if released is not None else '-'}→p{tag}"
            if dst is not None else "无目的寄存器"
        )
        if is_branch:
            desc += "；已建立分支检查点"
        return ("dispatch",
                f"seq={seq}({instr['op']}) 进入 ROB#{entry.rob_seq}，{desc}",
                {"seq": seq, "tag": tag, "released_tag": released,
                 "src_tags": src_tags, "is_branch": is_branch})

    def _do_writeback(self, ev: dict) -> tuple[str, str, dict]:
        seq = ev.get("seq")
        tag = ev.get("tag")
        entry: Optional[Entry] = None
        # 先按指令判定回滚状态：被清除指令的迟到写回即使带着已复用的旧标签也必须拒绝
        if isinstance(seq, int) and seq in self.squashed_seqs:
            return ("refused",
                    f"拒绝写回：seq={seq} 属于被错误预测分支清除的错误路径，结果丢弃",
                    {"seq": seq, "tag": tag, "reason": "squashed"})
        if isinstance(seq, int) and seq in self.committed:
            return ("ignored",
                    f"seq={seq} 已提交，迟到的重复写回被忽略",
                    {"seq": seq, "tag": tag, "reason": "already_committed"})
        if isinstance(seq, int):
            entry = self._entry_by_seq(seq)
        if entry is None and isinstance(tag, int):
            for e in reversed(self.rob):
                if e.tag == tag:
                    entry = e
                    break

        if entry is None:
            if isinstance(tag, int) and tag in self.dead_tags:
                return ("refused",
                        f"拒绝写回：标签 p{tag} 已随错误预测回滚失效，结果丢弃",
                        {"seq": seq, "tag": tag, "reason": "dead_tag"})
            if isinstance(seq, int) and seq not in self.instructions:
                raise Violation(f"writeback 引用了不存在的指令 seq={seq}",
                                "unknown_instr")
            raise Violation(
                f"writeback(seq={seq}, tag={tag}) 找不到对应的存活 ROB 项",
                "writeback_unknown",
                {"seq": seq, "tag": tag},
            )

        if entry.completed:
            return ("duplicate",
                    f"seq={entry.instr['seq']} 重复写回（p{entry.tag} 已完成，忽略）",
                    {"seq": entry.instr["seq"], "tag": entry.tag})
        entry.completed = True
        entry.value = ev.get("value")
        if entry.tag >= 0:
            self.completed[entry.tag] = entry.value
        return ("writeback",
                f"seq={entry.instr['seq']} 执行完成，写回 p{entry.tag}",
                {"seq": entry.instr["seq"], "tag": entry.tag, "value": entry.value})

    def _do_resolve(self, ev: dict) -> tuple[str, str, dict]:
        seq = ev.get("seq")
        if not isinstance(seq, int):
            raise Violation("resolve 事件缺少分支指令序号 seq", "bad_reference")
        entry = self._entry_by_seq(seq)
        if entry is None:
            raise Violation(f"resolve 引用了未 dispatch 的指令 seq={seq}",
                            "unknown_instr")
        if not entry.is_branch:
            raise Violation(
                f"指令 seq={seq}({entry.instr['op']}) 不是分支，不能 resolve",
                "not_a_branch")
        if entry.resolved:
            raise Violation(f"分支 seq={seq} 被重复 resolve", "double_resolve")

        if "target" in ev:
            actual = _as_bool(ev["target"])
        elif "taken" in ev:
            actual = _as_bool(ev["taken"])
        elif "value" in ev:
            actual = _as_bool(ev["value"])
        else:
            raise Violation(
                f"分支 seq={seq} 的 resolve 缺少 target/taken 实际方向",
                "missing_branch_target")
        predicted = (_as_bool(ev["predicted"]) if "predicted" in ev
                     else _predicted(entry.instr))
        entry.resolved = True
        entry.actual_taken = actual
        entry.completed = True  # 解析即执行完成，可在队首提交

        if actual == predicted:
            return ("resolve",
                    f"分支 seq={seq} 预测正确（{'taken' if actual else 'not-taken'}），"
                    "检查点保留至提交后释放",
                    {"seq": seq, "predicted": predicted, "actual": actual,
                     "mispredicted": False})

        # 误预测：截断所有更年轻 ROB 项并恢复检查点
        idx = self.rob.index(entry)
        younger = self.rob[idx + 1:]
        cp = self.checkpoints.get(entry.rob_seq)
        if cp is None:
            raise Violation(f"分支 seq={seq} 缺少检查点，无法恢复",
                            "missing_checkpoint")
        squashed_tags = []
        for ye in younger:
            if ye.tag >= 0:
                squashed_tags.append(ye.tag)
                self.dead_tags[ye.tag] = ye.instr["seq"]
            self.completed.pop(ye.tag, None)
            self.squashed_seqs.add(ye.instr["seq"])
            self.dispatched.discard(ye.instr["seq"])
            self.checkpoints.pop(ye.rob_seq, None)
        del self.rob[idx + 1:]
        self.map_table = dict(cp["map_table"])
        self.alloc_ptr = cp["alloc_ptr"]
        self.checkpoints.pop(entry.rob_seq, None)
        # 按“当前仍被引用的标签”重算空闲表：既撤销年轻指令的分配，
        # 也保留回滚前更老指令提交所释放的标签，避免资源泄漏。
        referenced = set(self.map_table.values()) | {
            e.tag for e in self.rob if e.tag >= 0
        }
        self.free = {
            p for p in range(ARCH_REGS, self.cap) if p not in referenced
        }
        return ("recovery",
                f"分支 seq={seq} 误预测（预测 {'taken' if predicted else 'not-taken'}，"
                f"实际 {'taken' if actual else 'not-taken'}）：清除 {len(younger)} 条"
                f"更年轻指令 {[e.instr['seq'] for e in younger] or ''}，"
                "映射/空闲表恢复至分支前检查点，迟到写回将被拒绝",
                {"seq": seq, "predicted": predicted, "actual": actual,
                 "mispredicted": True,
                 "squashed": [e.instr["seq"] for e in younger],
                 "squashed_tags": squashed_tags,
                 "restored_map": dict(self.map_table)})

    def _do_commit(self, ev: dict) -> tuple[str, str, dict]:
        if not self.rob:
            raise Violation("ROB 为空时出现 commit 事件", "commit_empty_rob")
        head = self.rob[0]
        seq = ev.get("seq")
        if isinstance(seq, int) and seq != head.instr["seq"]:
            raise Violation(
                f"commit 要求提交 seq={seq}，但 ROB 队首是 seq={head.instr['seq']}，"
                "乱序提交被拒绝",
                "commit_out_of_order",
                {"requested_seq": seq, "head_seq": head.instr["seq"]})
        if not head.completed:
            raise Violation(
                f"ROB 队首 seq={head.instr['seq']} 尚未执行完成（无 writeback/resolve），"
                "不能提交",
                "commit_uncompleted",
                {"head_seq": head.instr["seq"], "tag": head.tag})

        tag = head.tag
        dst = head.instr.get("dst")
        new_committed = dict(self.committed_map)
        released_tag = None
        if dst is not None:
            released_tag = self.committed_map.get(dst)
            new_committed[dst] = tag
            # 上一架构映射的标签永久回收（绝不能是错误路径标签）
            if released_tag is not None and released_tag != tag and released_tag >= ARCH_REGS:
                self.free.add(released_tag)
                self.dead_tags.pop(released_tag, None)
        self.committed_map = new_committed
        self.checkpoints.pop(head.rob_seq, None)
        self.rob.pop(0)
        self.dispatched.discard(head.instr["seq"])
        self.committed.add(head.instr["seq"])
        return ("commit",
                f"按序提交 seq={head.instr['seq']}"
                + (f"：架构 r{dst} 确认指向 p{tag}，回收旧标签 p{released_tag}"
                   if dst is not None else "（无写寄存器）"),
                {"seq": head.instr["seq"], "tag": tag, "dst": dst,
                 "released_tag": released_tag,
                 "committed_map": dict(self.committed_map)})

    def _do_exception(self, ev: dict) -> tuple[str, str, dict]:
        if not self.rob:
            raise Violation("ROB 为空时出现 exception 事件", "exception_empty_rob")
        seq = ev.get("seq")
        if not isinstance(seq, int):
            raise Violation("exception 事件缺少指令序号 seq", "bad_reference")
        entry = self._entry_by_seq(seq)
        if entry is None:
            raise Violation(f"exception 引用了未 dispatch 的指令 seq={seq}",
                            "unknown_instr")
        if self.rob[0] is not entry:
            raise Violation(
                f"异常 seq={seq} 尚未到达 ROB 队首"
                f"（队首 seq={self.rob[0].instr['seq']}），精确异常只能在队首交付",
                "exception_not_at_head",
                {"head_seq": self.rob[0].instr["seq"], "fault_seq": seq})

        cause = ev.get("cause", "precise exception")
        flushed = [e.instr["seq"] for e in self.rob]
        fault_tag = entry.tag
        precise_map = dict(self.committed_map)
        self.rob.clear()
        self.checkpoints.clear()
        self.completed.clear()
        # 异常指令自身与全部更年轻指令都不提交；恢复异常前精确映射
        self.map_table = dict(precise_map)
        self.free = set(range(ARCH_REGS, self.cap)) - set(precise_map.values())
        self.alloc_ptr = ARCH_REGS
        self.dispatched.clear()
        self.squashed_seqs.clear()
        self.dead_tags.clear()
        return ("exception",
                f"seq={seq} 在队首触发精确异常（{cause}）：冲刷 ROB 共 {len(flushed)} 项"
                f"（含异常指令自身），任何结果均不提交，架构映射恢复至异常前状态",
                {"seq": seq, "cause": cause, "flushed": flushed,
                 "fault_tag": fault_tag, "precise_map": precise_map})

    # ---------------- 重放 ----------------

    def run(self) -> dict:
        handlers = {
            "dispatch": self._do_dispatch,
            "writeback": self._do_writeback,
            "resolve": self._do_resolve,
            "commit": self._do_commit,
            "exception": self._do_exception,
        }
        for i, ev in enumerate(self.events):
            before = self._snapshot()
            try:
                outcome, message, info = handlers[ev["type"]](ev)
            except Violation as v:
                record = {
                    "index": i,
                    "event": ev,
                    "ok": False,
                    "outcome": "violation",
                    "code": v.code,
                    "message": v.message,
                    "evidence": v.evidence,
                    "before": before,
                    "after": self._snapshot(),
                }
                self.log.append(record)
                self.first_violation = record
                break
            self.log.append({
                "index": i,
                "event": ev,
                "ok": True,
                "outcome": outcome,
                "message": message,
                "detail": info,
                "before": before,
                "after": self._snapshot(),
            })

        return {
            "verdict": "violation" if self.first_violation else "consistent",
            "first_violation": self.first_violation,
            "events": self.log,
            "total_events": len(self.events),
            "processed_events": len(self.log),
            "params": {
                "rob_depth": self.rob_depth,
                "physical_capacity": self.cap,
                "grow_mode": self.grow_mode,
            },
            "final_state": self._snapshot(),
        }


def analyze(trace: Any) -> dict:
    norm = normalize_trace(trace)
    return Simulator(norm).run()
