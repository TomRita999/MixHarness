import json
import os
import sys
import tempfile
from pathlib import Path


def capture_statusline():
    state = json.load(sys.stdin.buffer)
    # 只交付额度与档位，过滤状态栏中的邮箱、目录和会话信息。
    snapshot = {k: state.get(k) for k in ("quota", "plan_tier", "agent_state")}
    path = Path(os.environ["MIXHARNESS_USAGE_FILE"])
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(snapshot, f)
    os.replace(f.name, path)
    print("MixHarness")


if __name__ == "__main__":
    capture_statusline()
