import logging

import uvicorn

from . import config

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("bridge.main:create_app", factory=True, host=config.HOST, port=config.PORT,
                proxy_headers=False, access_log=False, workers=1)
