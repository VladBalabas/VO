"""Recheck N=4 at 1000 rps with two synchronized 500-rps async generators."""
from __future__ import annotations

import json
import statistics
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from run_lab2 import OUT, ROOT, PROJECT, summary


def main():
    merged_path=OUT/"raw"/"n4_1000_recheck.json"
    if merged_path.exists():
        raise RuntimeError("Refusing to overwrite high-rate recheck")
    start_epoch=time.time()+15
    processes=[]
    commands=[]
    parts=[]

    def stats():
        time.sleep(max(0,start_epoch+12-time.time()))
        args=["docker","stats","--no-stream","--format",
              "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}"]
        result=subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (OUT/"stats"/"n4_1000_recheck.json").write_text(json.dumps(
            {"epoch":time.time(),"command":args,"exit_code":result.returncode,
             "output":result.stdout},indent=2),encoding="utf-8")

    for index in range(2):
        part=OUT/"raw"/f"n4_1000_part{index}.json"
        if part.exists():
            raise RuntimeError(f"Refusing to overwrite {part}")
        args=["docker","compose","-p",PROJECT,"--profile","load","run","--rm","--no-deps",
              "--entrypoint","python","load-generator","/load/lab2_async_generator.py","open",
              "--path","/referrals/{id}","--seed-file","/results/lab2/seed.json",
              "--rate","500","--duration","20","--timeout","5","--sample-interval-ms","100",
              "--start-epoch",str(start_epoch),"--output",f"/results/lab2/raw/n4_1000_part{index}.json"]
        commands.append(args)
        parts.append(part)
        processes.append(subprocess.Popen(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT))
    sampler=threading.Thread(target=stats,daemon=True); sampler.start()
    outputs=[p.communicate(timeout=60)[0] for p in processes]
    sampler.join(timeout=5)
    codes=[p.returncode for p in processes]
    if any(codes):
        raise RuntimeError(f"Recheck containers failed: {codes}; {outputs}")
    raw_parts=[json.loads(path.read_text(encoding="utf-8")) for path in parts]
    records=[]
    for index,part in enumerate(raw_parts):
        if abs(part["started_at_epoch"]-start_epoch)>0.001:
            raise AssertionError("Start epochs differ")
        for row in part["samples"]:
            row["request_index"]=row["request_index"]*2+index
            records.append(row)
    records.sort(key=lambda r:r["request_index"])
    lags=sorted(r["schedule_lag_ms"] for r in records)
    merged={"schema_version":3,"generator_implementation":"two_synchronized_aiohttp_processes",
            "latency_origin":"scheduled_at","started_at_epoch":start_epoch,
            "nominal_duration_seconds":20,"requested_rate_rps":1000,
            "path_template":"/referrals/{id}","requests_scheduled":len(records),
            "requests_sent":sum(p["requests_sent"] for p in raw_parts),
            "responses_2xx":sum(p["responses_2xx"] for p in raw_parts),
            "responses_non_2xx":sum(p["responses_non_2xx"] for p in raw_parts),
            "request_exceptions":sum(p["request_exceptions"] for p in raw_parts),
            "worker_peak_active_by_process":[p["worker_peak_active_by_process"][0] for p in raw_parts],
            "worker_capacity":None,"schedule_lag_p95_ms":lags[int(.95*(len(lags)-1))],
            "status_counts":dict(Counter(str(r["status"]) for r in records)),
            "samples":records}
    merged_path.write_text(json.dumps(merged,ensure_ascii=False,indent=2),encoding="utf-8")
    for part in parts:
        part.unlink()
    step=summary(merged_path,1000)
    (OUT/"steps"/"n4_1000_recheck.json").write_text(json.dumps(step,ensure_ascii=False,indent=2),encoding="utf-8")
    (OUT/"logs"/"n4_1000_recheck.json").write_text(json.dumps(
        {"commands":commands,"exit_codes":codes,"outputs":outputs,
         "start_epoch":start_epoch},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"x":step["x"],"sent_during_step":step["sent_during_step"],
                      "scheduled":step["scheduled"],"schedule_lag_p95_ms":step["schedule_lag_p95_ms"],
                      "exceptions":step["exceptions"]},indent=2))


if __name__=="__main__":
    main()
