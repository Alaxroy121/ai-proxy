"""Run the proxy with one command (from the project root):

    python -m ai_proxy
"""
import os

import uvicorn

from app import app as fastapi_app


def main():
    uvicorn.run(fastapi_app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
