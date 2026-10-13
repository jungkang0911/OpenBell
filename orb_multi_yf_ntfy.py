"""舊檔名相容：請改用 orb_monitor.py 或 python orb.py run。"""
from orb_monitor import *  # noqa: F401,F403
from orb_monitor import main

if __name__ == "__main__":
    main()
