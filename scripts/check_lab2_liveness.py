"""Verify /livez is independent of database readiness in the isolated lab stack."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"results"/os.getenv("LAB2_RUN_NAME","lab2_final")/"liveness_check.json"
COMPOSE=["docker","compose","-p","vo-lab2"]


def command(args):
    p=subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    return {"command":args,"exit_code":p.returncode,"output":p.stdout}


def request(path):
    try:
        response=urllib.request.urlopen("http://localhost:8080"+path,timeout=5)
    except urllib.error.HTTPError as exc:
        response=exc
    with response:
        return {"path":path,"status":response.status,
                "headers":dict(response.headers),"body":response.read().decode("utf-8")}


def main():
    trace=[]
    trace.append(command(COMPOSE+["up","-d","--scale","web=1","web"]))
    trace.append(command(COMPOSE+["restart","proxy"]))
    before=[request("/livez"),request("/healthz")]
    try:
        trace.append(command(COMPOSE+["stop","db"]))
        time.sleep(3)
        during=[request("/livez"),request("/healthz")]
    finally:
        trace.append(command(COMPOSE+["up","-d","db"]))
    result={"before":before,"during_db_stop":during,"commands":trace}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"before":[x["status"] for x in before],
                      "during_db_stop":[x["status"] for x in during]},indent=2))
    if [x["status"] for x in during]!=[200,503]:
        raise RuntimeError("Liveness/readiness separation failed")


if __name__=="__main__":
    main()
