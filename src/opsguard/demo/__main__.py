import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Run the local OpsGuard React demo API")
    parser.add_argument("--cluster",required=True,help="Explicit existing kind cluster name")
    parser.add_argument("--alert-port",type=int,default=9093)
    args = parser.parse_args()
    import uvicorn
    from opsguard.demo.api import create_app
    uvicorn.run(create_app(Path.cwd(),args.cluster,args.alert_port),host="127.0.0.1",port=8000)

if __name__ == "__main__":
    main()
