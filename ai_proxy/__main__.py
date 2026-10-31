"""Run the proxy with one command (from the project root):

    python -m ai_proxy
"""
import os
import sys

# Ensure project root is in sys.path so app, bot, store can be imported from anywhere
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_cwd = os.getcwd()
if _cwd not in sys.path:
    sys.path.insert(0, _cwd)

try:
    import uvicorn
    from app import app as fastapi_app
except ImportError:
    # 1. Check if a virtual environment exists (.venv or venv) and re-exec
    re_exec_done = False
    for venv_dir in (".venv", "venv", "../.venv", "../venv"):
        candidate = os.path.join(venv_dir, "bin", "python")
        if os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(sys.executable):
            try:
                os.execv(candidate, [candidate] + sys.argv)
                re_exec_done = True
                break
            except Exception:
                pass

    if not re_exec_done:
        # 2. If no virtualenv or re-exec failed, auto-install requirements.txt
        req_file = "requirements.txt"
        if not os.path.exists(req_file) and os.path.exists("../requirements.txt"):
            req_file = "../requirements.txt"

        if os.path.exists(req_file):
            print(f"[ai-proxy] Missing dependencies. Installing from {req_file}...")
            import subprocess
            res = subprocess.call([sys.executable, "-m", "pip", "install", "-r", req_file])
            if res == 0:
                import uvicorn
                from app import app as fastapi_app
            else:
                print("\n[ai-proxy] Failed to auto-install dependencies. Please run:\n    pip install -r requirements.txt")
                sys.exit(1)
        else:
            print("\n[ai-proxy] Error: dependencies not found. Please run:\n    pip install -r requirements.txt")
            sys.exit(1)


def main():
    uvicorn.run(fastapi_app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
