import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("APP_PORT", "8765")), reload=True)
