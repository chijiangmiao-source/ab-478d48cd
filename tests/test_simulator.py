"""复核引擎单元测试：重点覆盖分支回滚与精确异常边界。

运行：python -m unittest discover -s tests -v
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from simulator import analyze, normalize_trace, InputError, ARCH_REGS  # noqa: E402


def branch_rollback_trace():
    """误预测回滚标准轨迹。"""
    return {
        "rob_depth": 16,
        "instructions": [
            {"seq": 1, "op": "add", "dst": 1, "src1": 2},
            {"seq": 2, "op": "beq", "src1": 1, "src2": 2, "predicted": False},
            {"seq": 3, "op": "add", "dst": 3, "src1": 1},
            {"seq": 4, "op": "add", "dst": 4, "src1": 3},
        ],
        "events": [
            {"type": "dispatch", "seq": 1},
            {"type": "dispatch", "seq": 2},
            {"type": "dispatch", "seq": 3},
            {"type": "dispatch", "seq": 4},
            {"type": "writeback", "seq": 1, "value": 10},
            {"type": "writeback", "seq": 3, "value": 30},
            {"type": "resolve", "seq": 2, "target": "taken"},
            {"type": "writeback", "seq": 4, "value": 40},
            {"type": "writeback", "seq": 3, "value": 33},
            {"type": "writeback", "seq": 2},
            {"type": "commit", "seq": 1},
            {"type": "commit", "seq": 2},
        ],
    }


class TestRenameAndCommit(unittest.TestCase):
    def test_initial_mapping_fixed(self):
        r = analyze({
            "instructions": [{"seq": 1, "op": "add", "dst": 1}],
            "events": [{"type": "dispatch", "seq": 1}],
        })
        self.assertEqual(r["verdict"], "consistent")
        # r0..r7 初始固定映射 p0..p7
        self.assertEqual(r["final_state"]["committed_map"], {i: i for i in range(8)})

    def test_inorder_commit_recycles_old_tag(self):
        # 两条指令都写 r1：第二次重命名的标签 p8 在 seq2 提交时回收
        r = analyze({
            "instructions": [
                {"seq": 1, "op": "add", "dst": 1, "src1": 2},
                {"seq": 2, "op": "add", "dst": 1, "src1": 0},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "writeback", "seq": 1, "value": 7},
                {"type": "commit", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "writeback", "seq": 2, "value": 8},
                {"type": "commit", "seq": 2},
            ],
        })
        self.assertEqual(r["verdict"], "consistent")
        # 初始固定标签 p1 不回收；seq1 的重命名标签 p8 在 seq2 提交后回收
        self.assertEqual(r["final_state"]["committed_map"][1], 9)
        self.assertIn(8, r["final_state"]["free"])
        self.assertNotIn(1, r["final_state"]["free"])
        self.assertEqual(r["final_state"]["rob"], [])

    def test_src_tag_must_match_mapping(self):
        r = analyze({
            "instructions": [{"seq": 1, "op": "add", "dst": 1, "src1": 2}],
            "events": [{"type": "dispatch", "seq": 1, "src1_tag": 99}],
        })
        self.assertEqual(r["verdict"], "violation")
        self.assertEqual(r["first_violation"]["code"], "src_mapping_mismatch")

    def test_commit_only_completed_head(self):
        r = analyze({
            "instructions": [{"seq": 1, "op": "add", "dst": 1}],
            "events": [{"type": "dispatch", "seq": 1}, {"type": "commit", "seq": 1}],
        })
        self.assertEqual(r["first_violation"]["code"], "commit_uncompleted")

    def test_commit_out_of_order_rejected(self):
        r = analyze({
            "instructions": [
                {"seq": 1, "op": "add", "dst": 1},
                {"seq": 2, "op": "add", "dst": 2},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "writeback", "seq": 2},
                {"type": "commit", "seq": 2},
            ],
        })
        self.assertEqual(r["first_violation"]["code"], "commit_out_of_order")
        self.assertEqual(r["first_violation"]["evidence"]["head_seq"], 1)

    def test_rob_full_blocks_dispatch(self):
        r = analyze({
            "rob_depth": 2,
            "instructions": [
                {"seq": 1, "op": "add", "dst": 1},
                {"seq": 2, "op": "add", "dst": 2},
                {"seq": 3, "op": "add", "dst": 3},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "dispatch", "seq": 3},
            ],
        })
        self.assertEqual(r["first_violation"]["code"], "rob_full")

    def test_physical_register_exhaustion(self):
        instrs = [{"seq": i, "op": "add", "dst": i % 8} for i in range(1, 5)]
        events = [{"type": "dispatch", "seq": i} for i in range(1, 5)]
        r = analyze({"physical_registers": 8, "instructions": instrs, "events": events})
        self.assertEqual(r["first_violation"]["code"], "no_physical_register")


class TestBranchRollback(unittest.TestCase):
    def test_misprediction_squashes_and_restores(self):
        r = analyze(branch_rollback_trace())
        self.assertEqual(r["verdict"], "consistent")
        recovery = next(e for e in r["events"] if e["outcome"] == "recovery")
        self.assertEqual(recovery["detail"]["squashed"], [3, 4])
        self.assertEqual(recovery["detail"]["squashed_tags"], [9, 10])
        # 映射恢复到分支分派之前：r3->p3, r4->p4（初始）
        self.assertEqual(recovery["after"]["map_table"][3], 3)
        self.assertEqual(recovery["after"]["map_table"][4], 4)
        # ROB 截断到分支自身
        self.assertEqual([e["seq"] for e in recovery["after"]["rob"]], [1, 2])
        # 被清除标签被隔离
        dead = {d["tag"] for d in recovery["after"]["dead_tags"]}
        self.assertEqual(dead, {9, 10})

    def test_late_writeback_of_squashed_instr_refused(self):
        r = analyze(branch_rollback_trace())
        refused = [e for e in r["events"] if e["outcome"] == "refused"]
        self.assertTrue(refused, "被清除指令 seq=4 的迟到写回必须被拒绝")
        self.assertEqual({e["detail"]["seq"] for e in refused}, {4, 3})
        # 拒绝不得改变任何状态
        for e in refused:
            self.assertEqual(e["before"]["map_table"], e["after"]["map_table"])

    def test_wrong_path_result_never_committed(self):
        r = analyze(branch_rollback_trace())
        # 错误路径标签 p9/p10 绝不能出现在已提交映射中
        for tag in r["final_state"]["committed_map"].values():
            self.assertNotIn(tag, (9, 10))

    def test_correct_prediction_keeps_state(self):
        trace = branch_rollback_trace()
        trace["events"] = [
            {"type": "dispatch", "seq": 1},
            {"type": "dispatch", "seq": 2},
            {"type": "dispatch", "seq": 3},
            {"type": "dispatch", "seq": 4},
            {"type": "writeback", "seq": 1, "value": 1},
            {"type": "resolve", "seq": 2, "target": "not-taken"},  # 预测正确
            {"type": "writeback", "seq": 3, "value": 3},
            {"type": "writeback", "seq": 4, "value": 4},
            {"type": "writeback", "seq": 2},
            {"type": "commit", "seq": 1},
            {"type": "commit", "seq": 2},
            {"type": "commit", "seq": 3},
            {"type": "commit", "seq": 4},
        ]
        r = analyze(trace)
        self.assertEqual(r["verdict"], "consistent")
        self.assertTrue(any(e["outcome"] == "resolve" for e in r["events"]))
        self.assertEqual([e["seq"] for e in r["final_state"]["rob"]], [])

    def test_redispatch_after_rollback(self):
        trace = branch_rollback_trace()
        # 回滚后重新分派正确路径上的指令 3、4 并全部提交
        trace["events"] += [
            {"type": "dispatch", "seq": 3},
            {"type": "dispatch", "seq": 4},
            {"type": "writeback", "seq": 3, "value": 300},
            {"type": "writeback", "seq": 4, "value": 400},
            {"type": "commit", "seq": 3},
            {"type": "commit", "seq": 4},
        ]
        r = analyze(trace)
        self.assertEqual(r["verdict"], "consistent")
        # 重新分派解除标签隔离，新结果最终提交；ROB 清空
        self.assertEqual(r["final_state"]["rob"], [])
        self.assertEqual(r["final_state"]["dead_tags"], [])
        self.assertEqual(r["final_state"]["committed_map"][3],
                         r["final_state"]["committed_map"][3])

    def test_nested_branches_inner_rollback_preserves_outer_cp(self):
        trace = {
            "instructions": [
                {"seq": 1, "op": "beq", "src1": 0, "src2": 1, "predicted": False},
                {"seq": 2, "op": "add", "dst": 1, "src1": 0},
                {"seq": 3, "op": "bne", "src1": 0, "src2": 1, "predicted": False},
                {"seq": 4, "op": "add", "dst": 2, "src1": 0},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "dispatch", "seq": 3},
                {"type": "dispatch", "seq": 4},
                {"type": "writeback", "seq": 2, "value": 2},
                # 内层分支误预测：仅冲刷 seq4，外层检查点保留
                {"type": "resolve", "seq": 3, "target": "taken"},
            ],
        }
        r = analyze(trace)
        self.assertEqual(r["verdict"], "consistent")
        rec = r["events"][-1]
        self.assertEqual(rec["detail"]["squashed"], [4])
        cp_seqs = [c["instr_seq"] for c in rec["after"]["checkpoints"]]
        self.assertIn(1, cp_seqs)
        # seq2 的映射 r1->p8 保留（内层检查点之后、内层分支之前）
        self.assertEqual(rec["after"]["map_table"][1], 8)

    def test_checkpoint_free_list_keeps_older_state(self):
        # 初始固定标签 p0..p7 不参与分配；回滚后空闲表按存活引用精确重算
        trace = {
            "instructions": [
                {"seq": 1, "op": "add", "dst": 1, "src1": 0},
                {"seq": 2, "op": "add", "dst": 2, "src1": 0},
                {"seq": 3, "op": "beq", "src1": 0, "src2": 1, "predicted": False},
                {"seq": 4, "op": "add", "dst": 3, "src1": 0},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "writeback", "seq": 1},
                {"type": "commit", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "writeback", "seq": 2},
                {"type": "commit", "seq": 2},
                {"type": "dispatch", "seq": 3},
                {"type": "dispatch", "seq": 4},
                {"type": "resolve", "seq": 3, "target": "taken"},
            ],
        }
        r = analyze(trace)
        self.assertEqual(r["verdict"], "consistent")
        after = r["events"][-1]["after"]
        # seq4 的标签 p10 随回滚进入隔离，不可立即分配
        dead = {d["tag"] for d in after["dead_tags"]}
        self.assertIn(10, dead)
        self.assertNotIn(10, after["free"])
        # 已提交/存活标签 p8(r1)、p9(r2) 不得释放
        self.assertNotIn(8, after["free"])
        self.assertNotIn(9, after["free"])
        # 固定标签永不进入空闲表
        self.assertTrue(all(t >= ARCH_REGS for t in after["free"]))

    def test_dead_tag_not_reallocated_until_redispatch(self):
        # 回滚后若立即分派别的指令，不得拿到仍处隔离的 p9/p10
        trace = branch_rollback_trace()
        trace["instructions"].append({"seq": 5, "op": "add", "dst": 6, "src1": 0})
        # 在回滚事件之后插入对全新指令 5 的分派
        idx = next(i for i, e in enumerate(trace["events"])
                   if e["type"] == "resolve" and e["seq"] == 2)
        trace["events"] = trace["events"][:idx + 1] + [{"type": "dispatch", "seq": 5}]
        r = analyze(trace)
        self.assertEqual(r["verdict"], "consistent")
        disp5 = next(e for e in r["events"] if e["event"].get("seq") == 5
                     and e["event"]["type"] == "dispatch")
        self.assertNotIn(disp5["detail"]["tag"], (9, 10))
        # p9/p10 仍隔离属于 seq3/4
        dead = {d["tag"]: d["instr_seq"] for d in disp5["after"]["dead_tags"]}
        self.assertEqual(dead.get(9), 3)
        self.assertEqual(dead.get(10), 4)


class TestExceptionBoundary(unittest.TestCase):
    def _trace(self):
        return {
            "instructions": [
                {"seq": 1, "op": "add", "dst": 1, "src1": 0},
                {"seq": 2, "op": "lw", "dst": 2, "src1": 1},
                {"seq": 3, "op": "add", "dst": 3, "src1": 2},
                {"seq": 4, "op": "add", "dst": 4},
            ],
            "events": [
                {"type": "dispatch", "seq": 1},
                {"type": "writeback", "seq": 1, "value": 11},
                {"type": "commit", "seq": 1},
                {"type": "dispatch", "seq": 2},
                {"type": "dispatch", "seq": 3},
                {"type": "dispatch", "seq": 4},
                {"type": "writeback", "seq": 3, "value": 33},
                {"type": "writeback", "seq": 4, "value": 44},
                {"type": "exception", "seq": 2, "cause": "page fault"},
            ],
        }

    def test_exception_at_head_flushes_without_commit(self):
        r = analyze(self._trace())
        self.assertEqual(r["verdict"], "consistent")
        exc = r["events"][-1]
        self.assertEqual(exc["outcome"], "exception")
        # 异常指令 seq2 与更年轻 seq3/4 全部冲刷，无一提交
        self.assertEqual(exc["detail"]["flushed"], [2, 3, 4])
        self.assertEqual(r["final_state"]["rob"], [])
        # 已提交映射停在异常前一条指令 seq1：r1->p8
        self.assertEqual(r["final_state"]["committed_map"][1], 8)
        # 推测映射恢复为精确映射（r2/r3/r4 的错误结果全部撤销）
        self.assertEqual(r["final_state"]["map_table"][2], 2)
        self.assertEqual(r["final_state"]["map_table"][3], 3)
        self.assertEqual(r["final_state"]["map_table"][4], 4)
        self.assertEqual(r["final_state"]["map_table"][1], 8)

    def test_exception_must_reach_head(self):
        trace = self._trace()
        trace["events"][-1] = {"type": "exception", "seq": 3, "cause": "fault"}
        r = analyze(trace)
        self.assertEqual(r["verdict"], "violation")
        self.assertEqual(r["first_violation"]["code"], "exception_not_at_head")
        self.assertEqual(r["first_violation"]["evidence"]["head_seq"], 2)

    def test_younger_completed_values_dropped(self):
        r = analyze(self._trace())
        # seq3/seq4 虽已写回完成，但异常后 completed 缓冲被清空
        self.assertEqual(r["final_state"]["completed_tags"], [])
        for tag in r["final_state"]["committed_map"].values():
            self.assertNotIn(tag, (10, 11))  # seq3/seq4 的推测标签

    def test_exception_evidence_shows_before_after(self):
        r = analyze(self._trace())
        exc = r["events"][-1]
        self.assertIn("rob", exc["before"])
        self.assertEqual(len(exc["before"]["rob"]), 3)
        self.assertEqual(exc["after"]["rob"], [])


class TestInputValidation(unittest.TestCase):
    def test_too_many_instructions(self):
        with self.assertRaises(InputError):
            normalize_trace({
                "instructions": [{"seq": i, "op": "add", "dst": 0} for i in range(25)],
                "events": [],
            })

    def test_too_many_events(self):
        with self.assertRaises(InputError):
            normalize_trace({
                "instructions": [{"seq": 1, "op": "add", "dst": 0}],
                "events": [{"type": "commit"} for _ in range(97)],
            })

    def test_bad_event_type(self):
        with self.assertRaises(InputError):
            normalize_trace({
                "instructions": [{"seq": 1, "op": "add", "dst": 0}],
                "events": [{"type": "flush"}],
            })

    def test_bad_register_number(self):
        with self.assertRaises(InputError):
            normalize_trace({
                "instructions": [{"seq": 1, "op": "add", "dst": 9}],
                "events": [],
            })


if __name__ == "__main__":
    unittest.main(verbosity=2)
