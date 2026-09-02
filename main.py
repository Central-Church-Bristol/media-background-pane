import ctypes
import sys

ERROR_ALREADY_EXISTS = 183
_SINGLETON = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\MediaBackgroundPane.SingleInstance")
if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
    sys.exit(0)

from media_background_pane.app import main

if __name__ == "__main__":
    main()
