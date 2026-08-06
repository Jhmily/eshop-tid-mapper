#!/usr/bin/env python3
"""端到端编排: 按依赖顺序运行全部管线"""
import subprocess, sys
from pathlib import Path

DIR = Path(__file__).parent
STEPS = [
    ("hk_pipeline.py", "BASE TID 映射"),
    ("us_fetch.py", "US Algolia 数据"),
    ("enum_dlc.py", "DLC 枚举"),
    ("crawl_dlc.py", "DLC 元数据爬取"),
    ("merge_dlc.py", "合并输出"),
]

for script, desc in STEPS:
    print(f"\n{'='*40}\n  {desc} ({script})\n{'='*40}")
    r = subprocess.run([sys.executable, str(DIR / script)], cwd=str(DIR))
    if r.returncode != 0:
        print(f"  失败, 停止")
        sys.exit(r.returncode)
print(f"\n完成")
