import uvicorn

from .config import CFG

if __name__ == "__main__":
    uvicorn.run("server.app:app", host=CFG.host, port=CFG.port, log_level="info")
