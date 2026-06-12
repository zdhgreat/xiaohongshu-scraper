"""允许 python -m updater 方式运行。"""

from .fp_updater import run
import sys

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="XHS 指纹池全量更新")
    parser.add_argument("--dry-run", action="store_true", help="只检查不写入")
    args = parser.parse_args()
    ok = run(dry_run=args.dry_run)
    sys.exit(0 if ok else 1)
