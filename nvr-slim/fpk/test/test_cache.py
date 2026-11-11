#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存落点（0.9.141）的行为守卫。

这一层最容易出的三类错：
  C1 **把"盘掉线"变成"写系统盘"**：`record_dir` 的挂载点在盘拔出后仍然存在，
     `os.makedirs("<挂载点>/_cache")` 会成功 —— 于是程序以为自己在写外接盘，
     其实写进了根分区。所以第一条钉的是"缺少 record_dir 本体时绝不创建它"。
  C2 **删了不该删的**：remux 清理按"最久没碰"淘汰，正在播放的那份（10 分钟内
     动过）必须跳过，否则用户越看越卡（每播一次都要重转一遍）。
  C3 **迁移把用户数据搬丢**：`shots` 是用户会回头看的图，必须搬；`remux`/`tmp`
     是纯再生物，必须直接删（跨卷搬 14GB 换不来任何东西）。

跑：python test/test_cache.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import cache  # noqa: E402

GB = 1024.0 ** 3


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-cache-")
        self.store = os.path.join(self.tmp, "store")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.store)
        os.makedirs(self.var)
        cache.reset()
        cache.bind(store_fn=lambda: self.store)

    def tearDown(self):
        cache.reset()
        cache.bind(store_fn=lambda: None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, path, size=10, age=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))


class TestRoot(Base):
    def test_on_store(self):
        r = cache.root(self.var)
        self.assertEqual(os.path.abspath(r),
                         os.path.abspath(os.path.join(self.store, "_cache")))
        self.assertTrue(cache.mode()["on_store"])

    def test_fallback_when_store_missing(self):
        shutil.rmtree(self.store)
        r = cache.root(self.var)
        self.assertEqual(os.path.abspath(r),
                         os.path.abspath(os.path.join(self.var, "_cache")))
        self.assertFalse(cache.mode()["on_store"])
        # C1：绝不能把 record_dir 本体建出来（那意味着写到挂载点所在的系统盘）
        self.assertFalse(os.path.exists(self.store))

    def test_fallback_when_store_readonly(self):
        if getattr(os, "geteuid", lambda: 0)() == 0:
            self.skipTest("root 无视只读权限，这条在 root 下没有意义")
        os.chmod(self.store, 0o500)
        try:
            r = cache.root(self.var)
            self.assertEqual(os.path.abspath(r),
                             os.path.abspath(os.path.join(self.var, "_cache")))
            self.assertFalse(cache.mode()["on_store"])
        finally:
            os.chmod(self.store, 0o755)

    def test_sub_creates(self):
        d = cache.sub(self.var, "shots")
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(os.path.basename(d), "shots")

    def test_no_store_bound(self):
        cache.bind(store_fn=lambda: None)
        cache.reset()
        r = cache.root(self.var)
        self.assertEqual(os.path.abspath(r),
                         os.path.abspath(os.path.join(self.var, "_cache")))
        self.assertFalse(cache.mode()["on_store"])


class TestPruneRemux(Base):
    def setUp(self):
        Base.setUp(self)
        self.rd = cache.sub(self.var, "remux")
        self.now = 1700000000.0

    def _mk(self, name, size, age):
        p = os.path.join(self.rd, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        t = self.now - age
        os.utime(p, (t, t))
        return p

    def test_drop_expired(self):
        old = self._mk("old.mp4", 100, 8 * 86400)
        new = self._mk("new.mp4", 100, 60)
        r = cache.prune_remux(self.var, max_bytes=GB, max_age=7 * 86400,
                              hold=0, now=self.now)
        self.assertEqual(r["removed"], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_hold_keeps_playing(self):
        # C2：10 分钟内动过的文件不参与容量淘汰（可能正在播）。
        # 超期淘汰不用这条 —— 超期的一定超过 10 分钟，两件事不重叠。
        p = self._mk("playing.mp4", 300, 60)
        r = cache.prune_remux(self.var, max_bytes=100, max_age=10 ** 9,
                              hold=600, now=self.now)
        self.assertEqual(r["removed"], 0)
        self.assertTrue(os.path.exists(p))

    def test_size_trim_prefers_oldest(self):
        # 最旧的可删、正在播的留下：删完一够数就停，不会把新的也带走
        playing = self._mk("playing.mp4", 100, 60)
        old = self._mk("old.mp4", 100, 3000)
        r = cache.prune_remux(self.var, max_bytes=150, max_age=10 ** 9,
                              hold=600, now=self.now)
        self.assertEqual(r["removed"], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(playing))

    def test_trim_by_size_oldest_first(self):
        a = self._mk("a.mp4", 60, 3000)
        b = self._mk("b.mp4", 60, 2000)
        c = self._mk("c.mp4", 60, 1000)
        r = cache.prune_remux(self.var, max_bytes=120, max_age=10 ** 9,
                              hold=0, now=self.now)
        self.assertEqual(r["removed"], 1)
        self.assertFalse(os.path.exists(a))      # 最旧的先走
        self.assertTrue(os.path.exists(b) and os.path.exists(c))

    def test_under_limit_noop(self):
        self._mk("a.mp4", 10, 100)
        r = cache.prune_remux(self.var, max_bytes=GB, now=self.now)
        self.assertEqual(r["removed"], 0)

    def test_bad_path_noop(self):
        shutil.rmtree(self.store)               # 盘没了
        r = cache.prune_remux(self.var, now=self.now)
        self.assertEqual(r["removed"], 0)


class TestBackup(Base):
    def _mkbackup(self, name, age, files=2):
        d = os.path.join(self.var, "backup", name)
        for i in range(files):
            self.touch(os.path.join(d, "f%d.bin" % i), 100)
        t = time.time() - age
        os.utime(d, (t, t))
        return d

    def test_keeps_newest_two(self):
        old = self._mkbackup("app-old", 3000)
        mid = self._mkbackup("app-mid", 2000)
        new = self._mkbackup("app-new", 100)
        r = cache.prune_backup(self.var, keep=2)
        self.assertEqual(r["removed"], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(mid) and os.path.exists(new))

    def test_noop_when_few(self):
        self._mkbackup("app-a", 100)
        self.assertEqual(cache.prune_backup(self.var)["removed"], 0)

    def test_missing_dir(self):
        self.assertEqual(cache.prune_backup(self.var)["removed"], 0)


class TestMigrate(Base):
    def test_moves_shots_drops_remux(self):
        old_shot = os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg")
        self.touch(old_shot, 100)
        self.touch(os.path.join(self.var, "remux", "a.mp4"), 200)
        self.touch(os.path.join(self.var, "tmp", "x.part"), 50)
        out = cache.migrate_once(self.var)
        self.assertEqual(out["shots"]["action"], "moved")
        new_shot = os.path.join(self.store, "_cache", "shots", "cam1",
                                "2026-11-10", "120000_000.jpg")
        self.assertTrue(os.path.isfile(new_shot))
        self.assertFalse(os.path.exists(old_shot))
        self.assertFalse(os.path.exists(os.path.join(self.var, "remux")))
        self.assertFalse(os.path.exists(os.path.join(self.var, "tmp")))
        self.assertTrue(os.path.isfile(os.path.join(self.var, cache.MIGRATE_MARK)))

    def test_empty_legacy_tree_is_removed(self):
        """搬完不能留空壳：152 升级后 `event_shots/cam-test01` 就是空的残留。

        `os.walk(topdown=False)` 给的 dirs 是遍历开始时的快照，子目录刚删掉
        父目录仍"看着"有子目录 —— 必须用 listdir 判真实空。
        """
        self.touch(os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg"), 10)
        cache.migrate_once(self.var)
        self.assertFalse(os.path.exists(os.path.join(self.var, "event_shots")))

    def test_empty_shells_cleaned_after_done(self):
        """标记已写之后，再遇到空壳也要收掉（首版迁移的残留）。"""
        self.touch(os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg"), 10)
        cache.migrate_once(self.var)
        os.makedirs(os.path.join(self.var, "event_shots", "cam-test01"))
        cache.migrate_once(self.var)
        self.assertFalse(os.path.exists(os.path.join(self.var, "event_shots")))

    def test_idempotent(self):
        self.touch(os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg"), 10)
        cache.migrate_once(self.var)
        self.assertEqual(cache.migrate_once(self.var).get("skipped"), "done")

    def test_skip_in_fallback(self):
        shutil.rmtree(self.store)
        self.touch(os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg"), 10)
        out = cache.migrate_once(self.var)
        self.assertEqual(out.get("skipped"), "fallback")
        # 回退态下不能动用户的图
        self.assertTrue(os.path.isfile(os.path.join(
            self.var, "event_shots", "cam1", "2026-11-10", "120000_000.jpg")))

    def test_existing_target_kept(self):
        # 目标已有同名同大小文件：删源、留目标，不报错
        self.touch(os.path.join(self.var, "event_shots", "cam1", "2026-11-10",
                                "120000_000.jpg"), 40)
        dst = os.path.join(self.store, "_cache", "shots", "cam1", "2026-11-10",
                           "120000_000.jpg")
        self.touch(dst, 40)
        cache.migrate_once(self.var)
        self.assertTrue(os.path.isfile(dst))
        self.assertFalse(os.path.exists(os.path.join(
            self.var, "event_shots", "cam1", "2026-11-10", "120000_000.jpg")))


class TestMergeBack(Base):
    def test_files_move_back_and_shell_removed(self):
        """盘恢复后回退期的文件要搬回去，var/_cache 不能留空壳。

        152 真机实测：回退 26 秒后恢复，图搬走了但 `var/_cache/shots` 空目录
        一直在 —— 只 rmdir 顶层，子目录还在，`listdir(src)` 就非空。
        """
        shutil.rmtree(self.store)
        cache.reset()
        p = os.path.join(self.var, "_cache", "shots", "cam1", "2026-11-10", "a.jpg")
        self.touch(p, 10)
        # 先解析一次落点，mode() 才知道当前在回退态（未解析时默认乐观）
        self.assertEqual(os.path.abspath(cache.root(self.var)),
                         os.path.abspath(os.path.join(self.var, "_cache")))
        self.assertFalse(cache.mode()["on_store"])
        os.makedirs(self.store)
        cache.reset()
        cache._merge_back(self.var)
        dst = os.path.join(self.store, "_cache", "shots", "cam1", "2026-11-10",
                           "a.jpg")
        self.assertTrue(os.path.isfile(dst))
        self.assertFalse(os.path.exists(os.path.join(self.var, "_cache")))


class TestStats(Base):
    def test_counts_kinds(self):
        self.touch(os.path.join(self.store, "_cache", "shots", "a", "1.jpg"), 100)
        self.touch(os.path.join(self.store, "_cache", "remux", "b.mp4"), 200)
        st = cache.stats(self.var, fresh=True)
        self.assertEqual(st["kinds"]["shots"], 100)
        self.assertEqual(st["kinds"]["remux"], 200)
        self.assertEqual(st["total"], 300)

    def test_missing_dir_zero(self):
        st = cache.stats(self.var, fresh=True)
        self.assertEqual(st["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
