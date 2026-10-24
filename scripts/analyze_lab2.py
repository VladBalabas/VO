"""Recalculate Lab 2 tables, USL and figures from immutable raw measurements."""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from functools import lru_cache
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUN_NAME=os.getenv("LAB2_RUN_NAME","lab2_final")
if not re.fullmatch(r"lab2(?:_[a-z0-9-]{1,40})?",RUN_NAME):
    raise ValueError("Invalid LAB2_RUN_NAME")
DATA = ROOT / "results" / RUN_NAME
FIG = DATA / "figures"


@lru_cache(maxsize=12)
def read(path: str):
    return json.loads((DATA / path).read_text(encoding="utf-8"))


def series_values(prom_data, key, start, end, *, include_inf=False):
    result = prom_data.get(key, {}).get("result", [])
    excluded = ("NaN",) if include_inf else ("NaN", "+Inf", "-Inf")
    return [(item["metric"], float(t), float(v))
            for item in result for t, v in item["values"]
            if start <= float(t) < end and v not in excluded]


def metric_mean(prom_data, key, start, end, selector=None):
    vals = [v for metric, _, v in series_values(prom_data, key, start, end)
            if selector is None or selector(metric)]
    return statistics.fmean(vals) if vals else None


def metric_latest(prom_data, key, start, end):
    rows=series_values(prom_data,key,start,end)
    if not rows:
        return None
    latest=max(t for _,t,_ in rows)
    return statistics.fmean(v for _,t,v in rows if t==latest)


def fit_usl(points):
    # Unconstrained two-parameter least squares, followed by boundary refit.
    rows = [(n-1, n*(n-1), n*x1/x-1) for n, x, x1 in points if n > 1]
    if len(rows) < 2:
        return None
    matrix = np.array([[u, v] for u, v, _ in rows], dtype=float)
    y = np.array([z for _, _, z in rows], dtype=float)
    sigma, kappa = np.linalg.lstsq(matrix, y, rcond=None)[0]
    if sigma < 0 and kappa < 0:
        sigma = kappa = 0.0
    elif sigma < 0:
        sigma = 0.0
        kappa = max(0.0, float(np.dot(matrix[:, 1], y) / np.dot(matrix[:, 1], matrix[:, 1])))
    elif kappa < 0:
        kappa = 0.0
        sigma = max(0.0, float(np.dot(matrix[:, 0], y) / np.dot(matrix[:, 0], matrix[:, 0])))
    return {"sigma": float(sigma), "kappa": float(kappa),
            "N_star": math.sqrt((1-sigma)/kappa) if kappa > 0 and sigma < 1 else None,
            "no_finite_maximum": bool(kappa == 0)}


def percentile(values, q):
    if not values:
        return None
    return sorted(values)[int(q * (len(values)-1))]


def validate_continuous_profile(raw):
    steps=raw["steps"]
    if raw.get("mode")!="profile" or raw["nominal_duration_seconds"]!=10+20*len(steps):
        raise AssertionError("Profile duration/mode mismatch")
    if any(step["start_offset_ms"]!=(10+20*i)*1000 or step["step_index"]!=i
           for i,step in enumerate(steps)):
        raise AssertionError("Profile has a gap or unexpected step order")
    expected=2000+sum(step["rate"]*20 for step in steps)
    if raw["requests_scheduled"]!=expected or len(raw["samples"])!=expected:
        raise AssertionError("Profile scheduled request count mismatch")
    if raw["requests_sent"]!=expected:
        raise AssertionError("Not every scheduled request was dispatched")


def server_bucket(prom_data, start, end):
    rows = series_values(prom_data, "server_buckets", start, end)
    if not rows:
        return None
    latest = max(t for _, t, _ in rows)
    buckets = sorted(((float("inf") if m["le"] == "+Inf" else float(m["le"]), v)
                      for m, t, v in rows if t == latest), key=lambda x:x[0])
    total = next((v for le,v in buckets if math.isinf(le)), None)
    if not total:
        return None
    lower = 0.0
    for upper, count in buckets:
        if count >= .99*total:
            if math.isinf(upper):
                return {"label":f">{lower:g} s", "max_error_ms":None}
            return {"label":f"({lower:g}, {upper:g}] s",
                    "max_error_ms":(upper-lower)*1000}
        lower = upper
    return None


def write_per_second():
    destination = DATA / "per_second"
    destination.mkdir(exist_ok=True)
    for file in sorted((DATA / "raw").glob("*_profile.json")):
        raw = read(f"raw/{file.name}")
        if raw.get("mode") != "profile":
            continue
        duration = int(raw["nominal_duration_seconds"])
        result = [{"second": sec,
                   "planned": 200 if sec < 10 else 200 * (1 + (sec - 10) // 20),
                   "dispatched": 0, "completed_2xx": 0, "non_2xx": 0,
                   "timeouts": 0, "connection_errors": 0,
                   "client_latencies": [], "client_2xx_latencies": []}
                  for sec in range(duration)]
        for record in raw["samples"]:
            sent_second = int(record["sent_offset_ms"] // 1000)
            if 0 <= sent_second < duration:
                result[sent_second]["dispatched"] += 1
            complete_second = int(record["completed_offset_ms"] // 1000)
            if not 0 <= complete_second < duration:
                continue
            row = result[complete_second]
            row["client_latencies"].append(record["latency_ms"])
            status = record["status"]
            if status is not None and 200 <= status < 300:
                row["completed_2xx"] += 1
                row["client_2xx_latencies"].append(record["latency_ms"])
            elif status is not None or record["error_type"] == "TimeoutError":
                row["non_2xx"] += 1
                row["timeouts"] += int(record["error_type"] == "TimeoutError")
            else:
                row["connection_errors"] += 1
        for row in result:
            good = row.pop("client_latencies")
            successful = row.pop("client_2xx_latencies")
            row["client_p50_ms"] = percentile(good, .5)
            row["client_p95_ms"] = percentile(good, .95)
            row["client_p99_ms"] = percentile(good, .99)
            row["client_2xx_p99_ms"] = percentile(successful, .99)
        (destination / file.name).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")


def verify_step_from_raw(prefix, step):
    raw=read(f"raw/{prefix}_profile.json")
    rows=raw["samples"]
    start=step["start_offset_ms"]
    counts=[0]*15
    for row in rows:
        offset=row["completed_offset_ms"]-start
        if 5000 <= offset < 20000 and row["status"] is not None and 200 <= row["status"] < 300:
            counts[int((offset-5000)//1000)] += 1
    independently_calculated=statistics.fmean(counts)
    if abs(independently_calculated-step["x"])>1e-8:
        raise AssertionError(f"Raw/step mismatch: {prefix}_{step['rate']}")
    if len(rows)!=raw["requests_scheduled"] or raw["requests_sent"]!=len(rows):
        raise AssertionError(f"Raw dispatch/count mismatch: {prefix}_{step['rate']}")
    return {"name":f"{prefix}_{step['rate']}","verified_x":independently_calculated,
            "planned":len(rows),"sent":raw["requests_sent"]}


def output_graphs(table, detail, g):
    FIG.mkdir(exist_ok=True)
    xs = [row["n"] for row in table]
    ys = [row["x"] for row in table]
    errs = [row["stddev"] for row in table]
    x1 = ys[0]
    fig, ax = plt.subplots(figsize=(7, 4.4))
    ax.errorbar(xs, ys, yerr=errs, fmt="o", capsize=5, label="Measured ± 1 SD")
    xx = np.linspace(1, 4.3, 200)
    ax.plot(xx, x1*xx, "--", label="Ideal linear")
    model = detail["usl"]
    if model:
        ax.plot(xx, x1*xx/(1+model["sigma"]*(xx-1)+model["kappa"]*xx*(xx-1)), label="USL")
    ax.axhline(g, color="gray", linestyle=":", label="Generator G")
    ax.set(xlabel="Web replicas N", ylabel="Successful completions / s", title="Scalability")
    ax.set_xticks(xs)
    ax.legend(); fig.tight_layout(); fig.savefig(FIG / "usl.png", dpi=160); plt.close(fig)

    for n in (1, 4):
        steps = read(f"series/n{n}.json")["steps"]
        raw = read(f"raw/n{n}_profile.json")
        seconds = read(f"per_second/n{n}_profile.json")
        metrics = read(f"prometheus/n{n}.json")
        start = raw["started_at_epoch"]
        duration = raw["nominal_duration_seconds"]
        fig, axes = plt.subplots(4, 1, figsize=(11, 12), sharex=True)
        in_flight_axis = axes[3].twinx()
        instance_colors = {}
        times = [row["second"] for row in seconds]
        axes[0].step(times, [row["planned"] for row in seconds], where="post",
                     color="tab:orange", label="Planned rps")
        axes[0].plot(times, [row["completed_2xx"] for row in seconds],
                     color="tab:blue", linewidth=1, label="2xx completions/s")
        axes[0].legend(loc="upper left")
        client_times = list(range(10, duration + 1, 5))
        client_p99 = [percentile([r["latency_ms"] for r in raw["samples"]
                                  if (t - 15) * 1000 <= r["completed_offset_ms"] < t * 1000], .99)
                      for t in client_times]
        axes[1].plot(client_times, client_p99, color="tab:blue", marker=".", label="Client p99")
        server = series_values(metrics, "server_p99", start + 10,
                               start + duration + 1, include_inf=True)
        # Prometheus returns +Inf when p99 lies above the largest finite bucket.
        # Plot its conservative lower bound at 1000 ms; the table keeps the
        # explicit ">1 s" bucket label so this is never presented as 1000 ms.
        server_y = [1000 if math.isinf(v) else v * 1000 for _, _, v in server]
        axes[1].plot([t - start for _, t, _ in server], server_y,
                     color="tab:red", marker=".",
                     label="Server p99 (>1 s rank plotted at lower bound)")
        axes[1].legend(loc="upper left")
        for key, axis, style in (("web_requests", axes[2], "-"),
                                 ("web_cpu", axes[3], "-"),
                                 ("web_in_flight", in_flight_axis, "--")):
            by_instance = {}
            for metric, t, value in series_values(metrics, key, start, start + duration + 1):
                by_instance.setdefault(metric.get("instance", "?"), []).append((t, value))
            if key == "web_requests" and len(by_instance) < n:
                raise AssertionError(f"Missing per-instance request rates for N={n}")
            for instance, values in by_instance.items():
                if instance not in instance_colors:
                    instance_colors[instance] = plt.cm.tab10(len(instance_colors))
                axis.plot([t - start for t, _ in values], [v for _, v in values],
                          color=instance_colors[instance], linestyle=style,
                          label=f"{instance} {key}")
        for axis in axes:
            axis.axvspan(0, 10, color="gray", alpha=.07)
            for step in steps:
                axis.axvline(step["start_offset_ms"] / 1000, color="gray", alpha=.2, linewidth=.7)
        axes[0].set(ylabel="requests/s", title=f"N={n}: continuous profile (shaded = warm-up)")
        axes[1].set(ylabel="p99, ms", title="Client and server p99 over rolling 15 s")
        axes[2].set(ylabel="requests/s", title="Completed HTTP requests per web instance")
        axes[2].legend(fontsize=7, loc="upper left", ncol=2)
        axes[3].set(xlabel="Time since profile start, s", ylabel="CPU cores",
                    title="CPU and in-flight per web instance")
        axes[3].set_ylim(0, .65)
        in_flight_axis.set_ylabel("In-flight requests")
        h1, l1 = axes[3].get_legend_handles_labels()
        h2, l2 = in_flight_axis.get_legend_handles_labels()
        axes[3].legend(h1 + h2, l1 + l2, fontsize=6, loc="upper left", ncol=2)
        fig.tight_layout()
        fig.savefig(FIG / f"n{n}_timeline.png", dpi=160)
        plt.close(fig)
        db_rates, db_conns, db_active, db_idle = [], [], [], []
        for step in steps:
            rate = step["rate"]
            epoch = step["start_epoch"]
            db_rates.append((rate, metric_mean(metrics, "db_tps", epoch + 5, epoch + 20)))
            db_conns.append((rate, metric_mean(metrics, "db_connections", epoch + 5, epoch + 20,
                                              lambda m: m.get("datname") == "referrals")))
            db_active.append((rate, metric_mean(metrics, "db_active", epoch + 5, epoch + 20,
                                               lambda m: m.get("datname") == "referrals")))
            db_idle.append((rate, metric_mean(metrics, "db_idle", epoch + 5, epoch + 20,
                                             lambda m: m.get("datname") == "referrals")))
        detail[f"n{n}_db_by_rate"] = {"connections": db_conns, "active": db_active,
                                       "idle": db_idle, "tps": db_rates}

    fig, axes = plt.subplots(2, 1, figsize=(7, 6), sharex=True)
    for n, color in ((1,"tab:blue"),(4,"tab:orange")):
        db = detail[f"n{n}_db_by_rate"]
        for key, linestyle in (("active","-"),("idle","--")):
            pairs=[(r,v) for r,v in db[key] if v is not None]
            if pairs:
                axes[0].plot(*zip(*pairs), marker="o", color=color, linestyle=linestyle,
                             label=f"N={n} {key}")
        pairs=[(r,v) for r,v in db["tps"] if v is not None]
        if pairs:
            axes[1].plot(*zip(*pairs), marker="o", color=color, label=f"N={n}")
    axes[0].axhline(15, color="tab:blue", linestyle="--", alpha=.5)
    axes[0].axhline(60, color="tab:orange", linestyle="--", alpha=.5)
    axes[0].set(ylabel="DB connections", title="PostgreSQL connections vs pool capacities")
    axes[1].set(xlabel="Requested rps", ylabel="Transactions / s", title="PostgreSQL TPS")
    axes[0].legend(); axes[1].legend(); fig.tight_layout()
    fig.savefig(FIG / "postgres_variant.png", dpi=160); plt.close(fig)


def main():
    write_per_second()
    validate_continuous_profile(read("raw/noop_profile.json"))
    g = read("generator_ceiling.json")["G"]
    if not read("generator_ceiling.json")["stop_two_misses"]:
        raise AssertionError("Generator ceiling was not established by two misses")
    verified=[]
    for s in read("generator_ceiling.json")["steps"]:
        verified.append(verify_step_from_raw("noop",s))
    table=[]; detail={}
    for n in range(1,5):
        steps=read(f"series/n{n}.json")["steps"]
        raw=read(f"raw/n{n}_profile.json")
        validate_continuous_profile(raw)
        for s in steps:
            verified.append(verify_step_from_raw(f"n{n}",s))
        winning=max(steps,key=lambda s:s["x"])
        epoch=winning["start_epoch"]
        metrics=read(f"prometheus/n{n}.json")
        window=(epoch+5,epoch+20)
        bucket=server_bucket(metrics,epoch+15,epoch+21)
        step_start=winning["start_offset_ms"]
        active=[s["in_flight"] for s in raw["in_flight_samples"]
                if step_start <= s["offset_ms"] < step_start+20000]
        table.append({"n":n,"rate":winning["rate"],"x":winning["x"],"stddev":winning["stddev"],
                      "client_p99_ms":percentile([r["latency_ms"] for r in raw["samples"]
                                                  if winning["start_offset_ms"]+5000 <= r["completed_offset_ms"]
                                                  < winning["start_offset_ms"]+20000], .99),
                      "client_2xx_p99_ms":winning["client_p99_ms"],
                      "server_p99_ms":None if (p:=metric_latest(metrics,"server_p99",epoch+15,epoch+21)) is None else p*1000,
                      "server_p99_bucket":bucket,
                      "non_2xx_fraction":winning["non_2xx"]/max(1,winning["completed_window"]),
                      "timeouts":winning["timeouts"],
                      "connection_errors":winning["connection_errors"],
                      "web_cpu_mean_cores":metric_mean(metrics,"web_cpu",*window),
                      "web_in_flight_mean":metric_mean(metrics,"web_in_flight",*window),
                      "db_connections":metric_mean(metrics,"db_connections",*window,
                                                lambda m:m.get("datname")=="referrals"),
                      "db_tps":metric_mean(metrics,"db_tps",*window),
                      "generator_limited":winning["x"]>=.9*g or steps[-1]["x"]>=.9*steps[-1]["rate"],
                      "last_step_rate":steps[-1]["rate"],"last_step_x":steps[-1]["x"],
                      "generator_peak_active":max(active) if active else None,
                      "generator_capacity":raw["worker_capacity"],
                      "dispatch_ratio":winning["sent_during_step"]/winning["scheduled"],
                      "schedule_lag_p95_ms":winning["schedule_lag_p95_ms"]})
    x1=table[0]["x"]
    valid=[(r["n"],r["x"],x1) for r in table if not r["generator_limited"]]
    detail["usl"]=fit_usl(valid) if not table[0]["generator_limited"] else None
    if detail["usl"] is not None and detail["usl"]["kappa"] == 0:
        detail["usl"]["asymptotic_throughput"] = x1 / detail["usl"]["sigma"] if detail["usl"]["sigma"] > 0 else None
    if detail["usl"] is not None:
        sigma=detail["usl"]["sigma"]
        kappa=detail["usl"]["kappa"]
        predictions={r["n"]:x1*r["n"]/(1+sigma*(r["n"]-1)+kappa*r["n"]*(r["n"]-1))
                     for r in table}
        detail["usl"]["predicted_x"] = predictions
        detail["usl"]["rmse_x"] = math.sqrt(statistics.fmean(
            (r["x"]-predictions[r["n"]])**2 for r in table if r["n"]>1))
    detail["G"]=g
    detail["raw_verification"]=verified
    detail["table"]=table
    n4_winner=max(read("series/n4.json")["steps"],key=lambda step:step["x"])
    n4_resource_window=(n4_winner["start_epoch"]+5,n4_winner["start_epoch"]+20)
    resource_samples=read("stats/n4_profile.json")["samples"]
    detail["n4_winning_resource_samples"]=[sample for sample in resource_samples
                                            if n4_resource_window[0]<=sample["epoch"]<n4_resource_window[1]]
    if not detail["n4_winning_resource_samples"]:
        raise AssertionError("No Docker/host resource snapshot in X(4) window")
    detail["n1_p99_comparison"] = []
    for step in read("series/n1.json")["steps"]:
        epoch=step["start_epoch"]
        metrics=read("prometheus/n1.json")
        p=metric_latest(metrics,"server_p99",epoch+15,epoch+21)
        detail["n1_p99_comparison"].append({"rate":step["rate"],
              "client_p99_ms":percentile([r["latency_ms"] for r in read("raw/n1_profile.json")["samples"]
                                          if step["start_offset_ms"]+5000 <= r["completed_offset_ms"]
                                          < step["start_offset_ms"]+20000], .99),
              "client_2xx_p99_ms":step["client_p99_ms"],
              "server_p99_ms":None if p is None else 1000*p,
              "bucket":server_bucket(metrics,epoch+15,epoch+21)})
    output_graphs(table,detail,g)
    (DATA/"analysis.json").write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"G":g,"table":table,"usl":detail["usl"]},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
