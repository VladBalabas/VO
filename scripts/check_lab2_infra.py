"""Record non-destructive infrastructure checks for the Lab 2 report."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"results"/os.getenv("LAB2_RUN_NAME","lab2_final")/"infra_check.json"
COMPOSE=["docker","compose","-p","vo-lab2"]


def shell(args):
    result=subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,encoding="utf-8",errors="replace")
    return {"command":args,"exit_code":result.returncode,"output":result.stdout}


def http(url):
    with urllib.request.urlopen(url,timeout=10) as response:
        return {"url":url,"status":response.status,"headers":dict(response.headers),
                "body":response.read().decode("utf-8",errors="replace")}


def main():
    commands=[shell(COMPOSE+["ps","--format","table {{.Service}}\t{{.Status}}\t{{.Ports}}"]),
              shell(COMPOSE+["ps","-q","web"]),
              shell(["docker","info","--format","{{.NCPU}} CPUs; {{.MemTotal}} bytes RAM"]),
              shell(["powershell","-NoProfile","-Command",
                     "Get-CimInstance Win32_ComputerSystem | Select-Object NumberOfLogicalProcessors,TotalPhysicalMemory | ConvertTo-Json -Compress"])]
    ids=commands[1]["output"].splitlines()
    for container_id in ids:
        commands.append(shell(["docker","inspect","--format",
                               "{{.Name}}|{{.State.Running}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.State.ExitCode}}",
                               container_id]))
        commands.append(shell(["docker","top",container_id]))
        commands.append(shell(["docker","exec",container_id,"python","-c",
                               "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/metrics'); print(r.status); print(r.read().decode())"]))
    checks=[http("http://localhost:8080/livez"),http("http://localhost:8080/healthz"),
            http("http://localhost:8080/noop"),
            http("http://localhost:9090/api/v1/query?query=up"),
            http("http://localhost:9090/api/v1/targets?state=active")]
    distribution=[http("http://localhost:8080/livez") for _ in range(40)]
    pods=Counter(next((value for key,value in item["headers"].items()
                       if key.lower()=="x-pod"),"missing") for item in distribution)
    result={"epoch":time.time(),"commands":commands,"http":checks,
            "distribution_requests":distribution,"pod_counts":dict(pods)}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"web_replicas":len(ids),
                      "http_status":[x["status"] for x in checks],
                      "pod_counts":dict(pods)},indent=2))
    targets=json.loads(checks[-1]["body"])["data"]["activeTargets"]
    web_up=[target for target in targets
            if target["labels"].get("job")=="web" and target["health"]=="up"]
    if len(ids)!=4 or len(pods)!=4 or "missing" in pods:
        raise RuntimeError("Expected balanced evidence from all four web replicas")
    if len(web_up)!=4:
        raise RuntimeError(f"Expected four UP web targets, got {len(web_up)}")


if __name__=="__main__":
    main()
