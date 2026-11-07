#!/usr/bin/env python3
"""历史日志尾读测试（0.9.117）。

内存环只有 800 条，真机覆盖两三小时；查「昨天凌晨为什么断」必须读落盘文件。
而日志跑一个月能到几十 MB，readlines() 会把整份读进内存。这里锁住的是
「按字节从尾部倒读」的正确性：翻页不重不漏、半行必须丢掉、边界不能少一行。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import nvr_slim  # noqa: E402


def body(i):
    return "[nvr] %02d:%02d:00 第 %d 行 line-%d" % (i // 60 % 24, i % 60, i, i)


class TailReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-log-")
        self.path = os.path.join(self.tmp, "nvr.log")
        self.lines = [body(i) for i in range(5000)]
        self.write(self.lines)

    def write(self, lines, tail="\n", newline=b"\n"):
        data = newline.join(x.encode("utf-8") for x in lines) + (tail.encode() if tail else b"")
        with open(self.path, "wb") as fh:
            fh.write(data)

    def read(self, n, end=None):
        return nvr_slim._log_block(self.path, n, end)

    def test_tail_returns_last_lines_in_order(self):
        got, _, start, end, size = self.read(10)
        self.assertEqual(got, self.lines[-10:])
        self.assertEqual(end, size)
        self.assertTrue(start > 0)

    def test_start_is_line_boundary(self):
        """起点必须落在整行开头，否则前端第一行是缺头少尾的半截。"""
        for n in (7, 100, 801, 3000):
            got, _, start, _, _ = self.read(n)
            with open(self.path, "rb") as fh:
                fh.seek(start)
                raw = fh.read(len(got[0].encode("utf-8")))
            self.assertEqual(raw.decode("utf-8"), got[0], "n=%d" % n)

    def test_no_trailing_blank_line(self):
        """文件以换行结尾时，split 会多出一个空串 —— 不能当成一条日志行。"""
        got, _, _, _, _ = self.read(5)
        self.assertEqual(len(got), 5)
        self.assertFalse(any(x == "" for x in got))

    def test_no_final_newline(self):
        """最后一行没有换行符（进程被 kill）时，这一行仍是完整的一行。"""
        self.write(self.lines[:-1] + ["[nvr] 23:59:59 最后一行没换行"], tail="")
        got, _, _, _, _ = self.read(3)
        self.assertEqual(got[-1], "[nvr] 23:59:59 最后一行没换行")
        self.assertEqual(len(got), 3)

    def test_paging_covers_file_exactly(self):
        """从尾往前一页页翻，拼起来必须正好是全文件：不重、不漏、顺序对。"""
        got, _, start, _, size = self.read(800)
        pages = [got]
        while start > 0:
            got, _, start2, _, _ = self.read(800, start)
            self.assertLess(start2, start)
            pages.append(got)
            start = start2
        merged = []
        for p in reversed(pages):
            merged.extend(p)
        self.assertEqual(len(merged), len(self.lines))
        self.assertEqual(merged, self.lines)

    def test_more_flag(self):
        _, _, start, _, _ = self.read(10)
        self.assertTrue(start > 0)                 # 还有更早的
        got, _, start2, _, _ = self.read(5000)
        self.assertEqual(start2, 0)                # 一页读到文件头
        self.assertEqual(len(got), len(self.lines))

    def test_partial_first_line_dropped(self):
        """起点落在行中间时，那半截必须丢 —— 判断方式是真去看 pos-1 是不是换行。"""
        # 构造一个足够长的文件，让 64KB 的块边界正好切在某行中间
        big = ["[nvr] 00:00:%02d %s" % (i % 60, "x" * 200) for i in range(2000)]
        self.write(big)
        got, _, start, _, size = self.read(5)
        self.assertEqual(got, big[-5:])
        with open(self.path, "rb") as fh:
            fh.seek(start)
            self.assertTrue(fh.read(5) == b"[nvr]")

    def test_crlf_file(self):
        """文件被 Windows 侧工具碰过会变成 CRLF，行尾的 \\r 不能带进界面。"""
        self.write(self.lines[:20], newline=b"\r\n")
        got, _, _, _, _ = self.read(5)
        self.assertEqual(got, self.lines[15:20])
        self.assertFalse(any(x.endswith("\r") for x in got))

    def test_utf8_multibyte(self):
        """中文按字节倒读，不能被切成乱码。"""
        lines = ["[nvr] 01:00:00 归档完成：客厅 11-04 共 295 片" for _ in range(300)]
        self.write(lines)
        got, _, _, _, _ = self.read(50)
        self.assertEqual(got, lines[-50:])
        self.assertNotIn("\ufffd", "".join(got))


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-log-p-")
        # 日志固定写在 <var_dir>/log/nvr.log（_log_path），少一层目录就测了个空文件
        os.makedirs(os.path.join(self.tmp, "log"), exist_ok=True)
        self.path = os.path.join(self.tmp, "log", "nvr.log")
        nvr_slim.STATE["var_dir"] = self.tmp

    def put(self, n=500):
        with open(self.path, "wb") as fh:
            fh.write(("\n".join(body(i) for i in range(n)) + "\n").encode())

    def test_missing_file(self):
        d = self.nvr_slim_payload()
        self.assertTrue(d["ok"])
        self.assertFalse(d["exists"])
        self.assertEqual(d["lines"], [])
        self.assertFalse(d["more"])

    def nvr_slim_payload(self, n=None, end=None, access=False):
        return nvr_slim.log_file_payload(n, end, access)

    def put_mixed(self, biz=300, per_biz=4):
        """业务行 + 访问行混写：真机上访问行占九成，光拖一次进度条就是上百条。"""
        rows = []
        for i in range(biz):
            for k in range(per_biz):
                rows.append('02:%02d:%02d "GET /api/record/file?f=%d HTTP/1.1" 200 -'
                            % (i % 60, k, i * 10 + k))
            rows.append(body(i))
        with open(self.path, "wb") as fh:
            fh.write(("\n".join(rows) + "\n").encode())
        return rows

    def test_filters_access_lines_by_default(self):
        self.put_mixed()
        d = self.nvr_slim_payload(50)
        self.assertEqual(len(d["lines"]), 50)
        self.assertTrue(all(x.startswith("[nvr] ") for x in d["lines"]))
        self.assertEqual(d["lines"][-1], body(299))

    def test_access_flag_keeps_everything(self):
        total = len(self.put_mixed())
        d = self.nvr_slim_payload(50, None, True)
        self.assertEqual(len(d["lines"]), 50)
        self.assertTrue(any("GET /api" in x for x in d["lines"]))
        self.assertEqual(d["end"], os.path.getsize(self.path))
        self.assertGreater(total, 50)

    def test_start_matches_first_returned_line(self):
        """过滤之后 start 必须指向真正返回的第一行业务行，否则翻页会重复。"""
        self.put_mixed()
        d = self.nvr_slim_payload(50)
        with open(self.path, "rb") as fh:
            fh.seek(d["start"])
            raw = fh.read(len(d["lines"][0].encode("utf-8")) + 2)
        self.assertTrue(raw.startswith(d["lines"][0].encode("utf-8")), raw[:40])

    def test_paging_with_filter_is_exact(self):
        """一路翻到文件头：业务行不重、不漏、顺序不变。"""
        self.put_mixed()
        d = self.nvr_slim_payload(40)
        pages, start = [d["lines"]], d["start"]
        while start > 0:
            d = self.nvr_slim_payload(40, start)
            if not d["lines"]:
                break
            pages.append(d["lines"])
            self.assertLess(d["start"], start)
            start = d["start"]
        merged = []
        for p in reversed(pages):
            merged.extend(p)
        self.assertEqual(merged, [body(i) for i in range(300)])

    def test_mostly_access_file_still_returns_business_lines(self):
        """一整段全是访问行时，不能返回空页让人一直点「加载更早」。"""
        rows = ['02:00:%02d "GET /api/x HTTP/1.1" 200 -' % (i % 60) for i in range(3000)]
        rows += [body(i) for i in range(5)]
        with open(self.path, "wb") as fh:
            fh.write(("\n".join(rows) + "\n").encode())
        d = self.nvr_slim_payload(10)
        self.assertTrue(d["lines"])
        self.assertTrue(all(x.startswith("[nvr] ") for x in d["lines"]))
        self.assertEqual(d["lines"][-1], body(4))

    def test_empty_file(self):
        open(self.path, "wb").close()
        d = self.nvr_slim_payload()
        self.assertTrue(d["ok"])
        self.assertTrue(d["exists"])
        self.assertEqual(d["lines"], [])
        self.assertFalse(d["more"])

    def test_default_page(self):
        self.put(2000)
        d = self.nvr_slim_payload()
        self.assertEqual(len(d["lines"]), nvr_slim.LOG_TAIL_LINES)
        self.assertTrue(d["more"])
        self.assertEqual(d["lines"][-1], body(1999))

    def test_lines_clamped(self):
        self.put(6000)
        self.assertEqual(len(self.nvr_slim_payload(0)["lines"]), 1)
        self.assertEqual(len(self.nvr_slim_payload(-5)["lines"]), 1)
        self.assertEqual(len(self.nvr_slim_payload(999999)["lines"]),
                         nvr_slim.LOG_TAIL_MAX)
        # 非法字符串不能炸，回落默认
        self.assertEqual(len(self.nvr_slim_payload("abc")["lines"]),
                         nvr_slim.LOG_TAIL_LINES)

    def test_end_clamped(self):
        self.put(100)
        d = self.nvr_slim_payload(10, 10 ** 9)      # end 超过文件大小
        self.assertEqual(len(d["lines"]), 10)
        self.assertEqual(d["lines"][-1], body(99))

    def test_end_zero(self):
        self.put(100)
        d = self.nvr_slim_payload(10, 0)
        self.assertEqual(d["lines"], [])
        self.assertFalse(d["more"])

    def test_cleared_file_then_rewritten(self):
        """清空日志只把文件截成 0，之后再写的新行仍能正常读到。"""
        self.put(100)
        open(self.path, "wb").close()
        self.assertEqual(self.nvr_slim_payload()["lines"], [])
        with open(self.path, "wb") as fh:
            fh.write(b"[nvr] 09:00:00 v0.9.117 pid=1 listen=0.0.0.0:18080\n")
        d = self.nvr_slim_payload()
        self.assertEqual(d["lines"], ["[nvr] 09:00:00 v0.9.117 pid=1 listen=0.0.0.0:18080"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
