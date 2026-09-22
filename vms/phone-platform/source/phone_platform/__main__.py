import argparse
import os

from .config import Config
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Switchboard phone platform")
    parser.add_argument("command",nargs="?",choices=["serve","login-link"],default="serve")
    parser.add_argument("--url",default="http://192.168.0.233:8088")
    args = parser.parse_args()
    os.umask(0o077)
    config = Config()
    if args.command == "login-link":
        store = Store(config.state_dir)
        print(args.url.rstrip("/")+"/#login="+store.login("link",900))
        store.close()
        return
    import uvicorn
    from .app import create_app
    # URLs may include the phone's restricted directory token; omit access logs.
    uvicorn.run(create_app(config),host=config.host,port=config.port,access_log=False,proxy_headers=False,ws_max_size=1024*1024)


if __name__ == "__main__":
    main()
